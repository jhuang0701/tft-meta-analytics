import requests
import os
from dotenv import load_dotenv
import pandas as pd
from collections import Counter, defaultdict
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from assets import get_unit_cost
from db import (
    get_cached_puuid, save_puuid,
    get_cached_match_ids, save_match_ids,
    get_cached_match, save_match,
    get_cached_challenger_list, save_challenger_list,
    get_cached_matches_batch,
    get_cached_match_ids_batch,
    CACHE_TTL_HOURS,
    get_conn,
)

# ---------------------------------------------------------------------------
# BLOCKLISTS
# ---------------------------------------------------------------------------

_ITEM_BLOCKLIST = [
    "emptybag", "empty_bag",
    "tactician", "tft_tactician", "tft_item_tacticiancrown",
    "tacticiancrown", "tacticians_crown", "tacticians",
    "crownofdemacia", "forceofnature", "spatula",
    "anime", "animasquaditem", "psyops_", "ekkooffering",
    "tft_item_support", "slammeditem", "dummy", "tutorial",
    "_component",
    "chainvest", "recurvebow", "bfsword", "giantsbelt",
    "negatroncloak", "needlesslylargerod", "tearofthegoddess",
    "sparringgloves", "cloakofagility", "iostone",
]

_UNIT_BLOCKLIST = [
    "tft_elderdragon", "tft17_elderdragon",
    "tft_pve", "tft17_pve",
    "tft_neutral",
    "bardfollower", "turret", "clone", "trap",
    "training", "monster", "dummyunit", "summon", "ivernminion",
]

_RADIANT_MARKERS  = ["radiant"]
_ARTIFACT_MARKERS = ["ornn", "artifact", "mogul"]
_EMBLEM_MARKERS   = ["emblem"]

# ---------------------------------------------------------------------------
# NAME MAPS
# ---------------------------------------------------------------------------

TRAIT_NAME_MAP = {
    "admin":                        "Arbiter",
    "aptrait":                      "Channeler",
    "astrait":                      "Challenger",
    "animasquad":                   "Anima Squad",
    "assassintrait":                "Rogue",
    "astronaut":                    "Meeple",
    "blitzcrankuniquetrait":        "Party Animal",
    "drx":                          "Divine Duelist",
    "darkstar":                     "Dark Star",
    "fateweaver":                   "Fateweaver",
    "fiorauniquetrait":             "Divine Duelist",
    "flextrait":                    "Voyager",
    "gravestrait":                  "Factory New",
    "hptank":                       "Brawler",
    "jhununiquetrait":              "Eradicator",
    "jhinuniquetrait":              "Eradicator",
    "manatrait":                    "Channeler",
    "mecha":                        "Mecha",
    "meleetrait":                   "Marauder",
    "missfortuneundeterminedtrait": "Gun Goddess",
    "missfortuneuniquetrait":       "Gun Goddess",
    "morganauniquetrait":           "Shepherd",
    "primordian":                   "Primordian",
    "psyops":                       "N.O.V.A.",
    "rangedtrait":                  "Sniper",
    "resisttank":                   "Bastion",
    "rhaastuniquetrait":            "Primordian",
    "shenuniquetrait":              "Stargazer",
    "shieldtank":                   "Vanguard",
    "sonauniquetrait":              "Commander",
    "spacegroove":                  "Space Groove",
    "stargazer_fountain":           "Stargazer",
    "stargazer_huntress":           "Stargazer",
    "stargazer_medallion":          "Stargazer",
    "stargazer_mountain":           "Stargazer",
    "stargazer_serpent":            "Stargazer",
    "stargazer_shield":             "Stargazer",
    "stargazer_wolf":               "Stargazer",
    "summontrait":                  "Shepherd",
    "tahmkenchuniquetrait":         "Brawler",
    "timebreaker":                  "Timebreaker",
    "vexuniquetrait":               "Replicator",
    "zeduniquetrait":               "Galaxy Hunter",
}

UNIT_NAME_MAP = {
    "galio": "The Mighty Mech",
}

