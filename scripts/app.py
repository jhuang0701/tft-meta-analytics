import os
import re
import time
import requests

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from groq import Groq
from dotenv import load_dotenv

from backend import (
    get_puuid, get_match_objects, get_top_players,
    build_unit_stats, build_item_stats, build_comp_stats,
    build_match_history, get_match_ids, detect_latest_set,
    build_unit_item_stats, UNIT_NAME_MAP, clean_trait_name, fetch_meta_context,
    get_player_stats,
)
from assets import get_unit_icon, get_item_icon, get_item_name, get_unit_cost, get_trait_icon, load_maps
from db import (
    get_cached_match_ids_batch, get_cached_matches_batch,
    save_match, get_conn, save_player_performance,
)

load_dotenv()

unit_map, item_map, item_name_map, unit_cost_map, trait_icon_map = load_maps()

# ---------------------------------------------------------------------------
# DB STATS (sidebar counters)
# ---------------------------------------------------------------------------
try:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM matches")
    match_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM players")
    player_count = c.fetchone()[0]
    c.close()
    conn.close()
except Exception:
    match_count  = 0
    player_count = 0

# ---------------------------------------------------------------------------
# SESSION STATE
# ---------------------------------------------------------------------------
for key, default in [
    ("active_tab", 0),
    ("analysis_data", None),
    ("ai_report", None),
    ("ai_report_player", None),
    ("chat_history", []),
    ("chat_player", None),
    ("chat_input_counter", 0),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
API_KEY      = os.getenv("RIOT_API_KEY")
HEADERS      = {"X-Riot-Token": API_KEY}
MATCH_REGION = "https://americas.api.riotgames.com"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def clean_unit_name(name: str) -> str:
    name    = re.sub(r"^TFT\d+_", "", name)
    name    = name.replace("_", " ")
    name    = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    cleaned = name.title()
    return UNIT_NAME_MAP.get(cleaned.lower(), cleaned)


def get_top_items_for_unit(unit_id, unit_item_df, n=3):
    if unit_item_df is None or unit_item_df.empty:
        return []
    unit_rows = unit_item_df[unit_item_df["unit"] == unit_id]
    if unit_rows.empty:
        return []
    top = unit_rows.sort_values("total", ascending=False).head(n)
    return [get_item_icon(row["item"]) for _, row in top.iterrows()]


COST_COLORS = {
    1: "#808080",
    2: "#22c55e",
    3: "#3b82f6",
    4: "#a855f7",
    5: "#f59e0b",
}


def icon_html(url, name, size=60, item_icons=None, unit_id=None):
    item_strip   = ""
    border_color = "#2a3a55"

    if item_icons:
        imgs = "".join(
            f'<img src="{iurl}" width="18" height="18" '
            f'style="border-radius:3px;border:1px solid #2a3a55;margin:1px;"/>'
            for iurl in item_icons
        )
        item_strip = (
            f'<div style="display:flex;justify-content:center;flex-wrap:wrap;'
            f'gap:1px;margin-top:3px">{imgs}</div>'
        )

    if unit_id:
        cost         = get_unit_cost(unit_id)
        border_color = COST_COLORS.get(cost, "#2a3a55")

    return f"""
    <div class="icon-container">
        <img src="{url}" width="{size}"
             style="border-radius:8px;border:2px solid {border_color};
                    box-shadow:0 0 6px {border_color}55;"
             title="{name}"/>
        <div class="centered-label">{name}</div>
        {item_strip}
    </div>"""


def section_header(text):
    st.markdown(f'<div class="section-header">{text}</div>', unsafe_allow_html=True)


def metric_card(col, label, value, sub="", accent=False):
    accent_color = "#c89b3c" if accent else "#f1f5f9"
    col.markdown(f"""
    <div class="metric-card">
        <div style="font-size:10px;letter-spacing:2px;text-transform:uppercase;
                    color:#4a5568;margin-bottom:8px;font-family:'Inter',sans-serif">{label}</div>
        <div style="font-size:30px;font-weight:700;color:{accent_color};
                    font-family:'Rajdhani',sans-serif;line-height:1">{value}</div>
        <div style="font-size:11px;color:#2a3a55;margin-top:6px;
                    font-family:'Inter',sans-serif">{sub}</div>
    </div>
    """, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# CORE ANALYSIS
# ---------------------------------------------------------------------------

def run_analysis(game_name: str, tag_line: str = "NA1") -> dict:
    puuid       = get_puuid(game_name, tag_line)          # raises on bad name / rate-limit
    your_matches = get_match_objects(puuid, count=50, ttl_hours=1)

    if not your_matches:
        raise ValueError(
            f"No TFT matches found for {game_name}#{tag_line} in the current set. "
            "Play some games and try again!"
        )

    players = get_top_players()[:300]
    challenger_puuids = [p["puuid"] for p in players if p.get("puuid")]

    cached_id_lists          = get_cached_match_ids_batch(challenger_puuids)
    all_challenger_match_ids = []

    for p in players:
        cpuuid = p.get("puuid")
        if not cpuuid:
            continue
        ids = cached_id_lists.get(cpuuid) or get_match_ids(cpuuid, count=50)
        all_challenger_match_ids.extend(ids)

    all_challenger_match_ids = list(set(all_challenger_match_ids))
    cached_matches = get_cached_matches_batch(all_challenger_match_ids)
    uncached       = [mid for mid in all_challenger_match_ids if mid not in cached_matches]

    for mid in uncached:
        url = f"{MATCH_REGION}/tft/match/v1/matches/{mid}"
        time.sleep(0.2)
        response = requests.get(url, headers=HEADERS)
        if response.status_code == 429:
            raise RuntimeError("Riot API rate limit hit while fetching Challenger matches. Please wait a minute.")
        if response.status_code == 200:
            data = response.json()
            save_match(mid, data)
            cached_matches[mid] = data

    challenger_matches = [m for m in cached_matches.values() if m]
    latest_set         = detect_latest_set(challenger_matches)
    challenger_matches = [
        m for m in challenger_matches
        if m.get("info", {}).get("tft_set_number") == latest_set
    ]

    history    = build_match_history(your_matches, puuid)
    placements = [r["placement"] for r in history]
    if placements:
        top4_rate = sum(1 for p in placements if p <= 4) / len(placements)
        save_player_performance(puuid, game_name, tag_line, top4_rate, len(placements))

    top_chall_units = build_unit_stats(challenger_matches, min_games=3) \
        .sort_values("score", ascending=False).head(10)

    meta_lines = ["CHALLENGER META (live from match data):"]
    meta_lines += [
        f"• {clean_unit_name(r['unit'])}: {r['top4_rate']*100:.0f}% top4"
        for _, r in top_chall_units.iterrows()
    ]
    meta_context = "\n".join(meta_lines)

    return {
        "your_units":                 build_unit_stats(your_matches, min_games=2),
        "challenger_units":           build_unit_stats(challenger_matches, min_games=3),
        "your_items":                 build_item_stats(your_matches, min_games=2),
        "challenger_items":           build_item_stats(challenger_matches, min_games=3),
        "your_comps":                 build_comp_stats(your_matches, min_games=2, puuid=puuid),
        "challenger_comps":           build_comp_stats(challenger_matches, min_games=3),
        "match_history":              history,
        "unit_item_stats":            build_unit_item_stats(your_matches),
        "challenger_unit_item_stats": build_unit_item_stats(challenger_matches),
        "game_name":                  game_name,
        "tag_line":                   tag_line,
        "meta_context":               meta_context,  # ← now built from real data
    }


# ---------------------------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="ChatTFT",
    page_icon="♟",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# THEME
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=Inter:wght@300;400;500;600&display=swap');
    * { box-sizing: border-box; }
    .stApp { background-color: #080c14; color: #c9d1e0; font-family: 'Inter', sans-serif; }
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0a0e1a 0%, #080c14 100%);
        border-right: 1px solid #1a2235;
    }
    section[data-testid="stSidebar"] .block-container { padding: 2rem 1.2rem; }
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    header { visibility: hidden; height: 0 !important; min-height: 0 !important; }
    .stDeployButton { display: none; }
    .block-container { padding: 2rem 2.5rem 4rem 2.5rem; max-width: 1400px; }
    h1, h2, h3 { font-family: 'Rajdhani', sans-serif; color: #ffffff; letter-spacing: 0.5px; }
    .stTabs [data-baseweb="tab-list"] {
        background: transparent; border-bottom: 1px solid #1a2235;
        gap: 0; margin-bottom: 1.5rem;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: 'Rajdhani', sans-serif; font-size: 13px; font-weight: 600;
        letter-spacing: 2px; text-transform: uppercase; color: #4a5568;
        background: transparent; border: none; padding: 12px 24px;
        border-bottom: 2px solid transparent; transition: all 0.2s;
    }
    .stTabs [aria-selected="true"] {
        color: #c89b3c !important; border-bottom: 2px solid #c89b3c !important;
        background: transparent !important;
    }
    .stTabs [data-baseweb="tab"]:hover { color: #8a9bb5 !important; }
    .stTextInput input {
        background: #0d1221 !important; border: 1px solid #1e2d47 !important;
        border-radius: 8px !important; color: #c9d1e0 !important;
        font-family: 'Inter', sans-serif !important; font-size: 14px !important;
        padding: 10px 14px !important;
    }
    .stTextInput input:focus {
        border-color: #c89b3c !important;
        box-shadow: 0 0 0 2px rgba(200,155,60,0.15) !important;
    }
    .stTextInput label {
        color: #4a5568 !important; font-size: 11px !important;
        letter-spacing: 1.5px !important; text-transform: uppercase !important;
        font-family: 'Inter', sans-serif !important;
    }
    .stButton button {
        background: linear-gradient(135deg, #c89b3c, #a07830) !important;
        color: #080c14 !important; font-family: 'Rajdhani', sans-serif !important;
        font-weight: 700 !important; font-size: 13px !important;
        letter-spacing: 2px !important; text-transform: uppercase !important;
        border: none !important; border-radius: 8px !important;
        padding: 10px 24px !important; width: 100% !important;
        transition: all 0.2s !important; margin-top: 8px !important;
    }
    .stButton button:hover {
        background: linear-gradient(135deg, #d4a84a, #b08840) !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 4px 20px rgba(200,155,60,0.3) !important;
    }
    .stAlert {
        background: rgba(34, 197, 94, 0.08) !important;
        border: 1px solid rgba(34, 197, 94, 0.2) !important;
        border-radius: 10px !important; color: #22c55e !important;
    }
    .stSpinner > div { border-top-color: #c89b3c !important; }
    .metric-card {
        background: linear-gradient(145deg, #0d1221, #0a0e1a);
        padding: 20px 22px; border-radius: 12px; border: 1px solid #1a2235;
        box-shadow: 0 4px 24px rgba(0,0,0,0.4); transition: border-color 0.2s;
    }
    .metric-card:hover { border-color: #2a3a55; }
    .section-header {
        font-family: 'Rajdhani', sans-serif; font-size: 11px; font-weight: 600;
        letter-spacing: 3px; text-transform: uppercase; color: #4a5568;
        margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #1a2235;
    }
    .comp-card {
        background: linear-gradient(145deg, #0d1221, #0a0e1a);
        border: 1px solid #1a2235; border-radius: 12px;
        padding: 16px 20px; margin-bottom: 12px;
        transition: border-color 0.2s, transform 0.15s;
    }
    .comp-card:hover { border-color: #2a3a55; transform: translateY(-1px); }
    .comp-title {
        font-family: 'Rajdhani', sans-serif; font-size: 14px; font-weight: 700;
        letter-spacing: 2px; text-transform: uppercase; color: #c89b3c; margin-bottom: 12px;
    }
    .comp-score {
        font-family: 'Inter', sans-serif; font-size: 11px; color: #4a5568;
        margin-left: 8px; font-weight: 400; letter-spacing: 0; text-transform: none;
    }
    .unit-card {
        background: linear-gradient(145deg, #0d1221, #0a0e1a);
        border: 1px solid #1a2235; border-radius: 12px;
        padding: 16px 12px; text-align: center;
        transition: border-color 0.2s, transform 0.15s;
    }
    .unit-card:hover { border-color: #c89b3c; transform: translateY(-2px); }
    img { border-radius: 8px; transition: transform 0.15s ease; }
    img:hover { transform: scale(1.06); }
    .centered-label {
        text-align: center; font-size: 11px; margin-top: 6px; color: #8a9bb5;
        font-family: 'Inter', sans-serif; white-space: normal; overflow: visible;
        text-overflow: unset; word-break: break-word; width: 150%; line-height: 1.3;
    }
    .icon-container { display: flex; flex-direction: column; align-items: center; position: relative; cursor: default; }
    hr { border: none; border-top: 1px solid #1a2235; margin: 20px 0; }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #080c14; }
    ::-webkit-scrollbar-thumb { background: #1a2235; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #2a3a55; }
    .stSelectbox > div > div {
        background: #0d1221 !important; border: 1px solid #1e2d47 !important;
        border-radius: 8px !important; color: #c9d1e0 !important;
    }
    .stSelectbox > div > div > div { color: #c9d1e0 !important; font-family: 'Inter', sans-serif !important; font-size: 13px !important; }
    .stSelectbox svg { fill: #4a5568 !important; }
    .stSelectbox > div > div:focus-within { border-color: #c89b3c !important; box-shadow: 0 0 0 2px rgba(200,155,60,0.15) !important; }
    [data-baseweb="popover"], [data-baseweb="popover"] > div,
    [data-baseweb="select"] [role="listbox"], ul[data-baseweb="menu"], div[data-baseweb="menu"] {
        background-color: #0d1221 !important; border: 1px solid #1e2d47 !important; border-radius: 8px !important;
    }
    [data-baseweb="popover"] li, [data-baseweb="menu"] li, [role="option"], [data-baseweb="menu-item"] {
        background-color: #0d1221 !important; color: #c9d1e0 !important;
        font-family: 'Inter', sans-serif !important; font-size: 13px !important;
    }
    [data-baseweb="popover"] li:hover, [data-baseweb="menu"] li:hover,
    [role="option"]:hover, [data-baseweb="menu-item"]:hover {
        background-color: #1a2235 !important; color: #c89b3c !important; cursor: pointer !important;
    }
    [role="option"][aria-selected="true"], [data-baseweb="menu"] [aria-selected="true"] {
        background-color: #1a2235 !important; color: #c89b3c !important;
    }
    .stSelectbox label {
        color: #4a5568 !important; font-size: 11px !important;
        letter-spacing: 1.5px !important; text-transform: uppercase !important;
        font-family: 'Inter', sans-serif !important;
    }
    /* Error card */
    .error-card {
        background: rgba(239,68,68,0.06); border: 1px solid rgba(239,68,68,0.25);
        border-radius: 12px; padding: 20px 24px; margin: 16px 0;
    }
    [data-testid="stSidebarCollapseButton"] { display: none !important; }
    [data-testid="stSidebarNav"] { display: none !important; }
    [data-testid="stSidebarContent"] > div:first-child {
        visibility: hidden;
    }
}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
    <div style="margin-bottom: 32px;">
        <div style="font-family: 'Rajdhani', sans-serif; font-size: 22px; font-weight: 700;
                    color: #ffffff; letter-spacing: 2px;">♟ Chat<span style="color: #c89b3c;">TFT</span> </div><span style="font-size:11px;color:#4a5568;font-weight:400;letter-spacing:1px">v1.1 beta</span></div>
        <div style="font-size: 11px; color: #4a5568; letter-spacing: 1px; margin-top: 2px;">
            CHALLENGER META ENGINE
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown(
        '<div style="font-size:11px;letter-spacing:2px;color:#4a5568;'
        'text-transform:uppercase;margin-bottom:16px">Player Lookup</div>',
        unsafe_allow_html=True,
    )

    # ── CHANGED: empty defaults so placeholder text shows on first load ──
    game_name = st.text_input(
        "Game Name", "", label_visibility="collapsed", placeholder="Game Name"
    )
    st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
    tag_line = st.text_input(
        "Tag", "", label_visibility="collapsed", placeholder="Tag (e.g. NA1)"
    )
    st.markdown('<div style="height:12px"></div>', unsafe_allow_html=True)
    run = st.button("⚡  Analyze", use_container_width='stretch')

    st.markdown("""
    <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #1a2235;">
        <div style="font-size: 10px; color: #2a3a55; letter-spacing: 1px; line-height: 1.8;">
            PULLS LAST 50 GAMES<br>
            COMPARES VS TOP 300 CHALLENGERS<br>
            NA REGION · TFT SET 17
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)
    if st.session_state.get("analysis_data"):
        meta_ok    = bool(st.session_state["analysis_data"].get("meta_context", ""))
        meta_color = "#22c55e" if meta_ok else "#ef4444"
        meta_label = "✓ LIVE" if meta_ok else "✗ UNAVAILABLE"
        st.markdown(f"""
        <div style="font-size:10px;letter-spacing:1px;color:#2a3a55;margin-bottom:4px">META FEED</div>
        <div style="font-size:11px;color:{meta_color};font-family:'Rajdhani',sans-serif;
                    font-weight:700;letter-spacing:1px">{meta_label}</div>
        """, unsafe_allow_html=True)

    try:
        total_users, avg_improvement = get_player_stats()
        improvement_str   = f"+{avg_improvement*100:.0f}%" if avg_improvement and avg_improvement > 0 else "—"
        improvement_color = "#22c55e" if avg_improvement and avg_improvement > 0 else "#4a5568"
        st.markdown(f"""
        <div style="margin-top:20px;padding-top:20px;border-top:1px solid #1a2235;">
            <div style="font-size:12px;letter-spacing:2px;color:#2a3a55;margin-bottom:12px">COMMUNITY</div>
            <div style="font-size:26px;font-weight:700;color:#c89b3c;
                        font-family:'Rajdhani',sans-serif;line-height:1">{total_users}</div>
            <div style="font-size:10px;color:#4a5568;letter-spacing:1px;margin-bottom:12px">
                PLAYERS ANALYZED</div>
            <div style="font-size:26px;font-weight:700;color:{improvement_color};
                        font-family:'Rajdhani',sans-serif;line-height:1">{improvement_str}</div>
            <div style="font-size:10px;color:#4a5568;letter-spacing:1px">AVG TOP 4 IMPROVEMENT</div>
        </div>
        """, unsafe_allow_html=True)
    except Exception as e:
        st.error(f"Community Stats Error: {e}")

    st.markdown("<div style='flex:1'></div>", unsafe_allow_html=True)
    st.page_link("pages/terms.py", label="Terms & Privacy", icon="📄")

# ---------------------------------------------------------------------------
# HEADER
# ---------------------------------------------------------------------------
st.markdown("""
<div style="margin-bottom: 28px; padding-bottom: 20px; border-bottom: 1px solid #1a2235;">
    <div style="font-family: 'Rajdhani', sans-serif; font-size: 36px; font-weight: 700;
                color: #ffffff; letter-spacing: 1px; line-height: 1;">
        TFT Performance <span style="color: #c89b3c;">Analyzer</span>
    </div>
    <div style="font-size: 12px; color: #4a5568; letter-spacing: 2px; margin-top: 6px;
                text-transform: uppercase;">
        Challenger Benchmark · Unit Meta · Comp Analysis · Item Efficiency
    </div>
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# ERROR DISPLAY HELPER
# ---------------------------------------------------------------------------

def show_error(title: str, message: str, hint: str = ""):
    st.markdown(f"""
    <div class="error-card">
        <div style="font-family:'Rajdhani',sans-serif;font-size:16px;font-weight:700;
                    color:#ef4444;letter-spacing:1px;margin-bottom:8px">✗ {title}</div>
        <div style="font-size:13px;color:#c9d1e0;font-family:'Inter',sans-serif;
                    line-height:1.6;margin-bottom:{'8px' if hint else '0'}">{message}</div>
        {"<div style='font-size:12px;color:#4a5568;font-family:Inter,sans-serif'>" + hint + "</div>" if hint else ""}
    </div>
    """, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# LANDING SCREEN — shown only before first analysis
# ---------------------------------------------------------------------------
if st.session_state["analysis_data"] is None:
    st.components.v1.html("""
    <!DOCTYPE html>
    <html>
    <head>
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=Inter:wght@400;500&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: transparent; }
    </style>
    </head>
    <body>
    <div style="display:flex;flex-direction:column;align-items:center;
                justify-content:center;min-height:520px;text-align:center;
                padding:40px 20px;background:transparent">

        <svg width="120" height="120" viewBox="0 0 120 120"
             xmlns="http://www.w3.org/2000/svg"
             style="margin-bottom:28px;opacity:0.9">
            <defs>
                <linearGradient id="hexGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" style="stop-color:#c89b3c;stop-opacity:1"/>
                    <stop offset="100%" style="stop-color:#a07830;stop-opacity:1"/>
                </linearGradient>
                <linearGradient id="innerGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                    <stop offset="0%" style="stop-color:#0d1221;stop-opacity:1"/>
                    <stop offset="100%" style="stop-color:#1a2235;stop-opacity:1"/>
                </linearGradient>
            </defs>
            <polygon points="60,4 112,32 112,88 60,116 8,88 8,32"
                     fill="url(#hexGrad)" opacity="0.15"/>
            <polygon points="60,10 106,35 106,85 60,110 14,85 14,35"
                     fill="none" stroke="url(#hexGrad)" stroke-width="1.5" opacity="0.4"/>
            <polygon points="60,22 98,43 98,77 60,98 22,77 22,43"
                     fill="url(#innerGrad)" stroke="url(#hexGrad)" stroke-width="1.5"/>
            <text x="60" y="74" text-anchor="middle"
                  font-size="42" fill="url(#hexGrad)">&#9823;</text>
        </svg>

        <div style="font-family:'Rajdhani',sans-serif;font-size:42px;font-weight:700;
                    color:#ffffff;letter-spacing:2px;line-height:1;margin-bottom:8px">
            Chat<span style="color:#c89b3c">TFT</span>
        </div>
        <div style="font-size:12px;color:#4a5568;letter-spacing:3px;
                    text-transform:uppercase;margin-bottom:40px;
                    font-family:'Inter',sans-serif">
            Challenger Benchmark Engine &middot; NA Region
        </div>

        <div style="background:linear-gradient(145deg,#0d1221,#0a0e1a);
                    border:1px solid #1a2235;border-radius:16px;
                    padding:32px 40px;max-width:480px;width:100%;
                    box-shadow:0 8px 48px rgba(0,0,0,0.5)">

            <div style="font-family:'Rajdhani',sans-serif;font-size:13px;font-weight:600;
                        letter-spacing:3px;color:#c89b3c;text-transform:uppercase;
                        margin-bottom:20px">How To Get Started</div>

            <div style="display:flex;flex-direction:column;gap:14px;text-align:left">

                <div style="display:flex;align-items:flex-start;gap:14px">
                    <div style="background:#c89b3c;color:#080c14;font-family:'Rajdhani',sans-serif;
                                font-weight:700;font-size:12px;min-width:22px;height:22px;
                                border-radius:50%;display:flex;align-items:center;
                                justify-content:center;margin-top:1px;flex-shrink:0">1</div>
                    <div style="font-size:13px;color:#8a9bb5;font-family:'Inter',sans-serif;line-height:1.5">
                        Enter your <span style="color:#c9d1e0;font-weight:500">Riot Game Name</span>
                        in the sidebar &mdash; this is your in-game name
                    </div>
                </div>

                <div style="display:flex;align-items:flex-start;gap:14px">
                    <div style="background:#c89b3c;color:#080c14;font-family:'Rajdhani',sans-serif;
                                font-weight:700;font-size:12px;min-width:22px;height:22px;
                                border-radius:50%;display:flex;align-items:center;
                                justify-content:center;margin-top:1px;flex-shrink:0">2</div>
                    <div style="font-size:13px;color:#8a9bb5;font-family:'Inter',sans-serif;line-height:1.5">
                        Enter your <span style="color:#c9d1e0;font-weight:500">Tag Line</span>
                        &mdash; the code after # in your Riot ID
                        (e.g. <code style="background:#1a2235;padding:1px 5px;border-radius:3px;
                        font-size:12px;color:#c89b3c;font-family:monospace">NA1</code>)
                    </div>
                </div>

                <div style="display:flex;align-items:flex-start;gap:14px">
                    <div style="background:#c89b3c;color:#080c14;font-family:'Rajdhani',sans-serif;
                                font-weight:700;font-size:12px;min-width:22px;height:22px;
                                border-radius:50%;display:flex;align-items:center;
                                justify-content:center;margin-top:1px;flex-shrink:0">3</div>
                    <div style="font-size:13px;color:#8a9bb5;font-family:'Inter',sans-serif;line-height:1.5">
                        Click <span style="color:#c89b3c;font-weight:600">&#9889; Analyze</span>
                        &mdash; first-run analysis takes about 15&ndash;30 seconds
                    </div>
                </div>

            </div>

            <div style="margin-top:24px;padding-top:20px;border-top:1px solid #1a2235;
                        font-size:11px;color:#4a5568;font-family:'Inter',sans-serif;
                        line-height:1.7;text-align:left">
                This is an independent fan project and is not affiliated with, endorsed by,
                or sponsored by Riot Games. Teamfight Tactics and all related marks are
                property of Riot Games, Inc. AI-generated coaching insights are produced
                by a third-party language model and may contain errors or outdated
                information &mdash; provided for entertainment purposes only.
            </div>
            <div style="margin-top:12px;font-size:10px;color:#2a3a55;font-family:'Inter',sans-serif;
                        line-height:1.8;letter-spacing:0.5px">
                PULLS LAST 50 GAMES &middot; BENCHMARKS VS TOP 300 CHALLENGERS<br>
                NA REGION &middot; TFT SET 17 &middot; POWERED BY RIOT API + GROQ AI
            </div>
        </div>
    </div>
    </body>
    </html>
    """, height=820, scrolling=False)

# ---------------------------------------------------------------------------
# RUN ANALYSIS
# ---------------------------------------------------------------------------
if run:
    if not game_name.strip():
        show_error("Missing Input", "Please enter both a Game Name before analyzing.")
    else:
        resolved_tag = tag_line.strip() if tag_line.strip() else "NA1"
        with st.spinner("Pulling match data and crunching Challenger meta..."):
            try:
                data = run_analysis(game_name.strip(), resolved_tag)
                st.session_state["analysis_data"] = data
                st.session_state["ai_report"]        = None
                st.session_state["ai_report_player"] = None
                st.rerun()
            except ValueError as e:
                show_error("Player Not Found", str(e), "Double-check the spelling and make sure the tag is correct (e.g. NA1, EUW).")
            except RuntimeError as e:
                err = str(e)
                if "rate limit" in err.lower():
                    show_error("Rate Limited", err, "The Riot API allows a limited number of requests per second. Wait 60 seconds and try again.")
                else:
                    show_error("API Error", err)
            except requests.exceptions.ConnectionError:
                show_error("Connection Error", "Could not reach the Riot API. Check your internet connection and try again.")
            except requests.exceptions.Timeout:
                show_error("Request Timeout", "The Riot API took too long to respond. Try again in a moment.")
            except Exception as e:
                show_error("Unexpected Error", f"Something went wrong: {e}", "If this keeps happening, try refreshing the page.")

# ---------------------------------------------------------------------------
# MAIN DASHBOARD
# ---------------------------------------------------------------------------
if st.session_state["analysis_data"] is not None:
    data = st.session_state["analysis_data"]

    col_banner, col_refresh = st.columns([6, 1])
    with col_banner:
        st.markdown(f"""
        <div style="background:rgba(34,197,94,0.06);border:1px solid rgba(34,197,94,0.15);
                    border-radius:10px;padding:10px 18px;
                    font-family:'Rajdhani',sans-serif;font-size:13px;letter-spacing:2px;
                    color:#22c55e;text-transform:uppercase;">
            ✓ &nbsp; Analysis complete — {data['game_name']}#{data['tag_line']}
        </div>
        """, unsafe_allow_html=True)
    with col_refresh:
        if st.button("🔄 Refresh", use_container_width='stretch'):
            try:
                with st.spinner("Refreshing match data..."):
                    conn = get_conn()
                    cur  = conn.cursor()
                    cur.execute(
                        "SELECT puuid FROM players WHERE game_name=%s AND tag_line=%s",
                        (data["game_name"].lower(), data["tag_line"].lower()),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute("DELETE FROM player_matches WHERE puuid=%s", (row[0],))
                    conn.commit()
                    cur.close()
                    conn.close()

                    fresh_data = run_analysis(data["game_name"], data["tag_line"])
                st.session_state["analysis_data"]    = fresh_data
                st.session_state["ai_report"]        = None
                st.session_state["ai_report_player"] = None
                st.rerun()
            except Exception as e:
                show_error("Refresh Failed", str(e))

    TAB_NAMES = ["OVERVIEW", "UNITS", "COMPS", "ITEMS", "META GAPS", "AI INSIGHTS", "AI COACH"]
    tab0, tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(TAB_NAMES)

    # Tab persistence via query params
    st.components.v1.html("""
    <script>
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    async function trackTab() {
        await sleep(500);
        const tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
        tabs.forEach((tab, i) => {
            tab.addEventListener('click', () => {
                const url = new URL(window.parent.location);
                url.searchParams.set('active_tab', i);
                window.parent.history.replaceState({}, '', url);
            });
        });
    }
    async function restoreTab() {
        await sleep(300);
        const params = new URLSearchParams(window.parent.location.search);
        const activeTab = params.get('active_tab');
        if (activeTab !== null) {
            await sleep(200);
            const tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
            if (tabs[activeTab]) tabs[activeTab].click();
        }
    }
    restoreTab();
    trackTab();
    </script>
    """, height=0)

    # ── OVERVIEW ─────────────────────────────────────────────────────
    with tab0:
        history = data["match_history"]

        if not history:
            show_error("No Match History", "No match data was found for this player in the current set.", "Play some ranked TFT games and try again.")
        else:
            placements = [r["placement"] for r in history]
            levels     = [r["level"]     for r in history]
            golds      = [r["gold_left"] for r in history]
            game_nums  = list(range(1, len(placements) + 1))

            avg_placement = sum(placements) / len(placements)
            top4_rate     = sum(1 for p in placements if p <= 4) / len(placements)
            avg_level     = sum(levels) / len(levels)
            avg_gold      = sum(golds)  / len(golds)
            games         = len(placements)

            section_header("PERFORMANCE SUMMARY")
            m1, m2, m3, m4, m5 = st.columns(5)
            metric_card(m1, "Avg Placement",  f"{avg_placement:.2f}", f"{games} games analyzed")
            metric_card(m2, "Top 4 Rate",     f"{top4_rate*100:.0f}%", "win condition", accent=top4_rate >= 0.5)
            metric_card(m3, "Avg Level",      f"{avg_level:.1f}", "end-game level")
            metric_card(m4, "Avg Gold Left",  f"{avg_gold:.1f}g", "economy efficiency")
            metric_card(m5, "Games Sampled",  str(games), "current set only")

            st.markdown("<br>", unsafe_allow_html=True)

            section_header("PLACEMENT HISTORY")
            game_nums = list(range(1, len(placements) + 1))
            fig = go.Figure()
            fig.add_hrect(y0=0.5, y1=4.5, fillcolor="rgba(34,197,94,0.04)", line_width=0)
            fig.add_trace(go.Scatter(
                x=game_nums, y=placements,
                mode="lines+markers",
                line=dict(color="#c89b3c", width=2, shape="spline"),
                marker=dict(
                    size=9,
                    color=["#22c55e" if p <= 4 else "#ef4444" for p in placements],
                    line=dict(color="#080c14", width=2),
                ),
                hovertemplate="<b>Game %{x}</b><br>Placement: #%{y}<extra></extra>",
            ))
            fig.add_hline(
                y=avg_placement, line_dash="dot", line_color="rgba(200,155,60,0.3)",
                annotation_text=f"avg {avg_placement:.1f}",
                annotation_font_color="#c89b3c", annotation_position="right",
            )
            fig.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                yaxis=dict(
                    autorange="reversed", range=[8.5, 0.5],
                    gridcolor="rgba(255,255,255,0.04)",
                    tickfont=dict(color="#4a5568", family="Inter"),
                    tickvals=[1, 2, 3, 4, 5, 6, 7, 8], title="",
                ),
                xaxis=dict(gridcolor="rgba(255,255,255,0.04)",
                           tickfont=dict(color="#4a5568", family="Inter"), title=""),
                margin=dict(l=20, r=80, t=10, b=10), height=280, showlegend=False,
            )
            st.plotly_chart(fig, use_container_width='stretch', key="placement_chart")

            section_header("BOARD VALUE & ECON RATING")

            board_values = [r.get("board_value", 0) for r in history]
            econ_ratings = [r.get("econ", {}) for r in history]
            econ_colors  = [e.get("color", "#4a5568") for e in econ_ratings]
            econ_labels  = [e.get("rating", "?") for e in econ_ratings]

            avg_board_value = sum(board_values) / len(board_values) if board_values else 0

            # Econ summary cards
            e1, e2, e3 = st.columns(3)
            optimal_count = sum(1 for e in econ_ratings if e.get("rating") in ("Optimal", "Strong"))
            weak_count    = sum(1 for e in econ_ratings if e.get("rating") in ("Weak", "Poor"))
            metric_card(e1, "Avg Board Value",   f"{avg_board_value:.0f}g",  "sum of cost × star multiplier")
            metric_card(e2, "Strong Econ Games", str(optimal_count),          f"of {len(history)} games")
            metric_card(e3, "Weak Econ Games",   str(weak_count),             "hoarding or under-leveling")

            st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

            fig_bv = go.Figure()
            fig_bv.add_trace(go.Bar(
                x=game_nums,
                y=board_values,
                marker_color=econ_colors,
                marker_line_width=0,
                hovertemplate="<b>Game %{x}</b><br>Board Value: %{y}g<extra></extra>",
                text=econ_labels,
                textposition="outside",
                textfont=dict(size=9, color="#8a9bb5", family="Inter"),
            ))
            fig_bv.add_hline(
                y=avg_board_value,
                line_dash="dot",
                line_color="rgba(200,155,60,0.4)",
                annotation_text=f"avg {avg_board_value:.0f}g",
                annotation_font_color="#c89b3c",
                annotation_position="right",
            )
            fig_bv.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                height=220, margin=dict(l=20, r=80, t=24, b=10),
                xaxis=dict(gridcolor="rgba(0,0,0,0)",
                           tickfont=dict(color="#4a5568", family="Inter")),
                yaxis=dict(gridcolor="rgba(255,255,255,0.04)",
                           tickfont=dict(color="#4a5568", family="Inter"),
                           ticksuffix="g", title=""),
                showlegend=False,
            )
            st.plotly_chart(fig_bv, use_container_width='stretch')

            # Econ insight callouts
            all_insights = []
            for i, r in enumerate(history):
                for insight in r.get("econ", {}).get("insights", []):
                    all_insights.append(f"Game {i+1}: {insight}")

            if all_insights:
                insight_html = "".join([
                    f'<div style="padding:5px 0;border-bottom:1px solid #1a2235;'
                    f'font-size:12px;color:#8a9bb5;font-family:Inter,sans-serif">'
                    f'<span style="color:#c89b3c;font-family:Rajdhani,sans-serif;'
                    f'font-weight:700;margin-right:8px">{ins.split(":")[0]}:</span>'
                    f'{":".join(ins.split(":")[1:])}</div>'
                    for ins in all_insights[:6]
                ])
                st.markdown(f"""
                <div class="metric-card" style="margin-top:12px">
                    <div style="font-size:10px;letter-spacing:2px;color:#ef4444;
                                font-family:Inter,sans-serif;margin-bottom:10px;font-weight:600">
                        ⚠ ECON ISSUES DETECTED
                    </div>
                    {insight_html}
                </div>
                """, unsafe_allow_html=True)

            c1, c2 = st.columns(2)

            def sparkline(col, title, values, color, suffix=""):
                f = go.Figure(go.Scatter(
                    x=list(range(1, len(values) + 1)), y=values,
                    fill="tozeroy", mode="lines",
                    line=dict(color=color, width=2),
                    fillcolor=color.replace("rgb(", "rgba(").replace(")", ",0.1)"),
                    hovertemplate=f"%{{y}}{suffix}<extra></extra>",
                ))
                f.update_layout(
                    title=dict(text=title, font=dict(size=10, color="#4a5568", family="Inter"), x=0),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    height=160, margin=dict(l=10, r=10, t=30, b=10),
                    xaxis=dict(visible=False),
                    yaxis=dict(gridcolor="rgba(255,255,255,0.04)",
                               tickfont=dict(color="#4a5568", family="Inter", size=10)),
                    showlegend=False,
                )
                col.plotly_chart(f, use_container_width='stretch', key=f"sparkline_{title}")

            sparkline(c1, "LEVEL PER GAME",     levels, "rgb(99,102,241)")
            sparkline(c2, "GOLD LEFT PER GAME", golds,  "rgb(200,155,60)", "g")

            st.markdown("<br>", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            section_header("YOUR STRONGEST UNITS")
            top_units = data["your_units"].sort_values("score", ascending=False).head(10)
            if not top_units.empty:
                ucols = st.columns(10)
                for i, (_, row) in enumerate(top_units.iterrows()):
                    with ucols[i]:
                        item_icons = get_top_items_for_unit(row["unit"], data["unit_item_stats"])
                        st.markdown(
                            icon_html(get_unit_icon(row["unit"]), clean_unit_name(row["unit"]),
                                      55, item_icons=item_icons, unit_id=row["unit"]),
                            unsafe_allow_html=True,
                        )

            st.markdown("<br>", unsafe_allow_html=True)
            section_header("GAME DETAIL")

            selected_game = st.selectbox(
                "Select a game to inspect",
                options=list(range(1, len(placements) + 1)),
                format_func=lambda i: f"Game {i} — #{placements[i-1]} placement",
                label_visibility="collapsed",
                key="game_detail_select",
            )

            if selected_game:
                g         = history[selected_game - 1]
                placement = g["placement"]
                placement_color = "#22c55e" if placement <= 4 else "#ef4444"

                st.markdown(f"""
                <div class="metric-card" style="margin-bottom:16px;border-left:3px solid {placement_color}">
                    <div style="display:flex;gap:32px;flex-wrap:wrap">
                        <div>
                            <div style="font-size:10px;letter-spacing:2px;color:#4a5568;font-family:'Inter',sans-serif">PLACEMENT</div>
                            <div style="font-size:28px;font-weight:700;color:{placement_color};font-family:'Rajdhani',sans-serif">#{placement}</div>
                        </div>
                        <div>
                            <div style="font-size:10px;letter-spacing:2px;color:#4a5568;font-family:'Inter',sans-serif">LEVEL</div>
                            <div style="font-size:28px;font-weight:700;color:#f1f5f9;font-family:'Rajdhani',sans-serif">{g['level']}</div>
                        </div>
                        <div>
                            <div style="font-size:10px;letter-spacing:2px;color:#4a5568;font-family:'Inter',sans-serif">GOLD LEFT</div>
                            <div style="font-size:28px;font-weight:700;color:#c89b3c;font-family:'Rajdhani',sans-serif">{g['gold_left']}g</div>
                        </div>
                        <div>
                            <div style="font-size:10px;letter-spacing:2px;color:#4a5568;font-family:'Inter',sans-serif">BOARD VALUE</div>
                            <div style="font-size:28px;font-weight:700;color:#c89b3c;font-family:'Rajdhani',sans-serif">{g.get('board_value', 0)}g</div>
                        </div>
                        <div>
                            <div style="font-size:10px;letter-spacing:2px;color:#4a5568;font-family:'Inter',sans-serif">ECON</div>
                            <div style="font-size:20px;font-weight:700;font-family:'Rajdhani',sans-serif;color:{g.get('econ', {}).get('color', '#4a5568')}">{g.get('econ', {}).get('rating', '—')}</div>
                        </div>
                        <div>
                            <div style="font-size:10px;letter-spacing:2px;color:#4a5568;font-family:'Inter',sans-serif">UNITS</div>
                            <div style="font-size:28px;font-weight:700;color:#f1f5f9;font-family:'Rajdhani',sans-serif">{g['units']}</div>
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                traits = g.get("traits_detail", [])
                if traits:
                    TIER_COLORS = {1: "#7c5a3a", 2: "#6b7280", 3: "#a07830", 4: "#06b6d4"}
                    badges = ""
                    for t in sorted(traits, key=lambda x: x["num_units"], reverse=True)[:8]:
                        tier  = min(t.get("tier", 1), 4)
                        color = TIER_COLORS.get(tier, "#7c5a3a")
                        clean = clean_trait_name(t["name"])
                        badges += f"""
                        <div style="display:inline-flex;align-items:center;background:{color};
                                    border-radius:5px;padding:3px 8px;margin:2px;
                                    font-family:'Rajdhani',sans-serif;font-size:11px;
                                    font-weight:700;color:#fff;white-space:nowrap">
                            {clean} {t['num_units']}
                        </div>"""
                    st.markdown(f"""
                    <div style="margin-bottom:16px">
                        <div style="font-size:10px;letter-spacing:2px;color:#4a5568;
                                    font-family:'Inter',sans-serif;margin-bottom:8px">ACTIVE TRAITS</div>
                        <div>{badges}</div>
                    </div>
                    """, unsafe_allow_html=True)

                units_detail = g.get("units_detail", [])
                if units_detail:
                    st.markdown("""
                    <div style="font-size:10px;letter-spacing:2px;color:#4a5568;
                                font-family:'Inter',sans-serif;margin-bottom:8px">BOARD</div>
                    """, unsafe_allow_html=True)
                    cols = st.columns(min(len(units_detail), 10))
                    for i, unit in enumerate(units_detail[:10]):
                        with cols[i]:
                            item_icons = [get_item_icon(item) for item in unit["items"][:3]]
                            star_str   = "★" * unit.get("star", 1)
                            st.markdown(f"""
                            <div style="text-align:center">
                                <div style="font-size:9px;color:#c89b3c;font-family:'Rajdhani',sans-serif;
                                            margin-bottom:2px">{star_str}</div>
                                {icon_html(get_unit_icon(unit['id']), clean_unit_name(unit['id']),
                                           52, item_icons=item_icons, unit_id=unit['id'])}
                            </div>
                            """, unsafe_allow_html=True)

    # ── UNITS ────────────────────────────────────────────────────────
    with tab1:

        def render_units_by_cost(col, label, df, unit_item_df):
            with col:
                section_header(label)
                if df.empty:
                    st.markdown('<div style="color:#4a5568;font-size:13px">No data yet.</div>', unsafe_allow_html=True)
                    return

                df = df.copy()
                df["cost"] = df["unit"].apply(get_unit_cost)
                df = df.sort_values("score", ascending=False)

                for cost in [1, 2, 3, 4, 5]:
                    tier = df[df["cost"] == cost]
                    if tier.empty:
                        continue
                    color = COST_COLORS.get(cost, "#ffffff")
                    st.markdown(f"""
                    <div style="display:flex;align-items:center;gap:10px;margin:16px 0 8px 0">
                        <div style="background:{color};color:#080c14;font-family:'Rajdhani',sans-serif;
                                    font-size:11px;font-weight:700;padding:2px 9px;border-radius:4px;
                                    letter-spacing:1px">{cost} COST</div>
                        <div style="flex:1;height:1px;background:#1a2235"></div>
                    </div>
                    """, unsafe_allow_html=True)
                    cols = st.columns(min(len(tier), 6))
                    for i, (_, row) in enumerate(tier.head(6).iterrows()):
                        with cols[i]:
                            item_icons = get_top_items_for_unit(row["unit"], unit_item_df)
                            st.markdown(f"""
                            <div class="unit-card" style="border-color:{color}22">
                                <div style="width:6px;height:6px;border-radius:50%;
                                            background:{color};margin:0 auto 6px auto"></div>
                                {icon_html(get_unit_icon(row['unit']), clean_unit_name(row['unit']),
                                           54, item_icons=item_icons, unit_id=row['unit'])}
                                <div style="font-size:10px;color:#2a3a55;margin-top:6px;
                                            font-family:'Inter',sans-serif;text-align:center">
                                    {row['top4_rate']*100:.0f}% top4
                                </div>
                            </div>""", unsafe_allow_html=True)

        col1, col2 = st.columns(2, gap="large")
        render_units_by_cost(col1, "YOUR MOST PLAYED",  data["your_units"],        data["unit_item_stats"])
        render_units_by_cost(col2, "CHALLENGER META",   data["challenger_units"],  data["challenger_unit_item_stats"])

    # ── COMPS ────────────────────────────────────────────────────────
    with tab2:
        TIER_STYLES = {
            1: {"bg": "#7c5a3a", "border": "#a07850", "label": "bronze"},
            2: {"bg": "#6b7280", "border": "#9ca3af", "label": "silver"},
            3: {"bg": "#a07830", "border": "#c89b3c", "label": "gold"},
            4: {"bg": "#06b6d4", "border": "#67e8f9", "label": "prismatic"},
        }

        def trait_badge_html(trait):
            name      = trait.get("name", "")
            num_units = trait.get("num_units", 0)
            tier      = min(trait.get("tier", 1), 4)
            style     = TIER_STYLES.get(tier, TIER_STYLES[1])
            icon_url  = get_trait_icon(name)
            clean_name = clean_trait_name(name)
            icon_part = (
                f'<img src="{icon_url}" width="14" height="14" '
                f'style="border-radius:2px;filter:brightness(0) invert(1);'
                f'margin-right:3px;vertical-align:middle"/>'
                if icon_url else ""
            )
            return f"""
            <div style="display:inline-flex;align-items:center;
                        background:{style['bg']};border:1px solid {style['border']};
                        border-radius:5px;padding:3px 7px;margin:2px;
                        font-family:'Rajdhani',sans-serif;font-size:11px;
                        font-weight:700;color:#ffffff;white-space:nowrap">
                {icon_part}{clean_name} {num_units}
            </div>"""

        def render_comps(col, label, df, unit_item_df):
            with col:
                section_header(label)
                if df.empty:
                    st.markdown('<div style="color:#4a5568;font-size:13px">Not enough data yet.</div>', unsafe_allow_html=True)
                    return

                def render_comp_card(row):
                    score_pct    = f"{row['score']*100:.0f}%"
                    games_txt    = f"{int(row['games'])} games"
                    traits       = row.get("traits", [])
                    trait_badges = "".join(
                        trait_badge_html(t)
                        for t in sorted(traits, key=lambda x: x.get("num_units", 0), reverse=True)[:6]
                    )
                    st.markdown(f"""
                    <div class="comp-card">
                        <div style="display:flex;justify-content:space-between;
                                    align-items:flex-start;margin-bottom:10px">
                            <div>
                                <div class="comp-title">{row['comp_name']}</div>
                                <div class="comp-score">{score_pct} win · {games_txt}</div>
                            </div>
                            <div style="display:flex;flex-wrap:wrap;justify-content:flex-end;
                                        max-width:55%;gap:2px">{trait_badges}</div>
                        </div>
                    </div>""", unsafe_allow_html=True)

                    units = row.get("units", [])
                    if units:
                        cols = st.columns(len(units))
                        for i, unit in enumerate(units):
                            with cols[i]:
                                item_icons = get_top_items_for_unit(unit, unit_item_df)
                                st.markdown(
                                    icon_html(get_unit_icon(unit), clean_unit_name(unit),
                                              48, item_icons=item_icons, unit_id=unit),
                                    unsafe_allow_html=True,
                                )
                    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

                for _, row in df.head(5).iterrows():
                    render_comp_card(row)

                remaining = df.iloc[5:]
                if not remaining.empty:
                    with st.expander(f"Show {len(remaining)} more comps"):
                        remaining_sorted = remaining.sort_values("score", ascending=False)
                        table_rows = ""
                        for _, row in remaining_sorted.iterrows():
                            table_rows += f"""
                            <tr>
                                <td style="padding:6px 10px;color:#c9d1e0;font-family:'Inter',sans-serif;font-size:12px">{row['comp_name']}</td>
                                <td style="padding:6px 10px;color:#c89b3c;font-family:'Rajdhani',sans-serif;font-size:13px;font-weight:700;text-align:center">{row['score']*100:.0f}%</td>
                                <td style="padding:6px 10px;color:#4a5568;font-family:'Inter',sans-serif;font-size:11px;text-align:center">{int(row['games'])}g</td>
                            </tr>"""
                        st.markdown(f"""
                        <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
                            <thead>
                                <tr style="border-bottom:1px solid #1a2235">
                                    <th style="padding:6px 10px;color:#4a5568;font-size:10px;letter-spacing:2px;font-family:'Inter',sans-serif;text-align:left;font-weight:600">COMP</th>
                                    <th style="padding:6px 10px;color:#4a5568;font-size:10px;letter-spacing:2px;font-family:'Inter',sans-serif;text-align:center;font-weight:600">TOP4%</th>
                                    <th style="padding:6px 10px;color:#4a5568;font-size:10px;letter-spacing:2px;font-family:'Inter',sans-serif;text-align:center;font-weight:600">GAMES</th>
                                </tr>
                            </thead>
                            <tbody>{table_rows}</tbody>
                        </table>
                        """, unsafe_allow_html=True)
                        for _, row in remaining_sorted.iterrows():
                            render_comp_card(row)

        col1, col2 = st.columns(2, gap="large")
        render_comps(col1, "YOUR COMPS",       data["your_comps"],       data["unit_item_stats"])
        render_comps(col2, "CHALLENGER COMPS", data["challenger_comps"], data["challenger_unit_item_stats"])

    # ── ITEMS ────────────────────────────────────────────────────────
    with tab3:
        ITEM_TYPE_CONFIG = {
            "normal":   {"label": "STANDARD",  "color": "#8a9bb5"},
            "radiant":  {"label": "RADIANT",   "color": "#f59e0b"},
            "artifact": {"label": "ARTIFACT",  "color": "#a855f7"},
            "emblem":   {"label": "EMBLEM",    "color": "#22c55e"},
        }

        def render_items_by_type(col, label, df):
            with col:
                section_header(label)
                if df.empty:
                    st.markdown('<div style="color:#4a5568;font-size:13px">No data.</div>', unsafe_allow_html=True)
                    return
                for type_key, config in ITEM_TYPE_CONFIG.items():
                    tier = df[df["type"] == type_key].head(6)
                    if tier.empty:
                        continue
                    color      = config["color"]
                    type_label = config["label"]
                    st.markdown(f"""
                    <div style="display:flex;align-items:center;gap:10px;margin:16px 0 8px 0">
                        <div style="background:{color};color:#080c14;font-family:'Rajdhani',sans-serif;
                                    font-size:11px;font-weight:700;padding:2px 9px;border-radius:4px;
                                    letter-spacing:1px">{type_label}</div>
                        <div style="flex:1;height:1px;background:#1a2235"></div>
                    </div>
                    """, unsafe_allow_html=True)
                    cols = st.columns(min(len(tier), 6))
                    for i, (_, row) in enumerate(tier.iterrows()):
                        with cols[i]:
                            st.markdown(f"""
                            <div class="unit-card" style="border-color:{color}22">
                                <div style="width:6px;height:6px;border-radius:50%;
                                            background:{color};margin:0 auto 6px auto"></div>
                                {icon_html(get_item_icon(row['item']), get_item_name(row['item']), 50)}
                                <div style="font-size:10px;color:#2a3a55;margin-top:6px;
                                            font-family:'Inter',sans-serif;text-align:center">
                                    {row['top4_rate']*100:.0f}% top4
                                </div>
                            </div>""", unsafe_allow_html=True)

        col1, col2 = st.columns(2, gap="large")
        render_items_by_type(col1, "YOUR ITEMS",       data["your_items"])
        render_items_by_type(col2, "CHALLENGER ITEMS", data["challenger_items"])

    # ── META GAPS ────────────────────────────────────────────────────
    with tab4:
        section_header("META GAP ANALYSIS")

        your_units_df       = data["your_units"].copy()
        challenger_units_df = data["challenger_units"].copy()

        merged_units = your_units_df.merge(
            challenger_units_df, on="unit", how="outer", suffixes=("_you", "_challenger")
        ).fillna(0)

        merged_units["has_icon"] = merged_units["unit"].apply(
            lambda u: get_unit_icon(u) != "https://placehold.co/60x60/1a1a2e/white?text=?"
        )
        merged_units = merged_units[merged_units["has_icon"]]

        for col in ["top4_rate_you", "top4_rate_challenger", "score_you", "score_challenger"]:
            merged_units[col] = pd.to_numeric(merged_units.get(col, 0), errors="coerce").fillna(0)

        merged_units["gap"]  = merged_units["top4_rate_challenger"] - merged_units["top4_rate_you"]
        merged_units["name"] = merged_units["unit"].apply(clean_unit_name)
        merged_units = merged_units[merged_units["top4_rate_challenger"] > 0]
        merged_units = merged_units.sort_values("gap", ascending=False)

        your_items_df       = data["your_items"].copy()
        challenger_items_df = data["challenger_items"].copy()

        merged_items = your_items_df.merge(
            challenger_items_df, on="item", how="outer", suffixes=("_you", "_challenger")
        ).fillna(0)

        for col in ["top4_rate_you", "top4_rate_challenger", "score_you", "score_challenger"]:
            merged_items[col] = pd.to_numeric(merged_items.get(col, 0), errors="coerce").fillna(0)

        merged_items["gap"]  = merged_items["top4_rate_challenger"] - merged_items["top4_rate_you"]
        merged_items["name"] = merged_items["item"].apply(get_item_name)
        merged_items = merged_items[merged_items["top4_rate_challenger"] > 0]

        type_col     = "type_challenger" if "type_challenger" in merged_items.columns else "type"
        merged_items = merged_items[merged_items[type_col].isin(["normal"])]
        merged_items = merged_items.sort_values("gap", ascending=False)

        if merged_units.empty and merged_items.empty:
            st.markdown('<div style="color:#4a5568;font-size:13px">Not enough data to compute gaps yet.</div>', unsafe_allow_html=True)
        else:
            biggest_gap_unit  = merged_units.iloc[0]  if not merged_units.empty else None
            biggest_miss_unit = merged_units.iloc[-1] if not merged_units.empty else None

            if biggest_gap_unit is not None:
                g1, g2, g3, g4 = st.columns(4)
                metric_card(g1, "Biggest Opportunity",
                            clean_unit_name(biggest_gap_unit["unit"]),
                            f"+{biggest_gap_unit['gap']*100:.0f}% vs Challenger", accent=True)
                metric_card(g2, "Most Overplayed",
                            clean_unit_name(biggest_miss_unit["unit"]),
                            f"{biggest_miss_unit['gap']*100:.0f}% vs Challenger")
                play_more = merged_units[merged_units["gap"] > 0.15]
                play_less = merged_units[merged_units["gap"] < -0.15]
                metric_card(g3, "Units to Play More", str(len(play_more)), "above 15% gap vs Challenger")
                metric_card(g4, "Units to Play Less", str(len(play_less)), "underperforming vs Challenger")

            st.markdown("<br>", unsafe_allow_html=True)
            section_header("UNIT GAP HEATMAP — YOUR TOP4 RATE VS CHALLENGER")

            top_gap_units = pd.concat([
                merged_units.head(12), merged_units.tail(6)
            ]).drop_duplicates("unit").head(18)

            heatmap_data = pd.DataFrame({
                "Unit":       top_gap_units["name"].tolist(),
                "You":        (top_gap_units["top4_rate_you"] * 100).round(1).tolist(),
                "Challenger": (top_gap_units["top4_rate_challenger"] * 100).round(1).tolist(),
            }).set_index("Unit")

            fig_heat = go.Figure(data=go.Heatmap(
                z=[heatmap_data["You"].tolist(), heatmap_data["Challenger"].tolist()],
                x=heatmap_data.index.tolist(),
                y=["You", "Challenger"],
                colorscale=[[0.0, "#0d1221"], [0.35, "#1a3a55"], [0.65, "#c89b3c"], [1.0, "#22c55e"]],
                text=[[f"{v}%" for v in heatmap_data["You"].tolist()],
                      [f"{v}%" for v in heatmap_data["Challenger"].tolist()]],
                texttemplate="%{text}",
                textfont=dict(size=11, color="white", family="Inter"),
                hovertemplate="<b>%{x}</b><br>%{y}: %{z:.1f}%<extra></extra>",
                showscale=False,
            ))
            fig_heat.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                height=180, margin=dict(l=80, r=20, t=10, b=80),
                xaxis=dict(tickfont=dict(color="#8a9bb5", family="Inter", size=11), tickangle=-35),
                yaxis=dict(tickfont=dict(color="#8a9bb5", family="Inter", size=12)),
            )
            st.plotly_chart(fig_heat, use_container_width='stretch')

            section_header("UNIT GAPS — RANKED BY OPPORTUNITY")
            top_gaps   = merged_units.head(10)
            worst_gaps = merged_units.tail(8).iloc[::-1]
            bar_df     = pd.concat([top_gaps, worst_gaps]).drop_duplicates("unit")
            colors     = ["#22c55e" if g > 0 else "#ef4444" for g in bar_df["gap"]]

            fig_bar = go.Figure(go.Bar(
                x=bar_df["name"], y=(bar_df["gap"] * 100).round(1),
                marker_color=colors, marker_line_width=0,
                hovertemplate="<b>%{x}</b><br>Gap: %{y:.1f}%<extra></extra>",
                text=(bar_df["gap"] * 100).round(1).apply(lambda v: f"+{v:.0f}%" if v > 0 else f"{v:.0f}%"),
                textposition="outside",
                textfont=dict(color="#8a9bb5", size=10, family="Inter"),
            ))
            fig_bar.add_hline(y=0, line_color="rgba(255,255,255,0.1)", line_width=1)
            fig_bar.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                height=300, margin=dict(l=20, r=20, t=10, b=60),
                xaxis=dict(tickfont=dict(color="#8a9bb5", family="Inter", size=11),
                           tickangle=-35, gridcolor="rgba(0,0,0,0)"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.04)",
                           tickfont=dict(color="#4a5568", family="Inter", size=10),
                           ticksuffix="%", zeroline=False),
                showlegend=False,
            )
            st.plotly_chart(fig_bar, use_container_width='stretch')

            st.markdown("<br>", unsafe_allow_html=True)
            section_header("ITEM GAPS — RANKED BY OPPORTUNITY")

            item_top = merged_items.head(10)
            item_bot = merged_items.tail(6).iloc[::-1]
            item_bar = pd.concat([item_top, item_bot]).drop_duplicates("item")
            icolors  = ["#22c55e" if g > 0 else "#ef4444" for g in item_bar["gap"]]

            fig_items = go.Figure(go.Bar(
                x=item_bar["name"], y=(item_bar["gap"] * 100).round(1),
                marker_color=icolors, marker_line_width=0,
                hovertemplate="<b>%{x}</b><br>Gap: %{y:.1f}%<extra></extra>",
                text=(item_bar["gap"] * 100).round(1).apply(lambda v: f"+{v:.0f}%" if v > 0 else f"{v:.0f}%"),
                textposition="outside",
                textfont=dict(color="#8a9bb5", size=10, family="Inter"),
            ))
            fig_items.add_hline(y=0, line_color="rgba(255,255,255,0.1)", line_width=1)
            fig_items.update_layout(
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                height=300, margin=dict(l=20, r=20, t=10, b=80),
                xaxis=dict(tickfont=dict(color="#8a9bb5", family="Inter", size=11),
                           tickangle=-35, gridcolor="rgba(0,0,0,0)"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.04)",
                           tickfont=dict(color="#4a5568", family="Inter", size=10),
                           ticksuffix="%", zeroline=False),
                showlegend=False,
            )
            st.plotly_chart(fig_items, use_container_width='stretch')

            st.markdown("<br>", unsafe_allow_html=True)
            section_header("COACHING INSIGHTS")

            play_more = merged_units[merged_units["gap"] > 0.15].head(5)
            play_less = merged_units[merged_units["gap"] < -0.15].head(5)
            item_more = merged_items[merged_items["gap"] > 0.15].head(5)

            c1, c2, c3 = st.columns(3)

            def insight_card(col, title, color, rows, name_col, gap_col):
                items_html = "".join([
                    f'<div style="display:flex;justify-content:space-between;align-items:center;'
                    f'padding:7px 0;border-bottom:1px solid #1a2235;">'
                    f'<span style="font-size:13px;color:#c9d1e0;font-family:Inter,sans-serif">{row[name_col]}</span>'
                    f'<span style="font-size:12px;color:{color};font-family:Rajdhani,sans-serif;font-weight:700">'
                    f'{"+" if row[gap_col] > 0 else ""}{row[gap_col]*100:.0f}%</span>'
                    f'</div>'
                    for _, row in rows.iterrows()
                ]) if not rows.empty else \
                    '<div style="color:#4a5568;font-size:12px">No significant gaps</div>'

                col.markdown(f"""
                <div class="metric-card">
                    <div style="font-size:10px;letter-spacing:2px;color:{color};text-transform:uppercase;
                                font-family:Inter,sans-serif;margin-bottom:12px;font-weight:600">{title}</div>
                    {items_html}
                </div>
                """, unsafe_allow_html=True)

            insight_card(c1, "▲ Units to Play More", "#22c55e", play_more, "name", "gap")
            insight_card(c2, "▼ Units to Play Less", "#ef4444", play_less, "name", "gap")
            insight_card(c3, "▲ Items to Prioritize", "#c89b3c", item_more, "name", "gap")

    # ── AI INSIGHTS ──────────────────────────────────────────────────
    with tab5:
        section_header("AI COACHING REPORT")

        st.markdown("""
        <div style="font-size:12px;color:#4a5568;font-family:'Inter',sans-serif;
                    margin-bottom:20px;line-height:1.6">
            Personalized coaching report powered by Groq AI. Based on your stats vs Challenger meta.
        </div>
        """, unsafe_allow_html=True)

        # Recompute merged data for prompt
        _your_units_df = data["your_units"].copy()
        _ch_units_df   = data["challenger_units"].copy()
        _merged_units  = _your_units_df.merge(_ch_units_df, on="unit", how="outer", suffixes=("_you", "_challenger")).fillna(0)
        _merged_units["gap"]  = _merged_units["top4_rate_challenger"] - _merged_units["top4_rate_you"]
        _merged_units["name"] = _merged_units["unit"].apply(clean_unit_name)

        _your_items_df = data["your_items"].copy()
        _ch_items_df   = data["challenger_items"].copy()
        _merged_items  = _your_items_df.merge(_ch_items_df, on=["item", "type"], how="outer", suffixes=("_you", "_challenger")).fillna(0)
        _merged_items["gap"]  = _merged_items["top4_rate_challenger"] - _merged_items["top4_rate_you"]
        _merged_items["name"] = _merged_items["item"].apply(get_item_name)

        _history       = data["match_history"]
        _placements    = [r["placement"] for r in _history]
        _avg_placement = sum(_placements) / len(_placements) if _placements else 0
        _top4_rate     = sum(1 for p in _placements if p <= 4) / len(_placements) if _placements else 0
        _avg_level     = sum(r["level"] for r in _history) / len(_history) if _history else 0
        _avg_gold      = sum(r["gold_left"] for r in _history) / len(_history) if _history else 0

        _play_more    = _merged_units[_merged_units["gap"] > 0.15].sort_values("gap", ascending=False).head(5)
        _play_less    = _merged_units[_merged_units["gap"] < -0.15].sort_values("gap").head(5)
        _item_more    = _merged_items[_merged_items["gap"] > 0.15].sort_values("gap", ascending=False).head(5)
        _your_comps   = data["your_comps"]
        _chall_comps  = data["challenger_comps"]

        prompt = f"""You are an elite TFT (Teamfight Tactics) coach with deep knowledge of the current meta. You are analyzing real match data for a player and comparing them against Challenger-level players in NA. Your job is to give brutally honest, specific, actionable coaching — not generic advice.

PLAYER: {data['game_name']}#{data['tag_line']}
GAMES ANALYZED: {len(_placements)} games (current set only)

═══ PLAYER PERFORMANCE METRICS ═══
- Average Placement: {_avg_placement:.2f} (Challenger avg is typically 3.8)
- Top 4 Rate: {_top4_rate*100:.0f}% (Challenger players average 65%)
- Average Level: {_avg_level:.1f} (Challengers typically hit 8 by stage 4-2)
- Average Gold Left: {_avg_gold:.1f}g (high gold = not rolling enough or slow leveling)
- Win Rate Trend: {"above average" if _top4_rate > 0.5 else "below average"} — player is {"performing well but has room to optimize" if _top4_rate > 0.5 else "struggling to consistently top 4 and needs fundamental changes"}

═══ UNIT ANALYSIS ═══
Units this player plays that underperform vs Challenger meta:
{chr(10).join(f"• {row['name']}: player top4 rate {row['top4_rate_you']*100:.0f}% vs Challenger {row['top4_rate_challenger']*100:.0f}% (+{row['gap']*100:.0f}% gap)" for _, row in _play_more.iterrows()) if not _play_more.empty else "• No significant underperformed units identified"}

Units this player overplays relative to Challenger meta:
{chr(10).join(f"• {row['name']}: player top4 rate {row['top4_rate_you']*100:.0f}% vs Challenger {row['top4_rate_challenger']*100:.0f}% ({row['gap']*100:.0f}% gap)" for _, row in _play_less.iterrows()) if not _play_less.empty else "• No significantly overplayed units identified"}

═══ ITEM ANALYSIS ═══
Items with higher win rates in Challenger meta that this player underutilizes:
{chr(10).join(f"• {row['name']} ({row['type']} item): player top4 rate {row['top4_rate_you']*100:.0f}% vs Challenger {row['top4_rate_challenger']*100:.0f}% (+{row['gap']*100:.0f}% gap)" for _, row in _item_more.iterrows()) if not _item_more.empty else "• No significant item gaps identified"}

═══ COMP ANALYSIS ═══
Player's most played comps:
{chr(10).join(f"• {row['comp_name']}: {row['score']*100:.0f}% top4 rate over {int(row['games'])} games" for _, row in _your_comps.head(5).iterrows()) if not _your_comps.empty else "• No comp data available"}

Challenger meta comps:
{chr(10).join(f"• {row['comp_name']}: {row['score']*100:.0f}% top4 rate over {int(row['games'])} games" for _, row in _chall_comps.head(5).iterrows()) if not _chall_comps.empty else "• No challenger comp data available"}

Comp overlap: {", ".join(set(_your_comps["comp_name"].tolist()) & set(_chall_comps["comp_name"].tolist())) if not _your_comps.empty and not _chall_comps.empty else "none identified"}
Comps player plays that Challengers do NOT: {", ".join(set(_your_comps["comp_name"].tolist()) - set(_chall_comps["comp_name"].tolist())) if not _your_comps.empty and not _chall_comps.empty else "none identified"}

═══ ECONOMY & LEVELING ═══
- Gold Left: {f"{_avg_gold:.1f}g average — {'good economy management' if _avg_gold <= 2 else 'slow rolling issues or not spending efficiently' if _avg_gold <= 6 else 'very high — hoarding gold, not leveling or rolling at key breakpoints'}"}
- Level: {f"averaging {_avg_level:.1f} — {'hitting good level breakpoints' if _avg_level >= 7.5 else 'leveling too slow, missing 4-cost units' if _avg_level >= 6.5 else 'critically under-leveling'}"}
- Avg Board Value: {f"{sum(r.get('board_value', 0) for r in _history) / len(_history):.0f}g" if _history else "N/A"} (cost × star multiplier — Challenger boards typically 60-90g by level 8)
- Econ Ratings breakdown: {", ".join(f"{r.get('econ', {}).get('rating', '?')}" for r in _history[:10])} (last 10 games)
- Weak econ games: {sum(1 for r in _history if r.get('econ', {}).get('rating') in ('Weak', 'Poor'))} of {len(_history)} — games where board value, level, or gold spending was suboptimal
- Common econ issues: {"; ".join(set(i for r in _history for i in r.get('econ', {}).get('insights', [])))[:300] or "None detected"}

═══ COACHING INSTRUCTIONS ═══
Write a detailed personalized coaching report with EXACTLY these section headers on their own line:

PERFORMANCE SUMMARY
BIGGEST WEAKNESSES
UNIT PRIORITIES
ITEM STRATEGY
COMP RECOMMENDATIONS
ECONOMY & LEVELING
ONE KEY FOCUS

Rules: be brutally specific with actual unit/item/comp names and numbers. Use hyphens in round callouts (4-1 not 41). Each section 3-5 sentences minimum. Speak with authority, no "it seems". Do NOT give generic advice."""

        current_player  = f"{data['game_name']}#{data['tag_line']}"
        report_is_stale = st.session_state.get("ai_report_player") != current_player

        if st.session_state["ai_report"] is None or report_is_stale:
            st.markdown("""
            <div style="background:rgba(200,155,60,0.06);border:1px solid rgba(200,155,60,0.15);
                        border-radius:12px;padding:24px;text-align:center;margin:20px 0">
                <div style="font-family:'Rajdhani',sans-serif;font-size:18px;font-weight:700;
                            color:#c89b3c;letter-spacing:2px;margin-bottom:8px">AI COACHING REPORT READY</div>
                <div style="font-size:12px;color:#4a5568;font-family:'Inter',sans-serif;line-height:1.6">
                    Click below to generate your personalized report.<br>This uses Groq AI and takes ~10 seconds.
                </div>
            </div>
            """, unsafe_allow_html=True)

            if st.button("⚡  Generate My Coaching Report", use_container_width='stretch'):
                groq_key = os.getenv("GROQ_API_KEY")
                if not groq_key:
                    show_error("AI Unavailable", "GROQ_API_KEY is not configured.", "Add it to your environment variables or Streamlit secrets.")
                else:
                    with st.spinner("Analyzing your gameplay..."):
                        try:
                            groq_client = Groq(api_key=groq_key)
                            response = groq_client.chat.completions.create(
                                model="llama-3.3-70b-versatile",
                                messages=[{"role": "user", "content": prompt}],
                                max_tokens=2000,
                            )
                            st.session_state["ai_report"]        = response.choices[0].message.content
                            st.session_state["ai_report_player"] = current_player
                            st.rerun()
                        except Exception as e:
                            show_error("AI Report Failed", f"Could not generate report: {e}", "Check your GROQ_API_KEY and try again.")

        if st.session_state["ai_report"]:
            if st.button("🔄  Regenerate Report", use_container_width='stretch'):
                st.session_state["ai_report"]        = None
                st.session_state["ai_report_player"] = None
                st.rerun()

            if st.session_state.get("ai_report_player"):
                st.markdown(f"""
                <div style="font-size:11px;color:#4a5568;font-family:'Inter',sans-serif;
                            margin-bottom:16px;letter-spacing:1px;">
                    REPORT FOR: <span style="color:#c89b3c">{st.session_state["ai_report_player"]}</span>
                </div>
                """, unsafe_allow_html=True)

            report = st.session_state["ai_report"]
            sections = {
                "PERFORMANCE SUMMARY":  {"icon": "📊", "color": "#8a9bb5"},
                "BIGGEST WEAKNESSES":   {"icon": "⚠️", "color": "#ef4444"},
                "UNIT PRIORITIES":      {"icon": "♟",  "color": "#c89b3c"},
                "ITEM STRATEGY":        {"icon": "⚔️", "color": "#a855f7"},
                "COMP RECOMMENDATIONS": {"icon": "🎯", "color": "#3b82f6"},
                "ECONOMY & LEVELING":   {"icon": "💰", "color": "#f59e0b"},
                "ONE KEY FOCUS":        {"icon": "🔑", "color": "#22c55e"},
            }

            lines           = report.split("\n")
            current_section = None
            current_content = []
            parsed          = {}

            for line in lines:
                header_check = re.sub(r"[*#_=]", "", line).strip().upper()
                matched_section = next(
                    (s for s in sections if header_check == s or header_check.startswith(s)), None
                )
                if matched_section:
                    if current_section and current_content:
                        parsed[current_section] = "\n".join(current_content).strip()
                    current_section = matched_section
                    current_content = []
                elif current_section and line.strip():
                    clean = line.strip().lstrip("123456789. ").strip()
                    if clean:
                        current_content.append(clean)

            if current_section and current_content:
                parsed[current_section] = "\n".join(current_content).strip()

            if parsed:
                for section, config in sections.items():
                    content = parsed.get(section, "")
                    if not content:
                        continue
                    st.markdown(f"""
                    <div class="metric-card" style="margin-bottom:12px;border-left:3px solid {config['color']}">
                        <div style="font-size:10px;letter-spacing:2px;color:{config['color']};text-transform:uppercase;
                                    font-family:'Inter',sans-serif;font-weight:600;margin-bottom:10px">
                            {config['icon']} {section}
                        </div>
                        <div style="font-size:14px;color:#c9d1e0;font-family:'Inter',sans-serif;line-height:1.7">
                            {content}
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class="metric-card">
                    <div style="font-size:14px;color:#c9d1e0;font-family:'Inter',sans-serif;
                                line-height:1.7;white-space:pre-wrap">{report}</div>
                </div>
                """, unsafe_allow_html=True)

    # ── AI COACH CHAT ────────────────────────────────────────────────
    with tab6:
        section_header("AI COACH")

        st.markdown("""
        <div style="font-size:12px;color:#4a5568;font-family:'Inter',sans-serif;
                    margin-bottom:16px;line-height:1.6">
            Ask your coach anything about your gameplay, units, items, or the current meta.
            Full context of your stats is loaded automatically.
        </div>
        """, unsafe_allow_html=True)

        current_player = f"{data['game_name']}#{data['tag_line']}"
        if st.session_state["chat_player"] != current_player:
            st.session_state["chat_history"] = []
            st.session_state["chat_player"]  = current_player

        _history       = data["match_history"]
        _placements    = [r["placement"] for r in _history]
        _avg_placement = sum(_placements) / len(_placements) if _placements else 0
        _top4_rate     = sum(1 for p in _placements if p <= 4) / len(_placements) if _placements else 0
        _avg_level     = sum(r["level"] for r in _history) / len(_history) if _history else 0
        _avg_gold      = sum(r["gold_left"] for r in _history) / len(_history) if _history else 0

        _your_units_df = data["your_units"].copy()
        _ch_units_df   = data["challenger_units"].copy()
        _merged_units  = _your_units_df.merge(_ch_units_df, on="unit", how="outer", suffixes=("_you", "_challenger")).fillna(0)
        _merged_units["gap"]  = _merged_units["top4_rate_challenger"] - _merged_units["top4_rate_you"]
        _merged_units["name"] = _merged_units["unit"].apply(clean_unit_name)
        _play_more = _merged_units[_merged_units["gap"] > 0.15].sort_values("gap", ascending=False).head(5)

        chat_system_prompt = f"""You are an elite TFT (Teamfight Tactics) coach in a live coaching chat session.

PLAYER: {data['game_name']}#{data['tag_line']}
GAMES ANALYZED: {len(_placements)} games

PERFORMANCE:
- Avg Placement: {_avg_placement:.2f}
- Top 4 Rate: {_top4_rate*100:.0f}%
- Avg Level: {_avg_level:.1f}
- Avg Gold Left: {_avg_gold:.1f}g
- Avg Board Value: {f"{sum(r.get('board_value', 0) for r in _history) / len(_history):.0f}g" if _history else "N/A"} end-of-game (cost × star multiplier)
- Econ pattern: {sum(1 for r in _history if r.get('econ', {}).get('rating') in ('Optimal','Strong'))} strong / {sum(1 for r in _history if r.get('econ', {}).get('rating') in ('Weak','Poor'))} weak econ games out of {len(_history)}

TOP UNITS PLAYER PLAYS:
{chr(10).join(f"• {clean_unit_name(row['unit'])}: {row['top4_rate']*100:.0f}% top4 over {int(row['total_games'])} games" for _, row in data['your_units'].sort_values('score', ascending=False).head(8).iterrows())}

CHALLENGER META TOP UNITS:
{chr(10).join(f"• {clean_unit_name(row['unit'])}: {row['top4_rate']*100:.0f}% top4" for _, row in data['challenger_units'].sort_values('score', ascending=False).head(8).iterrows())}

PLAYER'S TOP COMPS:
{chr(10).join(f"• {row['comp_name']}: {row['score']*100:.0f}% top4 over {int(row['games'])} games" for _, row in data['your_comps'].head(5).iterrows()) if not data['your_comps'].empty else "No comp data"}

UNIT GAPS (opportunities where Challengers outperform this player):
{chr(10).join(f"• {row['name']}: +{row['gap']*100:.0f}% gap" for _, row in _play_more.iterrows()) if not _play_more.empty else "None significant"}

{f"CURRENT META CONTEXT:{chr(10)}{data['meta_context']}" if data.get("meta_context") else ""}

Be concise, specific, and direct. Use TFT terminology correctly. Reference the player's actual data when relevant. Use hyphens in round callouts (e.g. 4-1). Keep responses to 3-5 sentences unless a detailed breakdown is explicitly asked for."""

        # Render chat history
        for msg in st.session_state["chat_history"]:
            is_user       = msg["role"] == "user"
            bubble_bg     = "#0d1221" if is_user else "#111827"
            bubble_border = "#1e2d47" if is_user else "#c89b3c33"
            align         = "flex-end" if is_user else "flex-start"
            label         = data["game_name"] if is_user else "🤖 Coach"
            label_color   = "#4a5568" if is_user else "#c89b3c"

            st.markdown(f"""
            <div style="display:flex;flex-direction:column;align-items:{align};margin-bottom:12px">
                <div style="font-size:10px;color:{label_color};letter-spacing:1px;
                            margin-bottom:4px;font-family:'Inter',sans-serif">{label}</div>
                <div style="background:{bubble_bg};border:1px solid {bubble_border};
                            border-radius:12px;padding:10px 14px;max-width:80%;
                            font-size:13px;color:#c9d1e0;font-family:'Inter',sans-serif;line-height:1.6">
                    {msg["content"]}
                </div>
            </div>
            """, unsafe_allow_html=True)

        # Chat input
        with st.form(key=f"chat_form_{st.session_state['chat_input_counter']}", clear_on_submit=True):
            col_input, col_send = st.columns([5, 1])
            with col_input:
                user_input = st.text_input(
                    "chat_input",
                    placeholder="Ask your coach anything... (e.g. 'Why do I keep losing?' or 'How do I itemize Jinx?')",
                    label_visibility="collapsed",
                )
            with col_send:
                send = st.form_submit_button("Send ➤", use_container_width='stretch')

        if send and user_input.strip():
            groq_key = os.getenv("GROQ_API_KEY")
            if not groq_key:
                show_error("AI Unavailable", "GROQ_API_KEY is not configured.")
            else:
                st.session_state["chat_history"].append({"role": "user", "content": user_input.strip()})
                st.session_state["chat_input_counter"] += 1

                trimmed_history = st.session_state["chat_history"][-10:]
                groq_messages   = [{"role": "system", "content": chat_system_prompt}] + trimmed_history

                with st.spinner("Coach is thinking..."):
                    try:
                        groq_client = Groq(api_key=groq_key)

                        # Does this question need a web search?
                        search_check = groq_client.chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=groq_messages + [{
                                "role": "user",
                                "content": (
                                    "Before answering, decide: does this question require "
                                    "current TFT patch notes, tier lists, or meta info not "
                                    "already in your context? Reply ONLY with YES or NO."
                                ),
                            }],
                            max_tokens=5,
                        )
                        needs_search = "YES" in search_check.choices[0].message.content.upper()

                        web_context = ""
                        if needs_search:
                            query_resp = groq_client.chat.completions.create(
                                model="llama-3.3-70b-versatile",
                                messages=groq_messages + [{
                                    "role": "user",
                                    "content": (
                                        "Write a short Google search query (under 10 words) "
                                        "to find current TFT patch/meta info relevant to this question. "
                                        "Reply with ONLY the query, nothing else."
                                    ),
                                }],
                                max_tokens=30,
                            )
                            search_query = query_resp.choices[0].message.content.strip().strip('"')

                            serpapi_key = os.getenv("SERPAPI_KEY")
                            if serpapi_key:
                                search_resp = requests.get(
                                    "https://serpapi.com/search",
                                    params={"q": search_query, "api_key": serpapi_key, "num": 3, "engine": "google"},
                                    timeout=5,
                                )
                                if search_resp.status_code == 200:
                                    results  = search_resp.json().get("organic_results", [])
                                    snippets = [f"• {r.get('title','')}: {r.get('snippet','')}" for r in results[:3]]
                                    web_context = f"\n\nWEB SEARCH RESULTS for '{search_query}':\n" + "\n".join(snippets)
                            else:
                                web_context = "\n\nLIVE META CONTEXT:\n" + fetch_meta_context()

                        final_messages = groq_messages.copy()
                        if web_context:
                            final_messages[-1] = dict(final_messages[-1])
                            final_messages[-1]["content"] += web_context

                        response = groq_client.chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=final_messages,
                            max_tokens=600,
                        )
                        reply = response.choices[0].message.content
                        if needs_search and web_context:
                            reply += "\n\n*🔍 Answer informed by live web search.*"

                    except Exception as e:
                        reply = f"⚠️ Coach encountered an error: {e}"

                st.session_state["chat_history"].append({"role": "assistant", "content": reply})
                st.rerun()

        if st.session_state["chat_history"]:
            if st.button("🗑️ Clear Chat", use_container_width='content'):
                st.session_state["chat_history"]     = []
                st.session_state["chat_input_counter"] += 1
                st.rerun()