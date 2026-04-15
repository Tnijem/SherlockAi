"""
primary_law.chunker - section-aware splitter for authoritative legal text.

Design rule: a chunk must never span a section boundary. When a user asks about
O.C.G.A. § 9-3-24, retrieval should return a chunk whose `citation` metadata
exactly equals "O.C.G.A. § 9-3-24" - not a blob containing § 9-3-23 and half of
§ 9-3-24. This means the statute fetchers are responsible for emitting ONE
Document per section. This chunker then handles long sections by splitting them
into overlapping sub-chunks while preserving the section's citation metadata.

For non-statutory text (cases, court-rule PDFs, bills) we fall back to a
paragraph-based splitter with overlap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .fetchers.base import Document, validate_metadata


# Target chunk size in characters. Roughly 800-1200 tokens for English legal
# text. Kept conservative to leave headroom for embedder context (mxbai-embed
# -large has 512 token limit; at ~4 chars/token that's ~2000 chars max).
TARGET_CHARS = 1500
OVERLAP_CHARS = 200
MIN_CHARS = 80   # don't emit chunks smaller than this (tiny stubs hurt retrieval)


@dataclass
class Chunk:
    text: str
    metadata: dict


def _split_long_text(text: str, target: int, overlap: int) -> list[str]:
    """Paragraph-aware splitter for text longer than `target` chars."""
    if len(text) <= target:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    for para in paragraphs:
        pl = len(para) + 2  # account for the separator we'll re-add
        if buf_len + pl > target and buf:
            chunks.append("\n\n".join(buf).strip())
            # Start next buffer with tail of previous for overlap
            tail = chunks[-1][-overlap:] if overlap and len(chunks[-1]) > overlap else ""
            buf = [tail, para] if tail else [para]
            buf_len = len(tail) + pl
        else:
            buf.append(para)
            buf_len += pl

    if buf:
        chunks.append("\n\n".join(buf).strip())

    # A single paragraph that exceeds target - fall back to hard char-split.
    expanded: list[str] = []
    for c in chunks:
        if len(c) <= target * 1.5:
            expanded.append(c)
        else:
            step = target - overlap
            for i in range(0, len(c), step):
                expanded.append(c[i : i + target])
    return [c for c in expanded if len(c.strip()) >= MIN_CHARS]


def chunk_document(doc: Document) -> list[Chunk]:
    """Split one Document into Chunks, preserving metadata.

    Statutes: we assume the fetcher already emitted one Document per section.
    So normally this produces 1 chunk per doc. If a single section is very long
    (e.g. long definitional section), it's split with overlap but every chunk
    keeps the same citation metadata.
    """
    validate_metadata(doc.metadata)
    text = doc.text.strip()
    if len(text) < MIN_CHARS:
        return []

    parts = _split_long_text(text, TARGET_CHARS, OVERLAP_CHARS)
    total = len(parts)
    out: list[Chunk] = []
    for i, piece in enumerate(parts):
        md = dict(doc.metadata)
        md["chunk_index"] = i
        md["chunk_total"] = total
        out.append(Chunk(text=piece, metadata=md))
    return out


def chunk_all(docs: Iterable[Document]) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc))
    return all_chunks