# ---------------------------------------------------------------------------
# API CONFIG
# ---------------------------------------------------------------------------

MATCH_REGION    = "https://americas.api.riotgames.com"
PLATFORM_REGION = "https://na1.api.riotgames.com"

load_dotenv()
API_KEY = os.getenv("RIOT_API_KEY")
HEADERS = {"X-Riot-Token": API_KEY}

# ---------------------------------------------------------------------------
# RIOT API — CORE FETCHERS
# ---------------------------------------------------------------------------

def get_puuid(game_name: str, tag_line: str) -> str | None:
    cached = get_cached_puuid(game_name, tag_line)
    if cached:
        return cached

    url = f"{MATCH_REGION}/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    response = requests.get(url, headers=HEADERS)
    if response.status_code == 404:
        raise ValueError(f"Player '{game_name}#{tag_line}' not found. Check the name and tag.")
    if response.status_code == 429:
        raise RuntimeError("Riot API rate limit hit. Please wait a minute and try again.")
    if response.status_code != 200:
        raise RuntimeError(f"Riot API error {response.status_code}: {response.text}")

    puuid = response.json()["puuid"]
    save_puuid(game_name, tag_line, puuid)
    return puuid


def get_match_ids(puuid: str, count: int = 50, ttl_hours: int = CACHE_TTL_HOURS) -> list:
    cached = get_cached_match_ids(puuid, ttl_hours=ttl_hours)
    if cached:
        return cached[:count]

    url = f"{MATCH_REGION}/tft/match/v1/matches/by-puuid/{puuid}/ids?count={count}"
    time.sleep(0.2)
    response = requests.get(url, headers=HEADERS)
    if response.status_code == 429:
        raise RuntimeError("Riot API rate limit hit. Please wait a minute and try again.")
    if response.status_code != 200:
        return []

    match_ids = response.json()
    save_match_ids(puuid, match_ids)
    return match_ids


def get_match_objects(puuid: str, count: int = 50, ttl_hours: int = CACHE_TTL_HOURS) -> list:
    match_ids = get_match_ids(puuid, count=count, ttl_hours=ttl_hours)
    if not match_ids:
        return []

    cached_batch = get_cached_matches_batch(match_ids)
    raw_matches = list(cached_batch.values())
    uncached = [mid for mid in match_ids if mid not in cached_batch]

    def fetch_one(mid):
        url = f"{MATCH_REGION}/tft/match/v1/matches/{mid}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code == 429:
                raise RuntimeError("Riot API rate limit hit. Please wait a minute and try again.")
            if r.status_code == 200:
                return mid, r.json()
        except RuntimeError:
            raise
        except Exception:
            pass
        return mid, None

    if uncached:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fetch_one, mid): mid for mid in uncached}
            for future in as_completed(futures):
                mid, data = future.result()
                if data:
                    save_match(mid, data)
                    raw_matches.append(data)

    latest_set = detect_latest_set(raw_matches)
    return [m for m in raw_matches if m.get("info", {}).get("tft_set_number") == latest_set]


def get_top_players() -> list:
    cached = get_cached_challenger_list()
    if cached:
        return cached

    url = f"{PLATFORM_REGION}/tft/league/v1/challenger"
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 429:
        raise RuntimeError("Riot API rate limit hit. Please wait a minute and try again.")
    if res.status_code != 200:
        return []

    entries = res.json().get("entries", [])
    sorted_players = sorted(entries, key=lambda x: x["leaguePoints"], reverse=True)
    save_challenger_list(sorted_players)
    return sorted_players[:300]


# ---------------------------------------------------------------------------
# SET DETECTION
# ---------------------------------------------------------------------------

def detect_latest_set(matches: list) -> int | None:
    sets = [m.get("info", {}).get("tft_set_number") for m in matches]
    sets = [s for s in sets if s is not None]
    return max(sets) if sets else None


# ---------------------------------------------------------------------------
# STAT BUILDERS
# ---------------------------------------------------------------------------

