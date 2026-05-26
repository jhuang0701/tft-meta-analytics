from __future__ import annotations
import os
from dataclasses import dataclass
from typing import List, Optional
import psycopg2
import numpy as np
from dotenv import load_dotenv
from rag.embedder import embed_query
from pgvector.psycopg2 import register_vector

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

@dataclass
class RetrievedDoc:
    id: int
    source: str
    doc_type: str
    title: str
    content: str
    score: float

def retrieve(
    query: str,
    top_k: int = 5,
    doc_type_filter: Optional[str] = None,
    score_threshold: float = 0.35,
) -> List[RetrievedDoc]:
    q_vec = np.array(embed_query(query), dtype=np.float32)

    if doc_type_filter:
        sql = """
            WITH scored AS (
                SELECT id, source, doc_type, title, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM public.tft_documents
                WHERE doc_type = %s
            )
            SELECT * FROM scored
            ORDER BY score DESC
            LIMIT %s
        """
        params = (q_vec.copy(), doc_type_filter, top_k)
    else:
        sql = """
            WITH scored AS (
                SELECT id, source, doc_type, title, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM public.tft_documents
            )
            SELECT * FROM scored
            ORDER BY score DESC
            LIMIT %s
        """
        params = (q_vec.copy(), top_k)

    conn = psycopg2.connect(DB_URL)
    try:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public, extensions")
            cur.execute(sql, params)
            rows = cur.fetchall()

        return [
            RetrievedDoc(
                id=r[0], source=r[1], doc_type=r[2],
                title=r[3], content=r[4], score=float(r[5])
            )
            for r in rows
            if float(r[5]) >= score_threshold
        ]
    finally:
        conn.close()