import requests
import streamlit as st
import re
from db import get_cached_cdragon, save_cdragon
import base64
from pathlib import Path

# ----------------------------
# CONSTANTS
# ----------------------------
CD_BASE = "https://raw.communitydragon.org/latest/"
JSON_URL = f"{CD_BASE}cdragon/tft/en_us.json"
PLUGIN_ROOT = f"{CD_BASE}plugins/rcp-be-lol-game-data/global/default/"

PLACEHOLDER_UNIT = "https://placehold.co/60x60/1a1a2e/white?text=?"
PLACEHOLDER_ITEM = "https://placehold.co/50x50/1a1a2e/white?text=?"

def _img_to_base64(path: str) -> str:
    try:
        data = Path(path).read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        print(f"[IMG] Failed to load {path}: {e}")
        return PLACEHOLDER_ITEM

ITEM_ICON_OVERRIDES = {
    "TFT5_Item_RunaansHurricaneRadiant": _img_to_base64("static/items/tft5_item_runaanshurricaneradiant.png"),
    "TFT5_Item_RedemptionRadiant": _img_to_base64("static/items/tft5_item_redemptionradiant.png"),
}

UNIT_ICON_OVERRIDES = {
    "tft17_diana": _img_to_base64("static/units/tft17_diana.png"),
}

# ----------------------------
# DATA INITIALIZATION
# ----------------------------
@st.cache_data(show_spinner=False)
def load_maps():
    try:
        cached_data = get_cached_cdragon()
        if cached_data:
            data = cached_data
        else:
            res = requests.get(JSON_URL, timeout=15)
            res.raise_for_status()
            data = res.json()
            save_cdragon(data)
    except Exception as e:
        st.warning(f"Failed to load CDragon data: {e}")
        return {}, {}, {}, {}, {}
    
    unit_map = {}
    item_map = {}
    item_name_map = {}
    unit_cost_map = {}
    trait_icon_map = {}

    def clean_path(raw_path):
        if not raw_path:
            return None
        path = raw_path.lower().strip()
        prefix = "/lol-game-data/assets/"
        if prefix in path:
            path = path.split(prefix, 1)[1]
        else:
            path = path.lstrip("/")
        if path.endswith(".tex"):
            path = path[:-4] + ".png"
        return PLUGIN_ROOT + path

    # --- Units ---
    for set_data in data.get("setData", []):
        for champ in set_data.get("champions", []):
            api_name  = champ.get("apiName", "")
            name      = champ.get("name", "")
            icon_path = champ.get("tileIcon") or champ.get("squareIcon")
            icon_url  = clean_path(icon_path)
            cost      = champ.get("cost", 0)

            if not icon_url:
                continue
            if api_name:
                unit_map[api_name.lower()]      = icon_url
                unit_cost_map[api_name.lower()] = cost
            if name:
                unit_map[name.lower()] = icon_url

    # --- Items ---
    for item in data.get("items", []):
        api_name = item.get("apiName", "")
        name     = item.get("name", "")
        icon_url = clean_path(item.get("icon"))

        if not icon_url:
            continue
        if api_name:
            item_map[api_name.lower()] = icon_url
            if name:
                item_name_map[api_name.lower()] = name
        if name:
            item_map[name.lower()] = icon_url

    for set_data in data.get("setData", []):
        for trait in set_data.get("traits", []):
            api_name = trait.get("apiName", "")
            name     = trait.get("name", "")
            icon_url = clean_path(trait.get("icon"))
            if not icon_url:
                continue
            if api_name:
                trait_icon_map[api_name.lower()] = icon_url
            if name:
                trait_icon_map[name.lower()] = icon_url

    return unit_map, item_map, item_name_map, unit_cost_map, trait_icon_map

# ----------------------------
# PUBLIC API
# ----------------------------
def _get_maps():
    return load_maps()


def get_unit_icon(unit_id: str) -> str:
    if not unit_id:
        return PLACEHOLDER_UNIT
    if unit_id.lower() in UNIT_ICON_OVERRIDES:
        return UNIT_ICON_OVERRIDES[unit_id.lower()]
    unit_map, _, _, _, _ = _get_maps()
    return unit_map.get(unit_id.lower(), PLACEHOLDER_UNIT)


def get_unit_cost(unit_id: str) -> int:
    if not unit_id:
        return 0
    _, _, _, unit_cost_map, _ = _get_maps()
    return unit_cost_map.get(unit_id.lower(), 0)

def get_item_icon(item_id: str) -> str:
    # Check overrides case-insensitively
    for key, path in ITEM_ICON_OVERRIDES.items():
        if key.lower() == item_id.lower():
            return path
    if not item_id:
        return PLACEHOLDER_ITEM
    _, item_map, _, _, _ = _get_maps()
    return item_map.get(item_id.lower(), PLACEHOLDER_ITEM)

def get_item_name(item_id: str) -> str:
    if not item_id:
        return ""
    _, _, item_name_map, _, _ = _get_maps()
    name = item_name_map.get(item_id.lower())
    if name:
        return name
    n = re.sub(r"^TFT_Item_", "", item_id)
    n = re.sub(r"^TFT\d+_[^_]+_", "", n)
    n = re.sub(r"^TFT\d+_", "", n)
    n = re.sub(r"^TFT_", "", n)
    n = re.sub(r"([A-Z])", r" \1", n).strip()
    return n.title()

def get_trait_icon(trait_name: str) -> str:
    if not trait_name:
        return ""
    _, _, _, _, trait_icon_map = _get_maps()
    return trait_icon_map.get(trait_name.lower(), "")