def build_unit_stats(matches: list, min_games: int = 5) -> pd.DataFrame:
    unit_top4_counts  = defaultdict(int)
    unit_total_counts = defaultdict(int)

    for match in matches:
        for p in match["info"]["participants"]:
            placement = p.get("placement", 8)
            for unit in p.get("units", []):
                u = unit["character_id"]
                if any(blocked in u.lower() for blocked in _UNIT_BLOCKLIST):
                    continue
                unit_total_counts[u] += 1
                if placement <= 4:
                    unit_top4_counts[u] += 1

    stats = []
    for unit, total in unit_total_counts.items():
        if total < min_games:
            continue
        top4 = unit_top4_counts.get(unit, 0)
        stats.append({
            "unit":        unit,
            "total_games": total,
            "top4_games":  top4,
            "top4_rate":   top4 / total,
            "score":       (top4 + 1) / (total + 2),   # Laplace smoothing
        })

    df = pd.DataFrame(stats)
    if df.empty:
        return pd.DataFrame(columns=["unit", "total_games", "top4_games", "top4_rate", "score"])
    return df


def build_item_stats(matches: list, min_games: int = 5) -> pd.DataFrame:
    item_counts      = defaultdict(int)
    item_top4_counts = defaultdict(int)

    for match in matches:
        for p in match["info"]["participants"]:
            placement = p.get("placement", 8)
            for item in _extract_items(p):
                item_type = classify_item(item)
                if item_type is None:
                    continue
                item_counts[item] += 1
                if placement <= 4:
                    item_top4_counts[item] += 1

    stats = []
    for item, total in item_counts.items():
        if total < min_games:
            continue
        top4      = item_top4_counts.get(item, 0)
        item_type = classify_item(item)
        stats.append({
            "item":        item,
            "type":        item_type,
            "total_games": total,
            "top4_rate":   top4 / total,
            "score":       (top4 + 1) / (total + 2),
        })

    df = pd.DataFrame(stats)
    if df.empty:
        return pd.DataFrame(columns=["item", "type", "total_games", "top4_rate", "score"])
    return df.sort_values("score", ascending=False)


def build_unit_item_stats(matches: list) -> pd.DataFrame:
    unit_item_counts = defaultdict(lambda: defaultdict(int))
    unit_item_top4   = defaultdict(lambda: defaultdict(int))

    for match in matches:
        for p in match["info"]["participants"]:
            placement = p.get("placement", 8)
            for unit in p.get("units", []):
                unit_name = unit["character_id"]
                for item in unit.get("itemNames") or unit.get("items") or []:
                    item_type = classify_item(item)
                    if item_type not in ("normal", "artifact"):
                        continue
                    unit_item_counts[unit_name][item] += 1
                    if placement <= 4:
                        unit_item_top4[unit_name][item] += 1

    results = []
    for unit in unit_item_counts:
        for item, total in unit_item_counts[unit].items():
            if total < 2:
                continue
            top4 = unit_item_top4[unit].get(item, 0)
            results.append({
                "unit":      unit,
                "item":      item,
                "type":      classify_item(item),
                "total":     total,
                "top4_rate": top4 / total,
                "score":     (top4 + 1) / (total + 2),
            })
    return pd.DataFrame(results)


