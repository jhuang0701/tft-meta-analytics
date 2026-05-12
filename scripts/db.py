import psycopg2
import psycopg2.extras
import json
import os
import time
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

CACHE_TTL_HOURS = 24

def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            game_name   TEXT NOT NULL,
            tag_line    TEXT NOT NULL,
            puuid       TEXT NOT NULL,
            fetched_at  DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (game_name, tag_line)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id    TEXT PRIMARY KEY,
            data        TEXT NOT NULL,
            fetched_at  DOUBLE PRECISION NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS player_matches (
            puuid       TEXT NOT NULL,
            match_id    TEXT NOT NULL,
            fetched_at  DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (puuid, match_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS challenger_list (
            id          INTEGER PRIMARY KEY,
            data        TEXT NOT NULL,
            fetched_at  DOUBLE PRECISION NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cdragon (
            id          INTEGER PRIMARY KEY,
            data        TEXT NOT NULL,
            fetched_at  DOUBLE PRECISION NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS player_performance (
            puuid           TEXT PRIMARY KEY,
            game_name       TEXT NOT NULL,
            tag_line        TEXT NOT NULL,
            first_top4_rate  FLOAT NOT NULL,
            latest_top4_rate FLOAT NOT NULL,
            games_analyzed  INT NOT NULL,
            first_seen      DOUBLE PRECISION NOT NULL,
            last_seen       DOUBLE PRECISION NOT NULL
        )
    """)

    conn.commit()
    c.close()
    conn.close()

def is_fresh(fetched_at: float, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    return (time.time() - fetched_at) < (ttl_hours * 3600)

# ----------------------------
# PUUID
# ----------------------------
def get_cached_puuid(game_name: str, tag_line: str):
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(
        "SELECT puuid, fetched_at FROM players WHERE game_name=%s AND tag_line=%s",
        (game_name.lower(), tag_line.lower())
    )
    row = c.fetchone()
    c.close()
    conn.close()
    if row and is_fresh(row["fetched_at"]):
        return row["puuid"]
    return None

def save_puuid(game_name: str, tag_line: str, puuid: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO players (game_name, tag_line, puuid, fetched_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (game_name, tag_line) DO UPDATE
        SET puuid=EXCLUDED.puuid, fetched_at=EXCLUDED.fetched_at
    """, (game_name.lower(), tag_line.lower(), puuid, time.time()))
    conn.commit()
    c.close()
    conn.close()

# ----------------------------
# MATCH IDS
# ----------------------------
def get_cached_match_ids(puuid: str, ttl_hours: int = CACHE_TTL_HOURS):
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute(
        "SELECT match_id, fetched_at FROM player_matches WHERE puuid=%s ORDER BY fetched_at DESC",
        (puuid,)
    )
    rows = c.fetchall()
    c.close()
    conn.close()
    if rows and is_fresh(rows[0]["fetched_at"], ttl_hours=ttl_hours):
        return [r["match_id"] for r in rows]
    return None

def save_match_ids(puuid: str, match_ids: list):
    conn = get_conn()
    c = conn.cursor()
    now = time.time()
    for mid in match_ids:
        c.execute("""
            INSERT INTO player_matches (puuid, match_id, fetched_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (puuid, match_id) DO UPDATE
            SET fetched_at=EXCLUDED.fetched_at
        """, (puuid, mid, now))
    conn.commit()
    c.close()
    conn.close()

# ----------------------------
# MATCH DATA
# ----------------------------
def get_cached_match(match_id: str):
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT data FROM matches WHERE match_id=%s", (match_id,))
    row = c.fetchone()
    c.close()
    conn.close()
    if row:
        return json.loads(row["data"])
    return None

def save_match(match_id: str, data: dict):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO matches (match_id, data, fetched_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (match_id) DO NOTHING
    """, (match_id, json.dumps(data), time.time()))
    conn.commit()
    c.close()
    conn.close()

# ----------------------------
# CHALLENGER LIST
# ----------------------------
def get_cached_challenger_list():
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT data, fetched_at FROM challenger_list WHERE id=1")
    row = c.fetchone()
    c.close()
    conn.close()
    if row and is_fresh(row["fetched_at"]):
        return json.loads(row["data"])
    return None

def save_challenger_list(data: list):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO challenger_list (id, data, fetched_at)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE
        SET data=EXCLUDED.data, fetched_at=EXCLUDED.fetched_at
    """, (json.dumps(data), time.time()))
    conn.commit()
    c.close()
    conn.close()

# ----------------------------
# CDRAGON ASSETS
# ----------------------------
def get_cached_cdragon():
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT data, fetched_at FROM cdragon WHERE id=1")
    row = c.fetchone()
    c.close()
    conn.close()
    if row and is_fresh(row["fetched_at"], ttl_hours=168):
        return json.loads(row["data"])
    return None

def save_cdragon(data: dict):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO cdragon (id, data, fetched_at)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE
        SET data=EXCLUDED.data, fetched_at=EXCLUDED.fetched_at
    """, (json.dumps(data), time.time()))
    conn.commit()
    c.close()
    conn.close()

def get_cached_matches_batch(match_ids: list):
    """Fetch multiple matches in one query instead of one at a time."""
    if not match_ids:
        return {}
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    placeholders = ",".join(["%s"] * len(match_ids))
    c.execute(f"SELECT match_id, data FROM matches WHERE match_id IN ({placeholders})", match_ids)
    rows = c.fetchall()
    c.close()
    conn.close()
    return {row["match_id"]: json.loads(row["data"]) for row in rows}

def get_cached_match_ids_batch(puuids: list):
    """Fetch match ID lists for multiple players in one query."""
    if not puuids:
        return {}
    conn = get_conn()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    placeholders = ",".join(["%s"] * len(puuids))
    c.execute(
        f"SELECT puuid, match_id, fetched_at FROM player_matches WHERE puuid IN ({placeholders}) ORDER BY fetched_at DESC",
        puuids
    )
    rows = c.fetchall()
    c.close()
    conn.close()

    # group by puuid
    result = defaultdict(list)
    freshness = {}
    for row in rows:
        result[row["puuid"]].append(row["match_id"])
        if row["puuid"] not in freshness:
            freshness[row["puuid"]] = row["fetched_at"]

    # only return fresh entries
    return {
        puuid: ids
        for puuid, ids in result.items()
        if is_fresh(freshness[puuid])
    }

def get_player_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(DISTINCT puuid) FROM players")
    total_users = c.fetchone()[0]
    c.execute("""
        SELECT AVG(latest_top4_rate - first_top4_rate)
        FROM player_performance
        WHERE latest_top4_rate != first_top4_rate
    """)
    avg_improvement = c.fetchone()[0]
    c.close()
    conn.close()
    return total_users, avg_improvement

def save_player_performance(puuid, game_name, tag_line, top4_rate, games_analyzed):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO player_performance 
            (puuid, game_name, tag_line, first_top4_rate, latest_top4_rate, games_analyzed, first_seen, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (puuid) DO UPDATE
        SET latest_top4_rate = EXCLUDED.latest_top4_rate,
            games_analyzed   = EXCLUDED.games_analyzed,
            last_seen        = EXCLUDED.last_seen
    """, (puuid, game_name.lower(), tag_line.lower(), top4_rate, top4_rate,
          games_analyzed, time.time(), time.time()))
    conn.commit()
    c.close()
    conn.close()

# Initialize on import
init_db()