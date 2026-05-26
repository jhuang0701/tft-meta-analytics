from __future__ import annotations
import json
import os
import random
from datetime import datetime, timezone
from typing import Iterator
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import time
import re
 
load_dotenv()
DB_URL = os.environ["DATABASE_URL"]
 
# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
 
MIN_GAME_LENGTH   = 1200.0    
MAX_GAME_LENGTH   = 3600.0   
MIN_RESPONSE_WORDS = 40     
TOP_K_PLACEMENT   = 4        
 
SYSTEM_PROMPT = (
    "You are an expert TFT (Teamfight Tactics) coach. "
    "Given a match summary from a high-elo game, provide specific, actionable advice "
    "explaining why the player placed where they did and what they should focus on "
    "next game. Reference specific units, traits, and items by name."
)
 
# ---------------------------------------------------------------------------
# Helpers: parse the Riot API blob
# ---------------------------------------------------------------------------
 
def _clean_name(raw: str) -> str:
    """Strip TFT17_ prefix for readability in the prompt."""
    return raw.replace("TFT17_", "").replace("TFT_Item_", "").replace("TFT4_Item_", "")
 
 
def _format_traits(traits: list[dict]) -> str:
    """Return only active traits (tier_current > 0), sorted by num_units desc."""
    active = [t for t in traits if t.get("tier_current", 0) > 0]
    active.sort(key=lambda t: -t.get("num_units", 0))
    if not active:
        return "none"
    return ", ".join(
        f"{_clean_name(t['name'])} {t['num_units']}/{t['tier_total']}"
        for t in active[:8]
    )
 
 
def _format_units(units: list[dict]) -> str:
    """Format board as 'UnitName★tier (Item1, Item2)' sorted by rarity desc."""
    units_sorted = sorted(units, key=lambda u: -u.get("rarity", 0))
    parts = []
    for u in units_sorted[:10]:
        name = _clean_name(u["character_id"])
        star = "★" * u.get("tier", 1)
        items = u.get("itemNames", [])
        item_str = ""
        if items:
            item_str = f" ({', '.join(_clean_name(i) for i in items[:3])})"
        parts.append(f"{name}{star}{item_str}")
    return ", ".join(parts)
 
 
def _build_user_message(participant: dict, match_id: str, game_version: str) -> str:
    placement   = participant["placement"]
    level       = participant["level"]
    last_round  = participant["last_round"]
    gold_left   = participant["gold_left"]
    elims       = participant["players_eliminated"]
    damage      = participant["total_damage_to_players"]
    traits_str  = _format_traits(participant.get("traits", []))
    units_str   = _format_units(participant.get("units", []))
    outcome     = "WIN (top 4)" if placement <= TOP_K_PLACEMENT else "LOSS (bot 4)"
 
    return (
        f"Match: {match_id} | Patch: {game_version}\n"
        f"Result: {outcome} — placed {placement}/8\n"
        f"Level: {level} | Last round: {last_round} | Gold left: {gold_left}\n"
        f"Players eliminated: {elims} | Damage dealt: {damage}\n"
        f"Active traits: {traits_str}\n"
        f"Final board: {units_str}\n\n"
        f"What should this player focus on to improve?"
    )
 
 
# ---------------------------------------------------------------------------
# DB: stream participants from challenger matches
# ---------------------------------------------------------------------------
 
