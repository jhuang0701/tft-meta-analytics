from __future__ import annotations
import hashlib
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

VARIANTS = ["control", "treatment_chain_of_thought", "treatment_persona"]

def assign_variant(session_id: str) -> str:
    digest = int(hashlib.md5(session_id.encode()).hexdigest(), 16)
    return VARIANTS[digest % len(VARIANTS)]

def init_session(session_id: str) -> str:
    variant = assign_variant(session_id)
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO prompt_ab_sessions (session_id, prompt_variant) VALUES (%s, %s) ON CONFLICT (session_id) DO NOTHING",
            (session_id, variant)
        )
        conn.commit()
    return variant

def get_variant(session_id: str) -> str:
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT prompt_variant FROM prompt_ab_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
    if row:
        return row[0]
    return init_session(session_id)

def record_turn(session_id: str, response_len: int) -> None:
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE prompt_ab_sessions SET turns = turns + 1, avg_response_len = (COALESCE(avg_response_len, 0) * turns + %s) / (turns + 1), updated_at = now() WHERE session_id = %s",
            (response_len, session_id)
        )
        conn.commit()

def record_feedback(session_id: str, positive: bool) -> None:
    col = "thumbs_up" if positive else "thumbs_down"
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE prompt_ab_sessions SET {col} = {col} + 1, updated_at = now() WHERE session_id = %s",
            (session_id,)
        )
        conn.commit()