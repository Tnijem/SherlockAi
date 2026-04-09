"""RAG engine: embed query → ChromaDB retrieval → (optional web search) → Ollama LLM → stream."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from functools import lru_cache
from typing import AsyncIterator, Optional

import chromadb
import requests

from config import (
    CHROMA_URL, EMBED_MODEL, GLOBAL_COLLECTION, LLM_MODEL,
    OLLAMA_URL, RAG_TOP_N, user_collection,
)
try:
    import nas_text
    import nas_catalog
    _HAS_NAS_SEARCH = True
except ImportError:
    _HAS_NAS_SEARCH = False

from logging_config import get_logger

log = get_logger("sherlock.rag")

SEARXNG_URL = "http://localhost:8888"

# Minimum cosine-similarity score for a chunk to be passed to the LLM.
# Chunks below this are too dissimilar to be useful and increase hallucination risk.
MIN_SCORE_THRESHOLD = 0.30
MIN_SOURCE_DISPLAY_SCORE = 0.40  # chunks below this score are used for LLM context but not shown as sources

# ── ChromaDB client (persistent singleton) ────────────────────────────────────

_chroma: chromadb.HttpClient | None = None
_chroma_lock = threading.Lock()

def _chroma_client() -> chromadb.HttpClient:
    global _chroma
    if _chroma is None:
        with _chroma_lock:
            if _chroma is None:
                host, port = CHROMA_URL.rsplit(":", 1)
                host = host.replace("http://", "").replace("https://", "")
                _chroma = chromadb.HttpClient(host=host, port=int(port))
    return _chroma


def get_or_create_collection(name: str) -> chromadb.Collection:
    client = _chroma_client()
    return client.get_or_create_collection(
        name,
        metadata={"hnsw:space": "cosine"},
    )


def collection_exists(name: str) -> bool:
    try:
        _chroma_client().get_collection(name)
        return True
    except Exception:
        return False


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_text(text: str) -> list[float]:
    """Embed text via Ollama. Raises on failure."""
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text[:8192]},
        timeout=180,   # cold-load of bge-m3 can take >60s when LLM is resident
    )
    resp.raise_for_status()
    rj = resp.json()
    # Track embedding tokens (lightweight — batch-logged)
    p_tok = rj.get("prompt_eval_count", 0)
    if p_tok:
        _accumulate_embed_tokens(p_tok)
    emb = rj["embedding"]
    # L2-normalize so cosine distance == 1 - dot product (valid for any model)
    import math
    norm = math.sqrt(sum(x * x for x in emb)) or 1.0
    return [x / norm for x in emb]


# ── Batched embed token logging (avoid DB write per chunk) ────────────────────
_embed_token_buf: dict[str, int] = {"count": 0, "tokens": 0}
_embed_token_lock = threading.Lock()

def _accumulate_embed_tokens(prompt_tokens: int) -> None:
    """Accumulate embed token counts and flush every 50 calls to avoid DB thrash."""
    with _embed_token_lock:
        _embed_token_buf["count"] += 1
        _embed_token_buf["tokens"] += prompt_tokens
        if _embed_token_buf["count"] >= 50:
            _flush_embed_tokens()

def _flush_embed_tokens() -> None:
    """Write accumulated embed tokens to DB. Caller must hold _embed_token_lock."""
    tok = _embed_token_buf["tokens"]
    if tok <= 0:
        return
    _embed_token_buf["count"] = 0
    _embed_token_buf["tokens"] = 0
    try:
        from models import log_system_tokens
        log_system_tokens(source="system:embed", prompt_tokens=tok, completion_tokens=0)
    except Exception:
        pass


# LRU cache: avoid re-embedding identical queries (up to 256 cached)
@lru_cache(maxsize=256)
def _embed_cached(text: str) -> tuple[float, ...]:
    return tuple(embed_text(text))


def embed_query(text: str) -> list[float]:
    """Cached embed for query strings — skips Ollama roundtrip on repeat queries."""
    return list(_embed_cached(text.strip().lower()))


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ── Model keep-alive ──────────────────────────────────────────────────────────

def _keepalive_loop() -> None:
    """Ping Ollama every 4 min to keep LLM and embed model resident in memory."""
    while True:
        time.sleep(240)
        try:
            requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": "", "keep_alive": "30m"},
                timeout=600,   # long enough to cold-load the LLM if evicted
            )
            requests.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": "keep", "keep_alive": "30m"},
                timeout=600,   # long enough to cold-load the embed model
            )
        except Exception:
            pass


def start_keepalive() -> None:
    t = threading.Thread(target=_keepalive_loop, daemon=True, name="ollama-keepalive")
    t.start()
    log.info("Ollama keep-alive thread started", extra={"llm": LLM_MODEL, "embed": EMBED_MODEL})


# ── BM25 keyword search (FTS5) ────────────────────────────────────────────

def _fts5_query(query: str) -> str:
    """
    Convert a raw user query into a safe FTS5 MATCH expression.

    FTS5 treats bare hyphens as NOT, bare colons as column specifiers,
    and special chars like * " ( ) as operators — all of which cause
    "no such column" or syntax errors when user input is passed directly.

    Strategy: extract plain words, OR them together so partial matches
    still rank. Single-word queries are wrapped in quotes to prevent
    FTS5 interpreting them as column names.
    """
    import re
    # Pull out alphanumeric tokens (ignore punctuation / operators)
    tokens = re.findall(r"[a-zA-Z0-9']+", query)
    if not tokens:
        return '""'   # FTS5 matches nothing for empty query
    # Wrap each token in double-quotes so FTS5 treats them as literals
    return " OR ".join(f'"{t}"' for t in tokens)


def _bm25_search(query: str, collections: list[str], n: int = 20) -> list[dict]:
    """Keyword search via SQLite FTS5. Returns [{chunk_id, source, text, bm25_score}]."""
    import sqlite3
    from config import DB_PATH
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in collections)
        fts_query = _fts5_query(query)
        cur.execute(f"""
            SELECT chunk_id, source, content, bm25(chunk_fts) as score
            FROM chunk_fts
            WHERE chunk_fts MATCH ? AND collection IN ({placeholders})
            ORDER BY score
            LIMIT ?
        """, [fts_query] + collections + [n])
        results = []
        for row in cur.fetchall():
            results.append({
                "chunk_id": row[0],
                "source": row[1],
                "text": row[2],
                "bm25_score": -row[3],  # BM25 scores are negative in FTS5, negate for positive
            })
        conn.close()
        return results
    except Exception as e:
        log.warning("BM25 search failed: %s", e)
        return []


# ── Retrieval ─────────────────────────────────────────────────────────────────

def _validate_scope(scope: str, user_id: int) -> bool:
    """Validate that scope is a permitted collection name for this user."""
    import re
    allowed_keywords = {"all", "global", "user", "both"}
    if scope in allowed_keywords:
        return True
    # Allow user's own collection
    if scope == user_collection(user_id):
        return True
    # Allow case collections (must match pattern exactly)
    if re.fullmatch(r"case_\d+_docs", scope):
        return True
    return False


def retrieve(
    query: str,
    user_id: int,
    scope: str = "all",   # "all" | "global" | "user" | "both" | "case_{id}_docs"
    n: int = RAG_TOP_N,
    client_folder: str | None = None,
) -> list[dict]:
    """Return top-N relevant chunks across requested collections."""
    if not _validate_scope(scope, user_id):
        log.warning("Invalid scope rejected: %s (user %d)", scope, user_id)
        scope = "both"  # fallback to safe default

    embedding = embed_query(query)
    client = _chroma_client()
    results: list[dict] = []

    seen_names: set[str] = set()

    def _add(name: str):
        if name in seen_names:
            return
        try:
            collections_to_query.append(client.get_collection(name))
            seen_names.add(name)
        except Exception:
            pass

    collections_to_query = []

    if scope == "all":
        _add(GLOBAL_COLLECTION)
        _add(user_collection(user_id))
        try:
            for c in client.list_collections():
                name = c.name if hasattr(c, "name") else str(c)
                if name.startswith("case_") and name.endswith("_docs"):
                    _add(name)
        except Exception:
            pass
    elif scope in ("global", "both"):
        _add(GLOBAL_COLLECTION)
        if scope == "both":
            _add(user_collection(user_id))
    elif scope == "user":
        _add(user_collection(user_id))
    else:
        # Case-specific collection (e.g. case_7_docs).
        # Always include the global collection too — NAS files indexed
        # via the global indexer land in sherlock_cases, not the per-case
        # collection, so without this the query returns nothing.
        _add(scope)
        _add(GLOBAL_COLLECTION)

    # Build ChromaDB where filter for client_folder scoping
    where_filter = None
    if client_folder:
        where_filter = {"client_folder": client_folder}

    for coll in collections_to_query:
        try:
            count = coll.count()
            if count == 0:
                continue
            query_kwargs = dict(
                query_embeddings=[embedding],
                n_results=min(n, count),
                include=["documents", "metadatas", "distances"],
            )
            if where_filter:
                query_kwargs["where"] = where_filter
            res = coll.query(**query_kwargs)
            for doc, meta, dist in zip(
                res["documents"][0],
                res["metadatas"][0],
                res["distances"][0],
            ):
                results.append({
                    "text":       doc,
                    "source":     meta.get("source", meta.get("path", "unknown")),
                    "path":       meta.get("path", ""),
                    "chunk":      meta.get("chunk", 0),
                    "page_start": meta.get("page_start", 0),
                    "page_end":   meta.get("page_end", 0),
                    "line_start": meta.get("line_start", 0),
                    "line_end":   meta.get("line_end", 0),
                    "score":      round(1 - dist, 4),
                    "collection": coll.name,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)

    # ── Hybrid: BM25 keyword search ──────────────────────────────────────
    collection_names = list(seen_names)
    if collection_names:
        bm25_results = _bm25_search(query, collection_names, n=n * 4)
        if bm25_results:
            # Normalize BM25 scores to 0-1 range.
            # SQLite FTS5 bm25() returns NEGATIVE values: more negative = more relevant.
            # Normalize by dividing by the most-negative (best) score so the best = 1.0.
            min_bm25 = min(r["bm25_score"] for r in bm25_results)  # most negative = best
            if min_bm25 < 0:
                for r in bm25_results:
                    r["bm25_norm"] = r["bm25_score"] / min_bm25  # best → 1.0
            else:
                for r in bm25_results:
                    r["bm25_norm"] = 0

            # Build lookup of BM25 results by chunk_id
            bm25_lookup = {}
            for r in bm25_results:
                bm25_lookup[r["chunk_id"]] = r

            # Merge: boost vector results that also have BM25 hits
            for r in results:
                chunk_id = f"{r.get('path', '')}__chunk_{r.get('chunk', 0)}"
                if chunk_id in bm25_lookup:
                    bm25_norm = bm25_lookup[chunk_id]["bm25_norm"]
                    # Weighted combination: 0.6 vector + 0.4 keyword
                    r["score"] = round(0.6 * r["score"] + 0.4 * bm25_norm, 4)

            # Add BM25-only results (not in vector results) with reduced score
            existing_ids = {f"{r.get('path', '')}__chunk_{r.get('chunk', 0)}" for r in results}
            for bm25_r in bm25_results:
                if bm25_r["chunk_id"] not in existing_ids:
                    results.append({
                        "text": bm25_r["text"],
                        "source": bm25_r["source"],
                        "path": bm25_r["chunk_id"].rsplit("__chunk_", 1)[0] if "__chunk_" in bm25_r["chunk_id"] else "",
                        "chunk": int(bm25_r["chunk_id"].rsplit("__chunk_", 1)[1]) if "__chunk_" in bm25_r["chunk_id"] else 0,
                        "score": round(0.65 * bm25_r["bm25_norm"], 4),
                        "collection": "fts5",
                    })

            results.sort(key=lambda x: x["score"], reverse=True)

    return results[:n]



# ── Matter-scoped retrieval ──────────────────────────────────────────────

def retrieve_matter_chunks(
    query: str,
    user_id: int,
    matter_id: int,
    scope: str = "all",
    n: int = RAG_TOP_N,
) -> list[dict]:
    """
    Retrieve chunks with guaranteed coverage of every file in the matter.

    Two-pass approach:
      1. Standard similarity retrieval (same as retrieve())
      2. For any matter file NOT represented in pass-1 results,
         fetch the single best chunk for that file.

    This ensures cross-file synthesis queries (e.g. "total by provider")
    always have context from every attached document.
    """
    import json as _json
    from models import SessionLocal, MatterFile, Upload

    # ── Get all chroma_ids grouped by upload for this matter ──
    db = SessionLocal()
    try:
        rows = (
            db.query(Upload.id, Upload.filename, Upload.chroma_ids)
            .join(MatterFile, MatterFile.upload_id == Upload.id)
            .filter(MatterFile.matter_id == matter_id, Upload.status == "ready")
            .all()
        )
    finally:
        db.close()

    if not rows:
        # No files attached — fall back to standard retrieval
        return retrieve(query, user_id, scope, n)

    # Build map: upload_id -> {filename, chroma_ids set}
    file_map: dict[int, dict] = {}
    all_chunk_ids: list[str] = []
    for uid, fname, cids_json in rows:
        if not cids_json:
            continue
        try:
            cids = _json.loads(cids_json)
        except Exception:
            continue
        file_map[uid] = {"filename": fname, "chunk_ids": set(cids)}
        all_chunk_ids.extend(cids)

    if not all_chunk_ids:
        return retrieve(query, user_id, scope, n)

    # ── Full-context mode: if matter is small enough, pass ALL chunks ──
    FULL_CONTEXT_THRESHOLD = 80  # max chunks to dump everything
    if len(all_chunk_ids) <= FULL_CONTEXT_THRESHOLD:
        log.info("matter_full_context: %d chunks from %d files — using full context",
                 len(all_chunk_ids), len(file_map))
        embedding = embed_query(query)
        col_name = f"user_{user_id}_docs"
        try:
            client = _chroma_client()
            coll = client.get_collection(col_name)
            res = coll.get(
                ids=all_chunk_ids,
                include=["documents", "metadatas", "embeddings"],
            )
            if res["documents"]:
                chunks = []
                for doc, meta, emb in zip(res["documents"], res["metadatas"], res["embeddings"]):
                    # cosine similarity (embeddings are L2-normalized)
                    try:
                        raw_score = float(sum(a * b for a, b in zip(embedding, list(emb))))
                        # Floor at 0.50 — full-context mode means we want ALL chunks
                        # regardless of similarity (the whole point is complete coverage)
                        score = round(max(raw_score, 0.50), 4)
                    except Exception:
                        score = 0.50
                    chunks.append({
                        "text":       doc,
                        "source":     meta.get("source", "unknown"),
                        "path":       meta.get("path", ""),
                        "chunk":      meta.get("chunk", 0),
                        "page_start": meta.get("page_start", 0),
                        "page_end":   meta.get("page_end", 0),
                        "line_start": meta.get("line_start", 0),
                        "line_end":   meta.get("line_end", 0),
                        "score":      score,
                        "collection": col_name,
                    })
                chunks.sort(key=lambda x: x["score"], reverse=True)
                return chunks
        except Exception as e:
            log.warning("matter_full_context_error: %s", e)
            # Fall through to two-pass approach

    # ── Two-pass mode for larger matters ──
    # Pass 1: standard similarity search
    pass1 = retrieve(query, user_id, scope, n=n)

    # Track which matter files are already represented
    matter_chunk_set = set(all_chunk_ids)
    represented_files: set[int] = set()

    for chunk in pass1:
        chunk_path = chunk.get("path", "")
        for uid, info in file_map.items():
            if any(chunk_path and cid.startswith(str(chunk_path)) for cid in info["chunk_ids"]):
                represented_files.add(uid)
                break

    # Pass 2: fill gaps — fetch best chunk per missing file
    missing_uids = set(file_map.keys()) - represented_files
    if not missing_uids:
        return pass1

    log.info("matter_fill_gaps: %d/%d files missing from pass-1, fetching",
             len(missing_uids), len(file_map))

    embedding = embed_query(query)
    col_name = f"user_{user_id}_docs"
    try:
        client = _chroma_client()
        coll = client.get_collection(col_name)
    except Exception:
        return pass1

    for uid in missing_uids:
        info = file_map[uid]
        chunk_ids = list(info["chunk_ids"])
        if not chunk_ids:
            continue
        try:
            # Query ChromaDB for best-matching chunks from this file
            # Use the file path from the chunk IDs to filter
            file_path = chunk_ids[0].rsplit("__chunk_", 1)[0] if "__chunk_" in chunk_ids[0] else ""
            if file_path:
                res = coll.query(
                    query_embeddings=[embedding],
                    n_results=min(2, len(chunk_ids)),
                    where={"path": file_path},
                    include=["documents", "metadatas", "distances"],
                )
            else:
                # Fallback: query by IDs
                res = coll.query(
                    query_embeddings=[embedding],
                    n_results=min(2, len(chunk_ids)),
                    ids=chunk_ids,
                    include=["documents", "metadatas", "distances"],
                )
            if not res["documents"] or not res["documents"][0]:
                continue
            for doc, meta, dist in zip(
                res["documents"][0], res["metadatas"][0], res["distances"][0]
            ):
                score = round(1 - dist, 4)
                pass1.append({
                    "text":       doc,
                    "source":     meta.get("source", info["filename"]),
                    "path":       meta.get("path", ""),
                    "chunk":      meta.get("chunk", 0),
                    "page_start": meta.get("page_start", 0),
                    "page_end":   meta.get("page_end", 0),
                    "line_start": meta.get("line_start", 0),
                    "line_end":   meta.get("line_end", 0),
                    "score":      max(score, 0.31),  # floor above MIN_SCORE_THRESHOLD
                    "collection": col_name,
                    "matter_fill": True,
                })
        except Exception as e:
            log.warning("matter_fill_gap_error uid=%d: %s", uid, e)
            continue

    return pass1


# ── Web search (SearXNG) ──────────────────────────────────────────────────────

def search_web(query: str, n: int = 5) -> list[dict]:
    """
    Query local SearXNG instance. Returns list of {title, url, snippet}.
    Returns [] if SearXNG is unreachable.
    """
    try:
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": query, "format": "json", "categories": "general", "language": "en"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("results", [])[:n]:
            results.append({
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "snippet": r.get("content", r.get("snippet", "")),
            })
        return results
    except Exception:
        return []


def searxng_available() -> bool:
    try:
        r = requests.get(f"{SEARXNG_URL}/healthz", timeout=3)
        return r.ok
    except Exception:
        return False


# ── System prompt ─────────────────────────────────────────────────────────────

_BASE_SYSTEM = """You are Sherlock, a senior paralegal AI with the instincts of a 20-year litigation veteran. You work from the case documents provided, and when research mode is active, from public web sources as well. You are the attorney's most trusted researcher — precise, thorough, and unfailingly honest about what you do and don't know.

