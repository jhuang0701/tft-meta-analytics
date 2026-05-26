from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List

@dataclass
class Chunk:
    source: str
    doc_type: str
    title: str
    chunk_index: int
    content: str
    token_count: int
    metadata: dict

def _rough_token_count(text: str) -> int:
    return len(text) // 4

def chunk_markdown(
    text: str,
    source: str,
    doc_type: str,
    title: str,
    chunk_size: int = 400,    
    overlap: int = 50,         
    metadata: dict | None = None,
) -> List[Chunk]:
    metadata = metadata or {}
    sections = re.split(r"(?=^#{1,3} )", text, flags=re.MULTILINE)
    chunks: List[Chunk] = []
    idx = 0

    for section in sections:
        section = section.strip()
        if not section:
            continue
        if _rough_token_count(section) <= chunk_size:
            chunks.append(Chunk(source, doc_type, title, idx, section,
                                _rough_token_count(section), metadata))
            idx += 1
        else:
            # Split on double newlines (paragraphs)
            paragraphs = [p.strip() for p in section.split("\n\n") if p.strip()]
            buffer, buffer_tokens = [], 0
            for para in paragraphs:
                para_tokens = _rough_token_count(para)
                if buffer_tokens + para_tokens > chunk_size and buffer:
                    content = "\n\n".join(buffer)
                    chunks.append(Chunk(source, doc_type, title, idx,
                                        content, _rough_token_count(content), metadata))
                    idx += 1
                    # overlap: keep last paragraph
                    buffer = buffer[-1:] if overlap > 0 else []
                    buffer_tokens = _rough_token_count(buffer[0]) if buffer else 0
                buffer.append(para)
                buffer_tokens += para_tokens
            if buffer:
                content = "\n\n".join(buffer)
                chunks.append(Chunk(source, doc_type, title, idx,
                                    content, _rough_token_count(content), metadata))
                idx += 1
    return chunks