def iter_participants(
    limit_matches: int = 2000,
) -> Iterator[tuple[dict, str, str]]:
    """
    Yields (participant_dict, match_id, game_version) for every participant
    in every match, filtered to challenger-player matches only.
 
    Uses player_matches to find matches where at least one challenger puuid
    participated, then parses all 8 participants from that match blob.
    """
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT match_id
                FROM player_matches
                GROUP BY match_id
                ORDER BY MAX(fetched_at) DESC
                LIMIT %s
            """, (limit_matches,))
            match_ids = [r["match_id"] for r in cur.fetchall()]
 
        if not match_ids:
            print("[dataset] No matches found in player_matches.")
            return
 
        print(f"[dataset] Found {len(match_ids)} matches to process.")
 
        BATCH = 100
        for i in range(0, len(match_ids), BATCH):
            batch_ids = match_ids[i:i + BATCH]
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                placeholders = ",".join(["%s"] * len(batch_ids))
                cur.execute(
                    f"SELECT match_id, data FROM matches WHERE match_id IN ({placeholders})",
                    batch_ids,
                )
                rows = cur.fetchall()
 
            for row in rows:
                try:
                    blob = json.loads(row["data"])
                    info = blob.get("info", {})
 
                    game_length  = info.get("game_length", 0)
                    game_version = info.get("game_version", "unknown")
                    tft_type     = info.get("tft_game_type", "")
 
                    if game_length < MIN_GAME_LENGTH:
                        continue
                    if game_length > MAX_GAME_LENGTH:
                        continue
                    if tft_type != "standard":
                        continue 
 
                    participants = info.get("participants", [])
                    for p in participants:
                        yield p, row["match_id"], game_version
 
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    print(f"[dataset] Skipping {row['match_id']}: {e}")
                    continue
    finally:
        conn.close()
 
 
# ---------------------------------------------------------------------------
# Label generation via Groq
# ---------------------------------------------------------------------------
 
def _generate_coaching_response(user_msg: str, groq_client) -> str:
    for attempt in range(10):
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.25,
                max_tokens=450,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            msg = str(e)
            if "rate_limit_exceeded" in msg:
                match_ms = re.search(r"(\d+)m([\d.]+)s", msg)
                match_s  = re.search(r"in ([\d.]+)s", msg)
                if match_ms:
                    wait = int(match_ms.group(1)) * 60 + float(match_ms.group(2)) + 2.0
                elif match_s:
                    wait = float(match_s.group(1)) + 1.0
                else:
                    wait = 60.0
                print(f"[dataset] Rate limited (attempt {attempt+1}/10), waiting {wait:.1f}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Max retries exceeded")
 
 
# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------
 
def generate_dataset(
    output_path: str = "finetune/tft_coaching_dataset.jsonl",
    n_samples: int = 2000,
    limit_matches: int = 5000,
    groq_client=None,
    balance_placements: bool = True,
) -> int:
    """
    Generate n_samples fine-tuning examples and write to output_path as JSONL.
 
    Args:
        output_path:        Where to write the .jsonl file.
        n_samples:          How many examples to generate.
        limit_matches:      How many matches to pull from DB (pull more than needed
                            so filters don't starve you).
        groq_client:        Optional pre-built Groq client. Created if not provided.
        balance_placements: If True, roughly balance top-4 and bot-4 examples
                            so the model doesn't just learn "winning = good".
 
    Returns:
        Number of examples written.
    """
    if groq_client is None:
        from groq import Groq
        groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
 
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
 
    print("[dataset] Collecting participants from DB...")
    all_participants: list[tuple[dict, str, str]] = list(
        iter_participants(limit_matches=limit_matches)
    )
    print(f"[dataset] Total participants collected: {len(all_participants)}")
 
    if not all_participants:
        print("[dataset] No participants found. Check your DB has matches.")
        return 0
 
    random.shuffle(all_participants)
 
    if balance_placements:
        wins  = [(p, m, v) for p, m, v in all_participants if p["placement"] <= TOP_K_PLACEMENT]
        losses = [(p, m, v) for p, m, v in all_participants if p["placement"] > TOP_K_PLACEMENT]
        half = n_samples // 2
        wins   = wins[:half]
        losses = losses[:half]
        candidates = wins + losses
        random.shuffle(candidates)
        print(f"[dataset] Balanced pool: {len(wins)} wins, {len(losses)} losses")
        
    else:
        candidates = all_participants
 
    completed = set()
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as existing:
            for line in existing:
                try:
                    row = json.loads(line)
                    completed.add(row["metadata"]["match_id"])
                except:
                    continue
        print(f"[dataset] Resuming — {len(completed)} already written, skipping those.")
    candidates = [c for c in candidates if c[1] not in completed]

    written = 0
    skipped_quality = 0
    skipped_error   = 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def process_one(args):
        participant, match_id, game_version = args
        time.sleep(random.uniform(0.5, 1.5))
        try:
            user_msg = _build_user_message(participant, match_id, game_version)
            coaching = _generate_coaching_response(user_msg, groq_client)
            if len(coaching.split()) < MIN_RESPONSE_WORDS:
                return ("quality", None)
            example = {
                "messages": [
                    {"role": "system",    "content": SYSTEM_PROMPT},
                    {"role": "user",      "content": user_msg},
                    {"role": "assistant", "content": coaching},
                ],
                "metadata": {
                    "match_id":     match_id,
                    "placement":    participant["placement"],
                    "level":        participant["level"],
                    "patch":        game_version,
                    "outcome":      "win" if participant["placement"] <= TOP_K_PLACEMENT else "loss",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            return ("ok", example)
        except Exception as e:
            print(f"[dataset] Error on {match_id}: {e}")
            return ("error", None)

    with open(output_path, "a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(process_one, c): c for c in candidates[:n_samples]}
            for future in as_completed(futures):
                status, example = future.result()
                if status == "ok":
                    f.write(json.dumps(example) + "\n")
                    written += 1
                elif status == "quality":
                    skipped_quality += 1
                elif status == "error":
                    skipped_error += 1

                if written % 50 == 0 and written > 0:
                    print(f"[dataset] Progress: {written}/{n_samples} written "
                          f"({skipped_quality} quality-skipped, {skipped_error} errors)")
 
    print(
        f"\n[dataset] Done.\n"
        f"  Written:         {written}\n"
        f"  Quality-skipped: {skipped_quality}\n"
        f"  Errors:          {skipped_error}\n"
        f"  Output:          {output_path}"
    )
    return written
 
 
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
 
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate TFT fine-tuning dataset")
    parser.add_argument("--output",   default="finetune/tft_coaching_dataset.jsonl")
    parser.add_argument("--samples",  type=int, default=2000)
    parser.add_argument("--matches",  type=int, default=5000,
                        help="Max matches to pull from DB")
    parser.add_argument("--no-balance", action="store_true",
                        help="Disable win/loss balancing")
    args = parser.parse_args()
 
    generate_dataset(
        output_path=args.output,
        n_samples=args.samples,
        limit_matches=args.matches,
        balance_placements=not args.no_balance,
    )