═══ ABSOLUTE RULES ═══
1. Work ONLY from the provided context. If a fact isn't in the documents, it does not exist.
2. If context is insufficient, say exactly what is missing and why it matters to the question.
3. Never speculate, invent, or fill gaps with general legal knowledge presented as case facts.
4. You ARE permitted and expected to synthesize, calculate, compare, and aggregate data ACROSS multiple documents. If the user asks for a total, summary table, or comparison — build it from the individual documents provided. Arithmetic on document data is not speculation; it is analysis.
5. Cite every material claim inline using ONLY this format: [filename]
   Example: "The motion was denied on jurisdictional grounds. [Smith_v_Jones.txt]"
   You may cite multiple files in one sentence: [file1.txt] [file2.pdf]
6. For web sources cite as: [Web: page title or url]
7. Always name judges, parties, attorneys, and all other persons exactly as they appear in the documents.

═══ STRICTLY PROHIBITED ═══
- Using ANY knowledge from your training data that is not explicitly present in the document context below
- Inventing or guessing dates, names, dollar amounts, case numbers, rulings, procedural history, or any specific fact
- Presenting training knowledge as if it were document evidence
- Filling gaps with "typical" or "standard" legal outcomes as if they were established by the record
- Answering a specific case question when the documents do not contain the answer

