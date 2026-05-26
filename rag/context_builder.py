from __future__ import annotations
from typing import List, Tuple
from rag.retriever import retrieve, RetrievedDoc

def build_rag_context(
    query: str,
    top_k: int = 5,
    max_tokens: int = 1800,
) -> Tuple[str, List[RetrievedDoc]]:
    docs = retrieve(query, top_k=top_k)
    if not docs:
        return "", []

    sections = []
    total_tokens = 0
    for doc in docs:
        doc_tokens = len(doc.content) // 4
        if total_tokens + doc_tokens > max_tokens:
            break
        header = f"[{doc.doc_type.upper()} | {doc.source} | score={doc.score:.2f}]"
        sections.append(f"{header}\n{doc.content}")
        total_tokens += doc_tokens

    context = "\n\n---\n\n".join(sections)
    return context, docs[:len(sections)]