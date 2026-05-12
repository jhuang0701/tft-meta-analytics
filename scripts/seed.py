import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts'))



import requests
import time
from dotenv import load_dotenv
from backend import get_match_ids, detect_latest_set
from db import get_cached_matches_batch, save_match, save_player_performance, save_puuid
load_dotenv("/Users/jonathan/tft-analytics/.env")
API_KEY = os.getenv("RIOT_API_KEY")
HEADERS = {"X-Riot-Token": API_KEY}
PLATFORM = "https://na1.api.riotgames.com"
MATCH_REGION = "https://americas.api.riotgames.com"

def seed():
    r = requests.get(f"{PLATFORM}/tft/league/v1/master", headers=HEADERS)
    if r.status_code != 200:
        print(f"Failed to fetch master ladder: {r.status_code}")
        return

    entries = sorted(r.json().get("entries", []), key=lambda x: x["leaguePoints"])[:200]
    print(f"Seeding {len(entries)} lowest Master players...")
    print(f"Sample entry: {entries[0]}")

    for i, player in enumerate(entries):
        puuid = player.get("puuid")
        print(f"[{i+1}] puuid: {puuid}")
        if not puuid:
            print("  → skipped: no puuid")
            continue

        # Get Riot ID from PUUID
        r3 = requests.get(f"{MATCH_REGION}/riot/account/v1/accounts/by-puuid/{puuid}", headers=HEADERS)
        print(f"  → account status: {r3.status_code}")
        if r3.status_code != 200:
            print(f"  → skipped: {r3.text}")
            continue
        account   = r3.json()
        game_name = account.get("gameName")
        tag_line  = account.get("tagLine", "NA1")
        print(f"  → {game_name}#{tag_line}")
        if not game_name:
            print("  → skipped: no game_name")
            continue
        time.sleep(0.5)

        # Save PUUID
        save_puuid(game_name, tag_line, puuid)

        # Get match IDs
        match_ids = get_match_ids(puuid, count=20)
        print(f"  → match_ids: {len(match_ids)}")
        if not match_ids:
            print("  → skipped: no match_ids")
            continue
        time.sleep(0.5)

        # Fetch uncached matches
        cached  = get_cached_matches_batch(match_ids)
        uncached = [mid for mid in match_ids if mid not in cached]
        print(f"  → cached: {len(cached)}, uncached: {len(uncached)}")

        for mid in uncached:
            r4 = requests.get(f"{MATCH_REGION}/tft/match/v1/matches/{mid}", headers=HEADERS)
            if r4.status_code == 429:
                print("  → rate limited, sleeping 60s...")
                time.sleep(60)
                continue
            if r4.status_code == 200:
                data = r4.json()
                save_match(mid, data)
                cached[mid] = data
            time.sleep(0.3)

        # Calculate placements
        matches    = [m for m in cached.values() if m]
        latest_set = detect_latest_set(matches)
        matches    = [m for m in matches if m.get("info", {}).get("tft_set_number") == latest_set]

        placements = []
        for match in matches:
            for p in match.get("info", {}).get("participants", []):
                if p.get("puuid") == puuid:
                    placements.append(p.get("placement", 8))

        print(f"  → placements: {len(placements)}")
        if not placements:
            print("  → skipped: no placements")
            continue

        top4_rate = sum(1 for p in placements if p <= 4) / len(placements)
        save_player_performance(puuid, game_name, tag_line, top4_rate, len(placements))
        print(f"  → saved! {game_name}#{tag_line} — {top4_rate*100:.0f}% top4 over {len(placements)} games")
        time.sleep(0.5)

    print("Seeding complete!")

if __name__ == "__main__":
    seed()