═══ WHEN CONTEXT IS ABSENT OR INSUFFICIENT ═══
If the document context does not contain ANY relevant information to answer the question, you MUST respond with this exact structure:
"The indexed documents do not contain [specific information requested].
I cannot answer this without the source material. [Identify what document type would contain the answer, e.g., 'A deposition transcript', 'The settlement agreement', 'The docket sheet'.]"

IMPORTANT: If multiple documents each contain PARTIAL information (e.g., individual bills, separate invoices, different provider statements), you HAVE the information — synthesize it. Build the summary, calculate the totals, create the comparison. Only use the "do not contain" response when truly NO relevant data exists.

Do NOT say "typically" or "generally" or "in most cases" when speculating. But DO perform calculations and aggregations on data that IS present.

═══ HOW TO RESPOND ═══

Adapt your structure to the question type:

DOCUMENT REVIEW / SUMMARY
→ Key parties and their roles
→ Core provisions or allegations (with citations)
→ Notable clauses, missing standard protections, or unusual terms
→ ⚠ Red flags or inconsistencies requiring attorney attention

FACTUAL / RESEARCH QUESTION
→ Direct answer with citation
→ Supporting context from documents
→ Conflicting information across documents (if any)
→ What the record does NOT establish (evidentiary gaps)

TIMELINE REQUEST
→ Chronological list of all dated events found in the documents
→ Flag missing dates or ambiguous sequences
→ Note any legally significant gaps (e.g., lapse between injury and filing)