def build_comp_stats(matches: list, min_games: int = 2, puuid: str | None = None) -> pd.DataFrame:
    comp_counts       = defaultdict(int)
    comp_top4_counts  = defaultdict(int)
    comp_traits_list  = defaultdict(list)
    comp_unit_items   = defaultdict(list)
    comp_unit_lineups = defaultdict(list)

    for match in matches:
        for p in match["info"]["participants"]:
            if puuid and p.get("puuid") != puuid:
                continue
            comp      = _extract_comp(p)
            placement = p.get("placement", 8)
            traits    = get_active_traits(p)
            unit_items = get_units_with_items(p)
            units = [
                u["character_id"] for u in p.get("units", [])
                if not any(blocked in u["character_id"].lower() for blocked in _UNIT_BLOCKLIST)
            ]

            comp_counts[comp] += 1
            comp_traits_list[comp].append(traits)
            comp_unit_items[comp].append(unit_items)
            comp_unit_lineups[comp].append(tuple(sorted(units)[:10]))

            if placement <= 4:
                comp_top4_counts[comp] += 1

    stats = []
    for comp, total in comp_counts.items():
        if total < min_games:
            continue

        top4      = comp_top4_counts.get(comp, 0)
        top4_rate = top4 / total
        score     = (top4 + 1) / (total + 2)

        lineup_counter = Counter(comp_unit_lineups[comp])
        top_lineups    = [list(lu) for lu, _ in lineup_counter.most_common(3)]
        best_lineup    = sorted(
            max(top_lineups, key=len) if top_lineups else [],
            key=lambda u: get_unit_cost(u)
        )

        # top traits
        trait_counter_named = Counter()
        for traits in comp_traits_list[comp]:
            for t in traits:
                trait_counter_named[t["name"]] += 1
        most_common_traits = [name for name, _ in trait_counter_named.most_common(6)]

        best_traits = comp_traits_list[comp][0] if comp_traits_list[comp] else []

        # carriers
        unit_scores: dict = defaultdict(lambda: {"items": [], "stars": []})
        for game_units in comp_unit_items[comp]:
            for unit_id, items, star in game_units:
                if unit_id not in best_lineup:
                    continue
                unit_scores[unit_id]["items"].append(len(items))
                unit_scores[unit_id]["stars"].append(star)

        scored_units = []
        for unit_id, d in unit_scores.items():
            avg_items = sum(d["items"]) / len(d["items"])
            avg_stars = sum(d["stars"]) / len(d["stars"])
            cost      = get_unit_cost(unit_id)
            scored_units.append((avg_items, avg_stars, cost, unit_id))
        scored_units.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        top_carriers  = [u for _, _, _, u in scored_units[:2]]
        carrier_names = " · ".join(
            UNIT_NAME_MAP.get(
                re.sub(r"^TFT\d+_", "", u).replace("_", " ").title().lower(),
                re.sub(r"^TFT\d+_", "", u).replace("_", " ").title()
            )
            for u in top_carriers
        )

        # trait label
        trait_tiers: dict = defaultdict(list)
        for traits in comp_traits_list[comp]:
            for t in traits:
                tier = t.get("tier", 0)
                name = clean_trait_name(t["name"])
                if tier >= 2:
                    trait_tiers[name].append(tier)

        if "Primordian" in trait_tiers:
            trait_tiers["Primordian"] = [3] * len(trait_tiers["Primordian"])

        trait_scores = {
            name: (sum(tiers) / len(tiers)) * len(tiers)
            for name, tiers in trait_tiers.items()
        }
        top_traits = sorted(trait_scores, key=trait_scores.get, reverse=True)[:2]
        trait_str  = " · ".join(top_traits) if top_traits else "Flex"
        comp_name  = f"{carrier_names} · {trait_str}" if carrier_names else trait_str

        stats.append({
            "comp":       comp,
            "comp_name":  comp_name,
            "games":      total,
            "top4_rate":  top4_rate,
            "score":      score,
            "traits":     best_traits,
            "top_traits": most_common_traits,
            "units":      best_lineup,
        })

    df = pd.DataFrame(stats)
    if df.empty:
        return pd.DataFrame(columns=["comp", "comp_name", "games", "top4_rate", "score", "traits", "top_traits", "units"])
    return df.sort_values("games", ascending=False)


