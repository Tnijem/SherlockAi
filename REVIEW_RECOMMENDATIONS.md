# Sherlock — Review Recommendations

> Observations and improvement suggestions from a full codebase review.
> Organized by priority: critical → high → medium → low.

---

## Critical — Fix Before Production

### 1. JWT Secret is Insecure by Default — *OPEN*

**File**: `web/config.py`
**Issue**: `JWT_SECRET` has a hardcoded default fallback. If `sherlock.conf` is missing or the key isn't set, the app runs with a known secret — anyone can forge tokens.
**Fix**: Refuse to start if `JWT_SECRET` is not explicitly configured. Auto-generate a random secret on first setup and persist it to `sherlock.conf`.

### 2. Job Tracking Lost on Restart — *OPEN*

**Files**: `web/indexer.py`, `web/audio.py`
**Issue**: `_jobs` dict is in-memory only. If the server restarts mid-indexing, clients polling a job_id will get 404s forever, and the indexing state is lost.
**Fix**: Persist job state to a `jobs` table in SQLite. Mark jobs as `interrupted` on startup and allow retry. This also enables a "recent jobs" history view in the admin panel.

### 3. No Input Sanitization on Telemetry File Transfer — *OPEN (hardcoded paths FIXED)*

**File**: `telemetry/agent.py`
**Issue**: The file push/pull endpoints restrict to `SHERLOCK_BASE` but the path validation should guard against symlink traversal attacks (e.g., symlink inside Sherlock base pointing to `/etc/passwd`).
**Fix**: Resolve the real path with `Path.resolve()` and verify it's still under `SHERLOCK_BASE` after resolution.

---

## High — Significant Improvements

### 4. Single-File Backend Is Getting Unwieldy — *OPEN*

**File**: `web/main.py` (~1850 lines)
**Issue**: All routes, middleware, background threads, and helpers in one file. Hard to navigate, test, or review.
**Fix**: Split into FastAPI routers:
```
web/
  routers/
    auth.py        # /api/auth/*
    cases.py       # /api/cases/*
    matters.py     # /api/matters/*, chat
    uploads.py     # /api/upload, /api/files
    audio.py       # /api/audio
    outputs.py     # /api/outputs, /api/export
    admin.py       # /api/admin/*
    setup.py       # /api/setup/*
  main.py          # App factory, middleware, lifespan
```

### 5. No Hybrid Search (BM25 + Vector) — FIXED

**File**: `web/rag.py`
**Issue**: Pure vector similarity search misses keyword-exact matches (case numbers, statute citations, proper nouns). Legal work is keyword-heavy.
**Fix**: Implement hybrid search — combine ChromaDB vector scores with BM25 keyword scores (e.g., via SQLite FTS5 or a lightweight BM25 implementation). Weight: 0.6 vector + 0.4 keyword is a good starting point for legal content.

### 6. No Reranking Step

**File**: `web/rag.py`
**Issue**: Top-N chunks go straight to the LLM without reranking. Embedding similarity doesn't always correlate with answer relevance.
**Fix**: Add a cross-encoder reranker after initial retrieval. Options:
- Small local reranker model via Ollama (e.g., `bge-reranker-base`)
- Simple TF-IDF reranking as a lightweight fallback
- Retrieve top-20, rerank to top-5

### 7. Embed Token Batching Has a Flush Gap — *OPEN*

**Files**: `web/rag.py`, `web/indexer.py`
**Issue**: Embed tokens are batched and flushed every 50 calls. If the process exits before 50 embeddings accumulate (e.g., small upload, graceful shutdown), those tokens are never logged.
**Fix**: Add an `atexit` handler or flush on `SIGTERM`/`SIGINT` to write remaining buffer. Also flush at the end of each indexing job.

### 8. Rate Limiting Should Survive Restarts

**File**: `web/main.py`
**Issue**: Rate buckets are in-memory deques. A restart resets all limits, allowing burst abuse.
**Fix**: For a single-node system, a simple approach is to use SQLite: `INSERT INTO rate_events (user_id, ts)` and `SELECT COUNT(*) WHERE ts > now() - 60`. Or use a small on-disk LRU. This also enables rate limit history in the usage dashboard.

---

## Medium — Quality & Maintainability

### 9. Chunking Strategy Could Be Smarter

**File**: `web/indexer.py`
**Issue**: Fixed sliding-window chunking (1200 chars, 200 overlap) splits mid-sentence and mid-paragraph. Legal documents have strong structural boundaries (sections, clauses, numbered paragraphs).
**Fix**: Implement structure-aware chunking:
- Split on paragraph boundaries first
- Merge small paragraphs up to chunk size
- Only fall back to sliding window for giant paragraphs
- Preserve section headers in chunk metadata for better retrieval context

### 10. Frontend State Should Use URL Routing

**File**: `web/static/app.js`
**Issue**: Views are toggled by CSS class; no URL state. Can't bookmark, share, or back-button to a specific view/matter.
**Fix**: Use `history.pushState()` / `popstate` for basic client-side routing:
```
/#/chat/matter/42     → Chat view, matter 42 selected
/#/cases              → Cases view
/#/admin/logs         → Admin log viewer
```
Minimal code, major UX win.

