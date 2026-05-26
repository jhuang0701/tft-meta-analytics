from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional
import psycopg2
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

JUDGE_SYSTEM = """
You are an expert evaluator of TFT coaching AI responses.
Score the response on three dimensions, each from 1–5:

- specificity: Does it reference specific units, traits, augments, or mechanics from the query?
- accuracy: Is the TFT advice factually correct for the current patch?
- actionability: Does it give clear, concrete next steps the player can immediately act on?

Respond ONLY with valid JSON in this exact format (no prose, no markdown):
{"specificity": <1-5>, "accuracy": <1-5>, "actionability": <1-5>, "reasoning": "<one sentence>"}
""".strip()

@dataclass
class EvalResult:
    specificity:    int
    accuracy:       int
    actionability:  int
    reasoning:      str
    avg_score:      float

def judge_response(query: str, response: str, context: str = "") -> EvalResult:
    user_msg = f"Query: {query}\n\nContext provided to AI:\n{context}\n\nAI Response:\n{response}"
    for attempt in range(3):
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=150,
            )
            raw = resp.choices[0].message.content.strip()
            data = json.loads(raw)
            scores = [data["specificity"], data["accuracy"], data["actionability"]]
            return EvalResult(
                specificity=data["specificity"],
                accuracy=data["accuracy"],
                actionability=data["actionability"],
                reasoning=data.get("reasoning", ""),
                avg_score=sum(scores) / 3,
            )
        except (json.JSONDecodeError, KeyError):
            time.sleep(1)
    return EvalResult(0, 0, 0, "parse_error", 0.0)

def log_eval(
    session_id: str,
    query: str,
    response: str,
    retrieved_docs: list,
    eval_result: EvalResult,
    prompt_version: str,
    model: str,
    latency_ms: int,
) -> None:
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into coach_eval_results
                (session_id, query, response, retrieved_docs,
                 specificity, accuracy, actionability, judge_reasoning,
                 prompt_version, model, latency_ms)
            values (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                session_id, query, response,
                json.dumps([
                    {"id": d.id, "source": d.source, "score": d.score}
                    for d in retrieved_docs
                ]),
                eval_result.specificity,
                eval_result.accuracy,
                eval_result.actionability,
                eval_result.reasoning,
                prompt_version,
                model,
                latency_ms,
            ),
        )
        conn.commit()