RISK / DEADLINE ASSESSMENT
→ Identify statutes of limitations, filing deadlines, notice requirements
→ Flag expired or approaching deadlines
→ Note unsigned, undated, or incomplete documents
→ Identify conflicts between documents

DRAFTING SUPPORT
→ Pull precedent language verbatim from existing documents
→ Note what standard provisions are already present vs. missing
→ Flag terms that deviate from what other documents in the record use

═══ LANGUAGE & TONE ═══
- Distinguish clearly: "The contract states..." (fact) vs. "This suggests..." (inference) vs. "In my assessment..." (opinion)
- Be direct. Attorneys are busy. Lead with the answer, support with detail.
- Short questions get short answers. Complex questions get structured analysis.
- Never pad responses with disclaimers. One crisp note at the end if truly needed.
- If you find something the attorney didn't ask about but should know — say so."""

# Query-type directive prefixes — injected before the context
_QUERY_TYPE_DIRECTIVES: dict[str, str] = {
    "auto":     "",   # Sherlock decides based on the question
    "summary":  "TASK: Perform a DOCUMENT REVIEW / SUMMARY. Use that response format exactly. Do not answer as a factual question — summarize the documents themselves.",
    "timeline": "TASK: Build a TIMELINE. Extract every dated event from the documents in strict chronological order. Flag gaps and ambiguous sequences. Do not answer as a general question.",
    "risk":     "TASK: Perform a RISK / DEADLINE ASSESSMENT. Focus entirely on risks, deadlines, statutes of limitations, unsigned/undated documents, and conflicts. Do not summarize general content.",
    "drafting": "TASK: Provide DRAFTING SUPPORT. Extract verbatim precedent language, identify missing standard provisions, and flag deviations. Do not answer as a factual question.",
    "compare": "TASK: Perform a DOCUMENT COMPARISON. Analyze the provided documents side by side. Identify: (1) Key differences in terms, conditions, or obligations, (2) Conflicting provisions, (3) Missing clauses present in one but not the other, (4) Areas of agreement. Use a structured comparison format with clear headings.",
}

# Verbosity role modifiers — appended to system prompt
_VERBOSITY_MODIFIERS: dict[str, str] = {
    "attorney":  "\n\n═══ AUDIENCE: SENIOR ATTORNEY ═══\nLead with the one-sentence conclusion. Then support concisely. Skip scaffolding and preamble. Assume full legal knowledge. Use legal terms freely. Keep it dense and efficient.",
    "associate": "\n\n═══ AUDIENCE: ASSOCIATE ATTORNEY ═══\nProvide full IRAC-structured analysis. Show your reasoning. Include all citations. Use headings. Associates need to understand the full picture to draft, argue, or advise.",
    "paralegal": "\n\n═══ AUDIENCE: PARALEGAL ═══\nUse task-oriented format. Checklists where practical. Flag explicitly what needs attorney review vs. what the paralegal can handle. Be procedural and thorough.",
    "client":    "\n\n═══ AUDIENCE: CLIENT (NON-LAWYER) ═══\nUse plain English only. No legal jargon — if a term is unavoidable, explain it in parentheses. Warm, clear, reassuring tone. Focus on what this means for them personally, not legal technicalities.",
}


def _build_system_prompt(
    query_type: str = "auto",
    verbosity_role: str = "attorney",
    research_mode: bool = False,
    case_context: dict | None = None,
) -> str:
    parts = [_BASE_SYSTEM]

    # ── Case context block (pinned at top of system prompt) ──────────────────
    if case_context:
        lines = ["\n\n═══ ACTIVE CASE CONTEXT ═══"]
        lines.append(
            "You are currently working on the following case. Unless the user explicitly "
            "directs you to a different matter, ALL queries refer to this case. "
            "When the user says 'this case', 'the case', 'our client', 'the plaintiff', "
            "'the defendant', or similar — they mean this case."
        )
        if case_context.get("case_name"):
            lines.append(f"Case Name: {case_context['case_name']}")
        if case_context.get("case_number"):
            lines.append(f"Case Number: {case_context['case_number']}")
        if case_context.get("case_type"):
            lines.append(f"Case Type: {case_context['case_type']}")
        if case_context.get("client_name"):
            lines.append(f"Our Client: {case_context['client_name']}")
        if case_context.get("opposing_party"):
            lines.append(f"Opposing Party: {case_context['opposing_party']}")
        if case_context.get("jurisdiction"):
            lines.append(f"Jurisdiction: {case_context['jurisdiction']}")
        if case_context.get("assigned_to"):
            lines.append(f"Assigned Attorney: {case_context['assigned_to']}")
        if case_context.get("status"):
            lines.append(f"Status: {case_context['status']}")
        if case_context.get("description"):
            lines.append(f"Notes: {case_context['description']}")
        if case_context.get("matter_name"):
            lines.append(f"Current Task/Matter: {case_context['matter_name']}")
        lines.append("═══════════════════════════")
        parts.append("\n".join(lines))

    if research_mode:
        parts.append(
            "\n\n═══ RESEARCH MODE ACTIVE ═══\n"
            "Web search results are included below alongside case documents. "
            "Treat web sources as secondary public-record references — useful for statutes, case law, and general context, "
            "but NEVER as binding authority. Always prefer document evidence over web content. "
            "Cite web sources as [Web: url]."
        )

    directive = _QUERY_TYPE_DIRECTIVES.get(query_type, "")
    if directive:
        parts.append(f"\n\n{directive}")

    modifier = _VERBOSITY_MODIFIERS.get(verbosity_role, _VERBOSITY_MODIFIERS["attorney"])
    parts.append(modifier)

    return "".join(parts)