def build_match_history(matches: list, puuid: str) -> list:
    rows = []
    for match in matches:
        for p in match["info"]["participants"]:
            if p.get("puuid") != puuid:
                continue

            units_detail = []
            for unit in p.get("units", []):
                uid = unit.get("character_id", "")
                if any(blocked in uid.lower() for blocked in _UNIT_BLOCKLIST):
                    continue
                items = unit.get("itemNames") or unit.get("items") or []
                units_detail.append({
                    "id":    uid,
                    "star":  unit.get("tier", 1),
                    "items": [i for i in items if i],
                })

            board_value_data = calculate_board_value(units_detail)
            econ_data = evaluate_econ(
                board_value_data["total"],
                p.get("level", 0),
                p.get("gold_left", 0),
                last_round=p.get("last_round", 0),
                placement=p.get("placement", 8),  
            )

            rows.append({
                "placement":     p.get("placement", 8),
                "level":         p.get("level", 0),
                "gold_left":     p.get("gold_left", 0),
                "units":         len(units_detail),
                "units_detail":  units_detail,
                "traits_detail": get_active_traits(p),
                "board_value":   board_value_data["total"],
                "econ":          econ_data,
            })
    return rows

# ---------------------------------------------------------------------------
# ECON EVALUATION
# ---------------------------------------------------------------------------

_STAGE_BENCHMARKS = {
    #  min           good
    2: dict(min=16,  good=28),   
    3: dict(min=38,  good=60),  
    4: dict(min=75,  good=110),  
    5: dict(min=110, good=150),  
    6: dict(min=145, good=190),  
}

_LEVEL_GOLD_COST = {
    1: 0, 2: 0, 3: 2, 4: 6, 5: 10,
    6: 20, 7: 36, 8: 60, 9: 68, 10:68
}

def calculate_board_value(units_detail: list) -> dict:
    STAR_MULTIPLIERS = {1: 1, 2: 3, 3: 9}
    total_value = 0
    breakdown = []

    for unit in units_detail:
        cost = get_unit_cost(unit["id"])
        if cost == 0:
            continue
        star = unit.get("star", 1)
        multiplier = STAR_MULTIPLIERS.get(star, 1)
        unit_value = cost * multiplier
        total_value += unit_value
        breakdown.append({
            "id": unit["id"],
            "cost": cost,
            "star": star,
            "value": unit_value,
        })

    return {
        "total": total_value,
        "breakdown": breakdown,
    }

def _last_round_to_stage(last_round: int) -> int:
    """Convert Riot's flat round number to a TFT stage (1-6)."""
    if last_round <= 4:   return 1
    if last_round <= 11:  return 2
    if last_round <= 18:  return 3
    if last_round <= 25:  return 4
    if last_round <= 32:  return 5
    return 6

def evaluate_econ(
    board_value:  int,
    level:        int,
    gold_left:    int,
    last_round:   int = 0,
    placement:    int = 8,
) -> dict:
    stage = _last_round_to_stage(last_round) if last_round else max(2, min(6, level - 3))
    bench = _STAGE_BENCHMARKS.get(stage, _STAGE_BENCHMARKS[5])

    level_cost      = _LEVEL_GOLD_COST.get(level, 0)
    total_invested  = board_value + level_cost
    insights        = []

    # Total gold invested vs stage 
    if total_invested >= bench["good"]:
        invest_rating = "strong"
    elif total_invested >= bench["min"]:
        invest_rating = "healthy"
    else:
        invest_rating = "weak"
        insights.append(
            f"Only {total_invested}g invested by stage {stage} "
            f"({board_value}g board + {level_cost}g leveling) — "
            f"expected ≥{bench['min']}g"
        )

    # Gold left — skip for 1st place 
    if placement == 1:
        gold_rating = "exempt"  
    elif gold_left > 20:
        gold_rating = "hoarding"
        insights.append(
            f"Left {gold_left}g unspent — significant gold not converted into board power"
        )
    elif gold_left > 10:
        gold_rating = "conservative"
        insights.append(f"Left {gold_left}g unspent — could have rolled or leveled more")
    elif gold_left <= 2:
        gold_rating = "spent"
        if invest_rating == "weak":
            insights.append(
                f"Spent all gold but total investment still low — "
                f"may have rolled too early or over-leveled on a weak board"
            )
    else:
        gold_rating = "healthy"

    # ── Overall score ─────────────────────────────────────────────────────────
    _scores = {
        "strong": 2, "healthy": 1, "spent": 1,
        "exempt": 1,                               
        "conservative": 0, "weak": -1, "hoarding": -1,
    }
    total_score = _scores.get(invest_rating, 0) + _scores.get(gold_rating, 0)

    if total_score >= 3:
        rating, color = "Optimal", "#22c55e"
    elif total_score >= 2:
        rating, color = "Strong",  "#86efac"
    elif total_score >= 1:
        rating, color = "Healthy", "#c89b3c"
    elif total_score >= 0:
        rating, color = "Weak",    "#f97316"
    else:
        rating, color = "Poor",    "#ef4444"

    return {
        "rating":        rating,
        "color":         color,
        "score":         total_score,
        "stage":         stage,
        "total_invested": total_invested,
        "board_value":   board_value,
        "level_cost":    level_cost,
        "invest_rating": invest_rating,
        "gold_rating":   gold_rating,
        "insights":      insights,
    }

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def get_active_traits(participant: dict) -> list:
    traits = []
    for t in participant.get("traits", []):
        num_units    = t.get("num_units", 0)
        tier_current = t.get("tier_current", 0)
        if tier_current > 0 and num_units >= 2:
            traits.append({
                "name":       t.get("name", ""),
                "num_units":  num_units,
                "tier":       tier_current,
                "tier_total": t.get("tier_total", 3),
            })
    return sorted(traits, key=lambda x: x["num_units"], reverse=True)