### 11. No Automated Tests

**Issue**: Zero test files found in the codebase. For a system handling legal documents, this is risky.
**Fix**: Start with high-value integration tests:
- Auth flow (login, token validation, admin guard)
- Upload + index + query round-trip
- RAG retrieval accuracy (known document → expected chunks)
- Deadline extraction (known input → expected JSON)
- Rate limiting behavior

Use `pytest` + `httpx.AsyncClient` for FastAPI testing. Even 20-30 tests would catch regressions.

### 12. Audio Hardcoded to English — FIXED

**File**: `web/audio.py`
**Issue**: `language="en"` is hardcoded in the Whisper transcription call.
**Fix**: Make it a config parameter (`WHISPER_LANGUAGE`) or auto-detect (Whisper supports `language=None` for auto-detection).

### 13. No Connection Pooling for Ollama/ChromaDB

**Files**: `web/rag.py`, `web/indexer.py`
**Issue**: Every Ollama/ChromaDB call creates a new `requests.post()` connection. Under concurrent load this creates connection churn.
**Fix**: Use `requests.Session()` with connection pooling (keep-alive). The telemetry server already does this correctly with `httpx.AsyncClient()`.

### 14. Telemetry Server Has No Persistence

**File**: `telemetry/server/server.py`
**Issue**: All node state and alerts are in-memory. Server restart loses all history.
**Fix**: Add SQLite persistence for alerts and node history. Enables historical dashboards, alert review, and uptime tracking.

### 15. DOCX Export Logic Lives in main.py

**File**: `web/main.py` (export_memo endpoint)
**Issue**: The markdown-to-DOCX conversion with metadata table, formatting, and styling is complex and lives inline in the route handler.
**Fix**: Extract to `web/export.py` as a standalone module. Makes it testable and reusable (e.g., for batch export, CLI export).

---

## Low — Polish & Future-Proofing

### 16. Consider Async Ollama Calls

**Files**: `web/rag.py`, `web/indexer.py`
**Issue**: Ollama calls use synchronous `requests` library. In a FastAPI async context, this blocks the event loop thread.
**Fix**: Migrate to `httpx.AsyncClient` for Ollama calls in the async streaming path. The sync calls (briefs, deadlines) can remain sync since they run in background threads.

### 17. ChromaDB Collection Discovery Is Expensive

**File**: `web/rag.py`
**Issue**: `retrieve()` with scope `"all"` tries to query every `case_*_docs` collection. As case count grows, this becomes N+1 queries to ChromaDB.
**Fix**: Cache the list of active collections (refresh every 60s or on case creation). Or maintain a registry in SQLite.

### 18. Setup Wizard Config Format Inconsistency

**Issue**: `sherlock.conf` is KEY=VALUE, `agent.conf` is INI with `[agent]` section, `server.conf` is KEY=VALUE. Three config formats across one system.
**Fix**: Standardize on one format. INI with sections is the most flexible and already supported by Python's `configparser`.

### 19. No Health Endpoint for the Web App

**File**: `web/main.py`
**Issue**: The web app has `/api/admin/status` but it requires admin auth. There's no unauthenticated health check for load balancers or monitoring.
**Fix**: Add `GET /health` (no auth) returning `{"status": "ok", "uptime": ...}`. The telemetry agent currently checks by hitting port 3000 — a dedicated endpoint is cleaner.

### 20. Output Mirror Failures Are Silent

**File**: `web/outputs.py`
**Issue**: Mirror write failures are logged as warnings but the user gets no indication that their NAS mirror failed.
**Fix**: Return mirror status in the save response: `{"saved": true, "mirrors": [{"path": "/nas/...", "ok": true}, ...]}`. Let the frontend show a warning toast if any mirror failed.

### 21. Consider Moving to Native Ollama — FIXED

**Current**: Ollama runs in Docker on port 11435.
**Issue**: Docker on macOS cannot access Metal GPU acceleration. This was already identified — native Ollama on port 11434 gave 4x embedding speedup and 2.4x LLM speedup.
**Fix**: Move Ollama to native macOS install with launchd management. Update `docker-compose.yaml` to remove the Ollama container. Update `config.py` default to port 11434.

---

## Architecture Smell Summary

| Smell | Where | Severity |
|-------|-------|----------|
| God file | main.py (1850 lines) | Medium |
| In-memory state loss | Jobs, rate limits | High |
| No tests | Entire project | Medium |
| Hardcoded values | Audio language, chunk size, alert thresholds | Low |
| Format inconsistency | Config files (3 formats) | Low |
| Sync HTTP in async context | rag.py Ollama calls | Low |
| No hybrid search | rag.py retrieval | High |
| Missing auth on health check | main.py | Low |

---

*These recommendations are ordered by impact-to-effort ratio. Items 1-3 should be addressed before any customer deployment. Items 4-8 will significantly improve reliability and retrieval quality. Items 9-21 are incremental improvements that can be tackled as the system matures.*