# ── Prompt assembly ───────────────────────────────────────────────────────────

_NO_CONTEXT_MSG = (
    "⚠ **No relevant documents found.**\n\n"
    "The indexed files do not contain information responsive to this query. "
    "Sherlock cannot answer without source material — fabricating an answer would be worse than useless in a legal context.\n\n"
    "**To proceed:** Upload and index the relevant case documents, verify the correct case scope is selected, "
    "or rephrase the question using terms that appear in the indexed files."
)

_LOW_CONFIDENCE_WARNING = (
    "⚠ RETRIEVAL CONFIDENCE WARNING: The document chunks retrieved for this query have low relevance scores. "
    "The indexed files may not contain a clear answer. "
    "If you cannot find the specific information asked for within the excerpts below, "
    "you MUST say so — do not guess or infer from general legal knowledge."
)


def _build_prompt(
    query: str,
    context_chunks: list[dict],
    web_results: list[dict] | None = None,
    history: list[dict] | None = None,
    top_score: float = 1.0,
) -> str:
    sections = []

    # Prepend conversation history if available
    if history:
        history_lines = []
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant":
                content = content[:500]
                label = "Sherlock"
            else:
                label = "User"
            history_lines.append(f"{label}: {content}")
        sections.append("[Conversation History]\n" + "\n".join(history_lines))

    # Inject low-confidence warning when retrieval scores are marginal
    if top_score < 0.50:
        sections.append(_LOW_CONFIDENCE_WARNING)

    fence = "═" * 60
    doc_text = "\n\n---\n\n".join(
        "[Doc: {src} | {loc} | Relevance: {score}]\n{txt}".format(
            src=c["source"],
            loc=("Page " + (str(c["page_start"]) if c["page_start"] == c["page_end"] else f"{c['page_start']}-{c['page_end']}"))
                if c.get("page_start") else
                ("Lines " + (str(c["line_start"]) if c["line_start"] == c["line_end"] else f"{c['line_start']}-{c['line_end']}"))
                if c.get("line_start") else
                f"Chunk {c.get('chunk', 0)}",
            score=c["score"],
            txt=c["text"],
        )
        for c in context_chunks
    )

    sections.append(
        f"{fence}\n"
        f"DOCUMENT CONTEXT — YOUR ONLY PERMITTED SOURCE OF TRUTH\n"
        f"Every factual claim in your response MUST be traceable to text in this block.\n"
        f"If the answer is not explicitly stated below, say so — do not guess.\n"
        f"{fence}\n\n"
        f"{doc_text}\n\n"
        f"{fence}\n"
        f"END OF DOCUMENT CONTEXT\n"
        f"{fence}"
    )

    if web_results:
        web_text = "\n\n---\n\n".join(
            f"[Web: {r['url']}]\nTitle: {r['title']}\n{r['snippet']}"
            for r in web_results
        )
        sections.append(f"Web search results (public record — secondary reference only):\n\n{web_text}")

    sections.append(f"Question: {query}")
    return "\n\n---\n\n".join(sections)


# ── LLM streaming ─────────────────────────────────────────────────────────────


# ── Conversational query rewriter ─────────────────────────────────────────────

_FOLLOWUP_SIGNALS = re.compile(
    r"\b(he|she|they|them|his|her|their|it|its|"
    r"the (judge|plaintiff|defendant|attorney|lawyer|court|case|"
    r"party|parties|contract|agreement|ruling|decision|amount|"
    r"date|filing|settlement|verdict|claim|motion|order|"
    r"document|file|report))\b",
    re.IGNORECASE,
)

def _is_followup(query: str, history: list[dict] | None) -> bool:
    """Return True if query is likely a contextual follow-up that needs rewriting."""
    if not history:
        return False
    q = query.strip()
    # Short queries are almost always follow-ups
    if len(q.split()) <= 6:
        return True
    # Contains pronouns or references that imply prior context
    if _FOLLOWUP_SIGNALS.search(q):
        return True
    return False


def _rewrite_query(query: str, history: list[dict]) -> str:
    """
    Use a fast LLM call to rewrite a follow-up question into a self-contained
    search query, using the last 3 conversation turns as context.
    Returns the rewritten query, or the original if rewrite fails.
    """
    # Build a compact history string (last 3 turns max)
    recent = history[-3:] if len(history) > 3 else history
    hist_lines = []
    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content)[:400]
        if role == "user":
            hist_lines.append(f"User: {content}")
        elif role == "assistant":
            hist_lines.append(f"Assistant: {content[:200]}")

    hist_text = "\n".join(hist_lines)
    prompt = (
        f"Given this conversation:\n{hist_text}\n\n"
        f"Rewrite this follow-up question as a fully self-contained search query "
        f"(no pronouns, no references to \'the case\' without naming it). "
        f"Output ONLY the rewritten query — no explanation, no quotes.\n\n"
        f"Follow-up: {query}\nRewritten:"
    )
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": LLM_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 60},
            },
            timeout=30,
        )
        resp.raise_for_status()
        rewritten = resp.json().get("response", "").strip().strip('"\'\' ')
        if rewritten and len(rewritten) > 4:
            log.info("query_rewritten original=%r rewritten=%r", query[:80], rewritten[:80])
            return rewritten
    except Exception as e:
        log.warning("query_rewrite_failed: %s", e)
    return query



# ── NAS Catalog + FTS fallback search ─────────────────────────────────────────

