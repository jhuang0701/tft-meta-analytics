from __future__ import annotations
import json
import os
from typing import List
import psycopg2
from psycopg2.extras import execute_values

from rag.chunker import chunk_markdown, Chunk
from rag.embedder import embed_documents

from dotenv import load_dotenv
load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

def get_conn():
    return psycopg2.connect(DB_URL)

def upsert_chunks(chunks: List[Chunk], embeddings: List[List[float]]) -> int:
    rows = [
        (
            c.source, c.doc_type, c.title, c.chunk_index,
            c.content, c.token_count,
            json.dumps(embeddings[i]),  
            json.dumps(c.metadata),
        )
        for i, c in enumerate(chunks)
    ]
    with get_conn() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            insert into tft_documents
                (source, doc_type, title, chunk_index, content, token_count, embedding, metadata)
            values %s
            on conflict do nothing
            """,
            rows,
            template="(%s,%s,%s,%s,%s,%s,%s::vector,%s::jsonb)",
        )
        conn.commit()
    return len(rows)

def ingest_document(
    text: str,
    source: str,
    doc_type: str,
    title: str,
    metadata: dict | None = None,
) -> int:
    chunks = chunk_markdown(text, source, doc_type, title, metadata=metadata)
    if not chunks:
        return 0
    embeddings = embed_documents([c.content for c in chunks])
    return upsert_chunks(chunks, embeddings)

if __name__ == "__main__":
    import argparse, pathlib
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--type", required=True,
                        choices=["patch_note", "tier_list", "meta_guide"])
    parser.add_argument("--title", default="")
    args = parser.parse_args()

    text = pathlib.Path(args.file).read_text()
    n = ingest_document(text, args.source, args.type,
                        args.title or args.source)
    print(f"Ingested {n} chunks from {args.file}")