def get_units_with_items(participant: dict) -> list:
    result = []
    for unit in participant.get("units", []):
        unit_id = unit.get("character_id", "")
        if any(blocked in unit_id.lower() for blocked in _UNIT_BLOCKLIST):
            continue
        items = unit.get("itemNames") or unit.get("items") or []
        items = [i for i in items if classify_item(i) is not None]
        result.append((unit_id, items, unit.get("tier", 1)))
    return result


def classify_item(item_name: str) -> str | None:
    lowered = item_name.lower()
    if _is_blocked(lowered):
        return None
    if any(m in lowered for m in _RADIANT_MARKERS):
        return "radiant"
    if any(m in lowered for m in _ARTIFACT_MARKERS):
        return "artifact"
    if any(m in lowered for m in _EMBLEM_MARKERS):
        return "emblem"
    return "normal"


def clean_trait_name(raw: str) -> str:
    lowered  = raw.lower()
    stripped = re.sub(r"^tft\d+_", "", lowered)
    if stripped in TRAIT_NAME_MAP:
        return TRAIT_NAME_MAP[stripped]
    for key, display in TRAIT_NAME_MAP.items():
        if key in stripped:
            return display
    return raw


def fetch_meta_context() -> str:
    """Meta context is now built from live challenger data already pulled."""
    return "" 

def get_player_stats():
    conn = get_conn()
    c    = conn.cursor()
    c.execute("SELECT COUNT(DISTINCT puuid) FROM players")
    total_users = c.fetchone()[0]
    c.execute("SELECT AVG(latest_top4_rate) FROM player_performance")
    avg_top4 = c.fetchone()[0]
    c.close()
    conn.close()
    return total_users, avg_top4

# ---------------------------------------------------------------------------
# PRIVATE HELPERS
# ---------------------------------------------------------------------------

def _extract_items(participant: dict) -> list:
    items = []
    for unit in participant.get("units", []):
        unit_items = unit.get("items") or unit.get("itemNames") or []
        items.extend(unit_items)
    return items


def _extract_comp(participant: dict) -> str:
    return _get_dominant_trait(participant)


def _get_dominant_trait(participant: dict) -> str:
    traits = participant.get("traits", [])
    active = [t for t in traits if t.get("num_units", 0) >= 2 and t.get("style", 0) >= 2]
    if not active:
        return "Flex"
    dominant  = max(active, key=lambda t: (t.get("style", 0), t["num_units"]))
    raw_name  = dominant["name"]
    cleaned   = clean_trait_name(raw_name)
    return cleaned if cleaned != raw_name else re.sub(r"^TFT\d+_", "", raw_name)


def _is_blocked(item_name: str) -> bool:
    lowered = item_name.lower()
    return any(bad in lowered for bad in _ITEM_BLOCKLIST)