def _nas_fallback_search(query: str, limit: int = 10) -> list[dict]:
    """
    Search NAS catalog metadata + full-text content when ChromaDB has no results.
    Returns list of dicts with file info and text snippets.
    """
    if not _HAS_NAS_SEARCH:
        return []

    results = []
    seen_paths = set()

    # 1. Full-text content search (Tier 2)
    try:
        fts = nas_text.search_text(query=query, limit=limit)
        for r in fts.get("results", []):
            fp = r.get("file_path", "")
            if fp in seen_paths:
                continue
            seen_paths.add(fp)
            results.append({
                "source": r.get("filename", os.path.basename(fp)),
                "path": fp,
                "text": r.get("snippet", "").replace("<b>", "**").replace("</b>", "**"),
                "score": 0.40,  # Below embedding threshold but shows as "NAS search"
                "collection": "nas_fts",
                "nas_search": True,
            })
    except Exception as e:
        log.warning("nas_fts_search_error: %s", e)

    # 2. Catalog filename search (Tier 1) — catch files not yet text-extracted
    try:
        cat = nas_catalog.search_catalog(query=query, limit=limit)
        for r in cat.get("results", []):
            fp = r.get("file_path", "")
            if fp in seen_paths:
                continue
            seen_paths.add(fp)
            results.append({
                "source": r.get("filename", os.path.basename(fp)),
                "path": fp,
                "text": f"[NAS file: {r.get('filename', '')} | Client: {r.get('client_folder', '?')} | Type: {r.get('extension', '?')} | Modified: {r.get('mtime_date', '?')}]",
                "score": 0.35,
                "collection": "nas_catalog",
                "nas_search": True,
            })
    except Exception as e:
        log.warning("nas_catalog_search_error: %s", e)

    return results[:limit]



def _is_complex_query(query: str) -> bool:
    """Heuristic: does this query benefit from a stronger model?"""
    q = query.lower()
    if any(kw in q for kw in [
        "compare", "contrast", "analyze the interplay",
        "across all cases", "pattern", "trend",
        "what are the strongest arguments",
        "predict", "strategy", "synthesize",
    ]):
        return True
    if len(query.split()) > 50:
        return True
    return False


def _build_prompt_text(query, chunks, query_type, verbosity_role, research_mode, web_results):
    """Build prompt text for cloud LLM (same format as local, but from args)."""
    context_parts = []
    for i, c in enumerate(chunks):
        source = c.get("source", "unknown")
        text = c.get("text", "")
        chunk_idx = c.get("chunk", 0)
        context_parts.append(f"[Doc: {source} | Chunk {chunk_idx}]\n{text}")

    context = "\n\n---\n\n".join(context_parts) if context_parts else "(No documents found)"

    web_context = ""
    if web_results:
        web_parts = [f"[Web: {w.get('title', '')}]\n{w.get('snippet', '')}" for w in web_results[:3]]
        web_context = "\n\nWeb search results:\n" + "\n\n".join(web_parts)

    return f"""Based on the following document excerpts, answer the user\'s question.

{context}
{web_context}

User question: {query}"""


