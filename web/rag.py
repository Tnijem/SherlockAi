"""RAG engine: embed query → ChromaDB retrieval → (optional web search) → Ollama LLM → stream."""

from __future__ import annotations

import json
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
from logging_config import get_logger

log = get_logger("sherlock.rag")

SEARXNG_URL = "http://localhost:8888"

# Minimum cosine-similarity score for a chunk to be passed to the LLM.
# Chunks below this are too dissimilar to be useful and increase hallucination risk.
MIN_SCORE_THRESHOLD = 0.30

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
        timeout=60,
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
                json={"model": LLM_MODEL, "prompt": "", "keep_alive": "10m"},
                timeout=10,
            )
            requests.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": "keep", "keep_alive": "10m"},
                timeout=10,
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

    for coll in collections_to_query:
        try:
            res = coll.query(
                query_embeddings=[embedding],
                n_results=min(n, coll.count()),
                include=["documents", "metadatas", "distances"],
            )
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
                chunk_id = f"{r.get('path', '')}__chunk_{r['chunk']}"
                if chunk_id in bm25_lookup:
                    bm25_norm = bm25_lookup[chunk_id]["bm25_norm"]
                    # Weighted combination: 0.6 vector + 0.4 keyword
                    r["score"] = round(0.6 * r["score"] + 0.4 * bm25_norm, 4)

            # Add BM25-only results (not in vector results) with reduced score
            existing_ids = {f"{r.get('path', '')}__chunk_{r['chunk']}" for r in results}
            for bm25_r in bm25_results:
                if bm25_r["chunk_id"] not in existing_ids:
                    results.append({
                        "text": bm25_r["text"],
                        "source": bm25_r["source"],
                        "path": bm25_r["chunk_id"].rsplit("__chunk_", 1)[0] if "__chunk_" in bm25_r["chunk_id"] else "",
                        "chunk": int(bm25_r["chunk_id"].rsplit("__chunk_", 1)[1]) if "__chunk_" in bm25_r["chunk_id"] else 0,
                        "score": round(0.4 * bm25_r["bm25_norm"], 4),
                        "collection": "fts5",
                    })

            results.sort(key=lambda x: x["score"], reverse=True)

    return results[:n]


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
4. Cite every material claim inline using ONLY this format: [filename]
   Example: "The motion was denied on jurisdictional grounds. [Smith_v_Jones.txt]"
   You may cite multiple files in one sentence: [file1.txt] [file2.pdf]
5. For web sources cite as: [Web: page title or url]
6. Always name judges, parties, attorneys, and all other persons exactly as they appear in the documents.

═══ STRICTLY PROHIBITED ═══
- Using ANY knowledge from your training data that is not explicitly present in the document context below
- Inventing or guessing dates, names, dollar amounts, case numbers, rulings, procedural history, or any specific fact
- Presenting training knowledge as if it were document evidence
- Filling gaps with "typical" or "standard" legal outcomes as if they were established by the record
- Answering a specific case question when the documents do not contain the answer

═══ WHEN CONTEXT IS ABSENT OR INSUFFICIENT ═══
If the document context does not contain the information needed to answer the question, you MUST respond with this exact structure:
"The indexed documents do not contain [specific information requested].
I cannot answer this without the source material. [Identify what document type would contain the answer, e.g., 'A deposition transcript', 'The settlement agreement', 'The docket sheet'.]"

Do NOT attempt to answer. Do NOT say "typically" or "generally" or "in most cases." Stop there.

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
        f"[Doc: {c['source']} | Chunk {c['chunk']} | Relevance: {c['score']}]\n{c['text']}"
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

async def stream_response(
    query: str,
    user_id: int,
    scope: str = "both",
    query_type: str = "auto",
    verbosity_role: str = "attorney",
    research_mode: bool = False,
    history: list[dict] | None = None,
    case_context: dict | None = None,
) -> AsyncIterator[tuple]:
    """
    Yields (token, sources) tuples during streaming.
    Final yield is (token, sources, stats_dict) with Ollama performance metrics.
    """
    t_total = time.perf_counter()

    # Retrieve doc chunks
    t_retrieve = time.perf_counter()
    raw_chunks = retrieve(query, user_id, scope)
    ms_retrieve = _ms(t_retrieve)

    # ── Score threshold: drop chunks too dissimilar to be useful ──────────────
    chunks = [c for c in raw_chunks if c["score"] >= MIN_SCORE_THRESHOLD]
    top_score = chunks[0]["score"] if chunks else 0.0

    # ── No-context guard: refuse to call the LLM with no relevant material ────
    if not chunks and not research_mode:
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

    # Optionally fetch web results
    web_results: list[dict] = []
    if research_mode:
        web_results = search_web(query, n=5)

    prompt = _build_prompt(query, chunks, web_results if research_mode else None, history=history, top_score=top_score)
    system_prompt = _build_system_prompt(query_type, verbosity_role, research_mode, case_context=case_context)

    sources = [
        {
            "file":    c["source"],
            "path":    c.get("path", ""),
            "excerpt": c["text"][:200],
            "score":   c["score"],
        }
        for c in chunks
    ]

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

    t_llm = time.perf_counter()
    with requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model":  LLM_MODEL,
            "system": system_prompt,
            "prompt": prompt,
            "stream": True,
            "options": {"num_predict": 4096},
        },
        stream=True,
        timeout=120,
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
    chunks = retrieve(query_context or "deadlines filing dates statutes of limitations notices", user_id=user_id, scope=scope, n=12)
    if not chunks:
        return []

    doc_text = "\n\n---\n\n".join(
        f"[Doc: {c['source']} | Chunk {c['chunk']}]\n{c['text']}"
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
            timeout=120,
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
                timeout=120,
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
            "options": {"num_predict": 4096},
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

    sources = [
        {
            "file":    c["source"],
            "path":    c.get("path", ""),
            "excerpt": c["text"][:200],
            "score":   c["score"],
        }
        for c in chunks
    ]
    for r in web_results:
        sources.append({
            "file":    r["title"] or r["url"],
            "path":    r["url"],
            "excerpt": r["snippet"][:200],
            "score":   1.0,
            "web":     True,
        })
    return text, sources
