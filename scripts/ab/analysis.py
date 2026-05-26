from __future__ import annotations
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

def run_ab_analysis():
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
                prompt_variant,
                COUNT(*)            as sessions,
                SUM(thumbs_up)      as ups,
                SUM(thumbs_down)    as downs,
                ROUND(AVG(turns)::numeric, 2)            as avg_turns,
                ROUND(AVG(avg_response_len)::numeric, 0) as avg_response_len
            FROM prompt_ab_sessions
            GROUP BY prompt_variant
            ORDER BY prompt_variant
        """)
        rows = cur.fetchall()

    if not rows:
        print("No A/B data yet.")
        return

    print(f"\n{'Variant':<35} {'Sessions':>8} {'👍':>6} {'👎':>6} {'Turns':>7} {'RespLen':>8}")
    print("-" * 75)

    data = {}
    for row in rows:
        variant, sessions, ups, downs, turns, resp_len = row
        ups   = ups   or 0
        downs = downs or 0
        data[variant] = {"sessions": sessions, "ups": ups, "downs": downs}
        print(f"{variant:<35} {sessions:>8} {ups:>6} {downs:>6} {str(turns):>7} {str(resp_len):>8}")

    if len(data) >= 2:
        try:
            from scipy import stats
            variants    = list(data.keys())
            contingency = [[data[v]["ups"], data[v]["downs"]] for v in variants]
            if all(d["ups"] + d["downs"] > 0 for d in data.values()):
                chi2, p, dof, _ = stats.chi2_contingency(contingency)
                print(f"\nChi² = {chi2:.3f}, p = {p:.4f}, dof = {dof}")
                if p < 0.05:
                    winner = max(data, key=lambda v: data[v]["ups"] / max(data[v]["ups"] + data[v]["downs"], 1))
                    print(f"✅ Statistically significant (p < 0.05) — winner: {winner}")
                else:
                    print("⏳ Not yet significant — gather more data")
            else:
                print("\n⏳ No thumbs feedback recorded yet.")
        except ImportError:
            print("\n⚠ scipy not installed — run: pip install scipy")

if __name__ == "__main__":
    run_ab_analysis()