async def stream_response(
    query: str,
    user_id: int,
    scope: str = "both",
    query_type: str = "auto",
    verbosity_role: str = "attorney",
    research_mode: bool = False,
    history: list[dict] | None = None,
    case_context: dict | None = None,
    matter_id: int | None = None,
    client_folder: str | None = None,
) -> AsyncIterator[tuple]:
    """
    Yields (token, sources) tuples during streaming.
    Final yield is (token, sources, stats_dict) with Ollama performance metrics.
    """
    t_total = time.perf_counter()

    # Rewrite follow-up queries into self-contained search queries
    search_query = query
    if history and _is_followup(query, history):
        search_query = _rewrite_query(query, history)

    # Retrieve doc chunks
    t_retrieve = time.perf_counter()
    if matter_id:
        raw_chunks = retrieve_matter_chunks(search_query, user_id, matter_id, scope)
    else:
        raw_chunks = retrieve(search_query, user_id, scope, client_folder=client_folder)
    ms_retrieve = _ms(t_retrieve)

    # ── Score threshold: drop chunks too dissimilar to be useful ──────────────
    chunks = [c for c in raw_chunks if c["score"] >= MIN_SCORE_THRESHOLD]
    top_score = chunks[0]["score"] if chunks else 0.0

    # ── No-context guard: fall back to NAS catalog/FTS search ────────────────
    if not chunks and not research_mode:
        # Try NAS full-text + catalog search as fallback
        nas_results = _nas_fallback_search(search_query, limit=15)
        if nas_results:
            log.info("rag_nas_fallback: %d NAS results for query", len(nas_results))
            chunks = nas_results  # Use NAS results as context
        else:
            log.warning(
                "rag_no_context",
                extra={
                    "user_id": user_id,
                    "query": query[:120],
                    "scope": scope,
                    "raw_chunks": len(raw_chunks),
                    "top_raw_score": raw_chunks[0]["score"] if raw_chunks else 0.0,
                },
            )
            yield (_NO_CONTEXT_MSG, [])
            return

    # ── Always supplement with NAS search for broader coverage ─────────────
    if not research_mode:
        nas_limit = 15 if len(chunks) < 3 else 8
        nas_extra = _nas_fallback_search(search_query, limit=nas_limit)
        existing_paths = {c.get("path", "") for c in chunks}
        added = 0
        for nr in nas_extra:
            if nr["path"] not in existing_paths:
                chunks.append(nr)
                existing_paths.add(nr["path"])
                added += 1
        if added:
            log.info("rag_nas_supplement: added %d NAS results (had %d ChromaDB chunks)", added, len(chunks) - added)

    # Optionally fetch web results
    web_results: list[dict] = []
    if research_mode:
        web_results = search_web(query, n=5)

    prompt = _build_prompt(query, chunks, web_results if research_mode else None, history=history, top_score=top_score)
    system_prompt = _build_system_prompt(query_type, verbosity_role, research_mode, case_context=case_context)

    # Deduplicate: one entry per unique file, highest-scoring chunk wins
    _seen_sources: dict[str, dict] = {}
    for c in chunks:
        if c["score"] < MIN_SOURCE_DISPLAY_SCORE:
            continue
        key = c["source"].lower()
        if key not in _seen_sources or c["score"] > _seen_sources[key]["score"]:
            _seen_sources[key] = {
                "file":    c["source"],
                "path":    c.get("path", ""),
                "excerpt": c["text"][:200],
                "score":   c["score"],
            }
    sources = sorted(_seen_sources.values(), key=lambda x: x["score"], reverse=True)

    # Add web sources (score=1.0 marks them as web results)
    for r in web_results:
        sources.append({
            "file":    r["title"] or r["url"],
            "path":    r["url"],
            "excerpt": r["snippet"][:200],
            "score":   1.0,
            "web":     True,
        })

    collections_hit = list({c.get("collection", "?") for c in chunks})
    top_score = chunks[0]["score"] if chunks else 0.0

    log.info(
        "rag_query_start",
        extra={
            "user_id":             user_id,
            "query":               query[:120],
            "scope":               scope,
            "query_type":          query_type,
            "verbosity_role":      verbosity_role,
            "research_mode":       research_mode,
            "sources":             len(chunks),
            "web_results":         len(web_results),
            "top_score":           top_score,
            "collections":         ",".join(collections_hit),
            "latency_retrieve_ms": ms_retrieve,
        },
    )

    # ── Hybrid routing: decide local vs cloud ────────────────────────────────
    _use_cloud = False
    try:
        import cloud_llm
        from config import CLOUD_MODE, CLOUD_ENABLED
        if cloud_llm.cloud_available():
            if CLOUD_MODE == "always":
                _use_cloud = True
            elif CLOUD_MODE == "fallback":
                # Escalate if retrieval quality is low or query is complex
                _use_cloud = (top_score < 0.40) or _is_complex_query(query)
            # CLOUD_MODE == "manual": only via explicit override (future)
    except Exception:
        pass  # Cloud not available — use local

    t_llm = time.perf_counter()

    if _use_cloud:
        # ── Cloud path: scrub → cloud API → re-identify ──────────────────
        try:
            from privacy_gateway import scrub_for_cloud, StreamReidentifier

            scrub_result = scrub_for_cloud(query, chunks, system_prompt)
            if scrub_result is None:
                # RED sensitivity — fall back to local
                log.info("rag_cloud_blocked: RED sensitivity, using local")
                _use_cloud = False
            else:
                scrubbed_query, scrubbed_system, scrubbed_chunks, entity_map = scrub_result
                # Build scrubbed prompt
                scrubbed_prompt = _build_prompt_text(scrubbed_query, scrubbed_chunks, query_type, verbosity_role, research_mode, web_results)
                reid = StreamReidentifier(entity_map)

                log.info("rag_cloud_start: scrubbed %d entities", entity_map.entity_count)

                first = True
                cloud_tokens = 0
                async for chunk in cloud_llm.stream_cloud_response(
                    system_prompt=scrubbed_system,
                    user_prompt=scrubbed_prompt,
                ):
                    if chunk["done"]:
                        # Flush remaining buffer
                        remaining = reid.flush()
                        if remaining:
                            yield (remaining, [])

                        ms_llm = _ms(t_llm)
                        ms_total = _ms(t_total)
                        usage = chunk.get("usage", {})
                        prompt_tokens = usage.get("input_tokens", 0)
                        completion_tokens = usage.get("output_tokens", 0)
                        cost_usd = usage.get("cost_usd", 0.0)
                        tokens_per_sec = (
                            round(completion_tokens / (ms_llm / 1000), 1)
                            if ms_llm > 0 else 0.0
                        )
                        log.info(
                            "rag_cloud_done",
                            extra={
                                "user_id": user_id,
                                "query": query[:120],
                                "provider": cloud_llm.get_cloud_config()["provider"],
                                "model": cloud_llm.get_cloud_config()["model"],
                                "entities_scrubbed": entity_map.entity_count,
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": completion_tokens,
                                "cost_usd": round(cost_usd, 6),
                                "tokens_per_sec": tokens_per_sec,
                                "latency_llm_ms": ms_llm,
                                "latency_total_ms": ms_total,
                            },
                        )
                        yield ("", [], {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                            "tokens_per_sec": tokens_per_sec,
                            "latency_llm_ms": ms_llm,
                            "latency_total_ms": ms_total,
                            "source": "cloud",
                            "cloud_provider": cloud_llm.get_cloud_config()["provider"],
                            "cloud_model": cloud_llm.get_cloud_config()["model"],
                            "entities_scrubbed": entity_map.entity_count,
                            "cost_usd": round(cost_usd, 6),
                        })
                        return
                    else:
                        raw_token = chunk["token"]
                        clean_token = reid.feed(raw_token)
                        if clean_token:
                            cloud_tokens += 1
                            yield (clean_token, sources if first else [])
                            first = False
        except Exception as e:
            log.warning("rag_cloud_error: %s — falling back to local", str(e)[:200])
            _use_cloud = False

    if not _use_cloud:
        # ── Local Ollama path (default) ──────────────────────────────────────
        with requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":  LLM_MODEL,
                "system": system_prompt,
                "prompt": prompt,
                "stream": True,
                "options": {"num_predict": 8192},
            },
            stream=True,
            timeout=600,
        ) as resp:
            resp.raise_for_status()
            first = True
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                token = data.get("response", "")
                if token:
                    yield (token, sources if first else [])
                    first = False
                if data.get("done"):
                    ms_llm   = _ms(t_llm)
                    ms_total = _ms(t_total)
                    # Extract Ollama token metrics from final chunk
                    prompt_tokens = data.get("prompt_eval_count", 0)
                    completion_tokens = data.get("eval_count", 0)
                    eval_duration_ns = data.get("eval_duration", 0)
                    tokens_per_sec = (
                        round(completion_tokens / (eval_duration_ns / 1e9), 1)
                        if eval_duration_ns > 0 else 0.0
                    )
                    log.info(
                        "rag_query_done",
                        extra={
                            "user_id":             user_id,
                            "query":               query[:120],
                            "scope":               scope,
                            "query_type":          query_type,
                            "verbosity_role":      verbosity_role,
                            "research_mode":       research_mode,
                            "sources":             len(chunks),
                            "web_results":         len(web_results),
                            "top_score":           top_score,
                            "latency_retrieve_ms": ms_retrieve,
                            "latency_llm_ms":      ms_llm,
                            "latency_total_ms":    ms_total,
                            "prompt_tokens":       prompt_tokens,
                            "completion_tokens":   completion_tokens,
                            "tokens_per_sec":      tokens_per_sec,
                        },
                    )
                    # Yield token stats as final metadata (empty token, no sources)
                    yield ("", [], {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "tokens_per_sec": tokens_per_sec,
                        "latency_llm_ms": ms_llm,
                        "latency_total_ms": ms_total,
                        "source": "local",
                    })
                    break


def extract_deadlines(
    query_context: str,
    user_id: int,
    scope: str = "all",
) -> list[dict]:
    """
    Run a deadline-extraction pass over the indexed documents.
    Returns list of {date_str, description, dl_type, source_file, urgency}.
    """

    # When case selector provides client_folder, use "all" scope for retrieval
    # and let post-filter narrow by path. Bare "case" scope is invalid without case_id.
    if scope == "case" and client_folder:
        scope = "all"
    elif scope == "case":
        scope = "both"

    chunks = retrieve(query_context or "deadlines filing dates statutes of limitations notices", user_id=user_id, scope=scope, n=12)
    if not chunks:
        return []

    doc_text = "\n\n---\n\n".join(
        f"[Doc: {c['source']} | Chunk {c.get('chunk', 0)}]\n{c['text']}"
        for c in chunks
    )

    system = (
        "You are a deadline extraction engine. Extract EVERY date, deadline, filing window, "
        "statute of limitations, notice requirement, contract deadline, hearing date, and time-sensitive "
        "obligation from the provided documents. "
        "Return ONLY valid JSON — an array of objects with these exact keys:\n"
        '  "date_str": the date or time period (ISO date YYYY-MM-DD if known, else descriptive string),\n'
        '  "description": what the deadline is for (1-2 sentences, cite the source doc),\n'
        '  "dl_type": one of: statute_of_limitations | filing | notice | contract | hearing | other,\n'
        '  "source_file": filename from [Doc: ...] tag,\n'
        '  "urgency": critical (expired or <30 days) | high (30-90 days) | normal (>90 days or unknown)\n'
        "Return [] if no deadlines found. No prose, no explanation — only the JSON array."
    )

    prompt = f"Documents:\n\n{doc_text}\n\n---\n\nExtract all deadlines and time-sensitive obligations as JSON."

    import requests as _req
    from config import OLLAMA_URL, LLM_MODEL
    import json as _json

    try:
        t0 = time.perf_counter()
        resp = _req.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": LLM_MODEL, "prompt": prompt, "system": system,
                  "stream": False, "options": {"temperature": 0.0, "num_predict": 2048}},
            timeout=600,
        )
        resp.raise_for_status()
        rj = resp.json()
        raw = rj.get("response", "").strip()

        # Log system token usage
        from models import log_system_tokens
        p_tok = rj.get("prompt_eval_count", 0)
        c_tok = rj.get("eval_count", 0)
        e_dur = rj.get("eval_duration", 0)
        log_system_tokens(
            source="system:deadline",
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            tokens_per_sec=round(c_tok / (e_dur / 1e9), 1) if e_dur > 0 else 0.0,
            latency_ms=_ms(t0),
            user_id=user_id,
        )

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        return _json.loads(raw)
    except Exception as e:
        log.warning(f"Deadline extraction failed: {e}")
        return []


def generate_brief(query: str, user_id: int, scope: str = "all") -> dict:
    """
    Generate a matter brief synchronously. Returns {brief_md, risks_md}.
    Runs two passes: summary + risk.
    """
    import requests as _req
    from config import OLLAMA_URL, LLM_MODEL

    def _run(prompt_text: str, system_text: str) -> str:
        try:
            t0 = time.perf_counter()
            resp = _req.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": prompt_text, "system": system_text,
                      "stream": False, "options": {"temperature": 0.05, "num_predict": 1024}},
                timeout=600,
            )
            resp.raise_for_status()
            rj = resp.json()
            # Log system token usage
            from models import log_system_tokens
            p_tok = rj.get("prompt_eval_count", 0)
            c_tok = rj.get("eval_count", 0)
            e_dur = rj.get("eval_duration", 0)
            log_system_tokens(
                source="system:brief",
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                tokens_per_sec=round(c_tok / (e_dur / 1e9), 1) if e_dur > 0 else 0.0,
                latency_ms=_ms(t0),
                user_id=user_id,
            )
            return rj.get("response", "").strip()
        except Exception as e:
            return f"[Brief generation failed: {e}]"

    chunks = retrieve("summary overview parties allegations key provisions", user_id=user_id, scope=scope, n=8)
    if not chunks:
        return {"brief_md": "No documents indexed for this matter.", "risks_md": ""}

    doc_text = "\n\n---\n\n".join(
        f"[Doc: {c['source']}]\n{c['text']}" for c in chunks
    )

    brief_system = (
        _BASE_SYSTEM +
        "\n\nTASK: Produce a MATTER BRIEF. Structure: "
        "**Parties** | **Core Facts / Allegations** | **Key Documents** | **Current Posture**. "
        "Be concise — this is an executive summary for a busy partner. 200-400 words max."
    )
    brief_md = _run(f"Documents:\n\n{doc_text}\n\n---\n\nProduce a matter brief.", brief_system)

    risk_system = (
        _BASE_SYSTEM +
        "\n\nTASK: RISK & DEADLINE ASSESSMENT only. List: "
        "critical deadlines, statutes of limitations, missing documents, "
        "unsigned/undated items, and any red flags. Bullet format. Be blunt."
    )
    risks_md = _run(f"Documents:\n\n{doc_text}\n\n---\n\nList all risks, deadlines, and red flags.", risk_system)

    return {"brief_md": brief_md, "risks_md": risks_md}


def query_sync(
    query: str,
    user_id: int,
    scope: str = "both",
    query_type: str = "auto",
    verbosity_role: str = "attorney",
    research_mode: bool = False,
) -> tuple[str, list[dict]]:
    """Non-streaming query — returns (full_response_text, sources)."""
    chunks = retrieve(query, user_id, scope)
    web_results = search_web(query, n=5) if research_mode else []
    prompt = _build_prompt(query, chunks, web_results if research_mode else None)
    system_prompt = _build_system_prompt(query_type, verbosity_role, research_mode)

    t0 = time.perf_counter()
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model":  LLM_MODEL,
            "system": system_prompt,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 8192},
        },
        timeout=180,
    )
    resp.raise_for_status()
    rj = resp.json()
    text = rj.get("response", "")

    # Log token usage
    from models import log_system_tokens
    p_tok = rj.get("prompt_eval_count", 0)
    c_tok = rj.get("eval_count", 0)
    e_dur = rj.get("eval_duration", 0)
    log_system_tokens(
        source="system:sync",
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        tokens_per_sec=round(c_tok / (e_dur / 1e9), 1) if e_dur > 0 else 0.0,
        latency_ms=_ms(t0),
        user_id=user_id,
    )

    # Deduplicate: one entry per unique file, highest-scoring chunk wins
    _seen_sources: dict[str, dict] = {}
    for c in chunks:
        if c["score"] < MIN_SOURCE_DISPLAY_SCORE:
            continue
        key = c["source"].lower()
        if key not in _seen_sources or c["score"] > _seen_sources[key]["score"]:
            _seen_sources[key] = {
                "file":    c["source"],
                "path":    c.get("path", ""),
                "excerpt": c["text"][:200],
                "score":   c["score"],
            }
    sources = sorted(_seen_sources.values(), key=lambda x: x["score"], reverse=True)
    for r in web_results:
        sources.append({
            "file":    r["title"] or r["url"],
            "path":    r["url"],
            "excerpt": r["snippet"][:200],
            "score":   1.0,
            "web":     True,
        })
    return text, sources
