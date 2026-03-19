# Sherlock — Architecture & Design Document

> **Purpose**: This document provides a complete technical breakdown of the Sherlock system for
> architectural review, improvement analysis, and codebase audit by an AI reviewer.
>
> **Last Updated**: 2026-03-18
> **Author**: Generated from live codebase analysis

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Deployment Architecture](#2-deployment-architecture)
3. [Service Inventory](#3-service-inventory)
4. [Backend Application (FastAPI)](#4-backend-application-fastapi)
5. [RAG Pipeline](#5-rag-pipeline)
6. [Document Indexing Engine](#6-document-indexing-engine)
7. [Database Schema](#7-database-schema)
8. [Authentication & Authorization](#8-authentication--authorization)
9. [Frontend Architecture](#9-frontend-architecture)
10. [Telemetry & Monitoring](#10-telemetry--monitoring)
11. [Logging & Observability](#11-logging--observability)
12. [Configuration Management](#12-configuration-management)
13. [Token Tracking & Billing](#13-token-tracking--billing)
14. [Network & Security](#14-network--security)
15. [Cross-Cutting Patterns](#15-cross-cutting-patterns)
16. [Known Design Decisions & Tradeoffs](#16-known-design-decisions--tradeoffs)
17. [File Manifest](#17-file-manifest)

---

## 1. System Overview

Sherlock is a **fully air-gapped, local RAG (Retrieval-Augmented Generation) system** purpose-built for law firms. It runs entirely on-premise — no cloud APIs, no data exfiltration. All AI inference (LLM, embeddings, audio transcription) happens locally via Ollama.

### Core Capabilities

- **Document ingestion**: PDF, DOCX, DOC, XLSX, PPTX, TXT, HTML, RTF, images (OCR), audio (Whisper), email (.eml) — GB-scale with hash-based deduplication
- **Semantic search**: ChromaDB vector store with overlapping chunked embeddings
- **Conversational AI**: Streaming chat with source citations, multiple query modes, role-based verbosity
- **Case management**: Hierarchical Case → Matter → Message model with isolated document collections
- **Internet research**: Optional SearXNG integration for web-augmented queries
- **Output management**: Save responses to primary + NAS mirror paths, Word/DOCX export
- **Audio transcription**: Local Whisper (faster-whisper) with in-UI transcript editing
- **Deadline extraction**: LLM-powered structured deadline parsing from legal documents
- **Matter briefs**: Auto-generated executive summaries with risk assessments
- **Telemetry**: Remote monitoring with system metrics, service health, alerts, file transfer, remote control
- **Token accounting**: Per-user and per-system-operation token tracking for future billing

### Target Deployment

- **Hardware**: Mac Mini (M-series Apple Silicon)
- **Network**: LAN-only, air-gapped; optional Tailscale tunnel for remote telemetry
- **Users**: Small law firm (5-50 users), admin + user roles
- **Scale**: Thousands of documents, dozens of concurrent users

---

## 2. Deployment Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Mac Mini (Host)                             │
│                                                                     │
│  ┌─────────────────┐   ┌──────────────────────────────────────────┐│
│  │   Nginx (443)   │   │         Docker Compose                   ││
│  │   TLS reverse   │   │  ┌──────────┐ ┌────────┐ ┌───────────┐ ││
│  │   proxy         │──▶│  │  Ollama   │ │ChromaDB│ │  SearXNG  │ ││
│  │   (native)      │   │  │  :11434   │ │ :8000  │ │  :8888    │ ││
│  └─────────────────┘   │  └──────────┘ └────────┘ └───────────┘ ││
│          │              └──────────────────────────────────────────┘│
│          ▼                        ▲  ▲  ▲                          │
│  ┌─────────────────┐              │  │  │                          │
│  │ Sherlock Web App │─────────────┘──┘──┘                          │
│  │ FastAPI :3000    │                                              │
│  │ (launchd native) │──▶ SQLite DB                                 │
│  └─────────────────┘──▶ Filesystem (NAS mounts, uploads, outputs)  │
│                                                                     │
│  ┌─────────────────┐        ┌────────────────────┐                 │
│  │ Telemetry Agent │───────▶│ Telemetry Server   │                 │
│  │ :9100 (local)   │  HTTP  │ :9200 (local/remote)│                │
│  └─────────────────┘        └────────────────────┘                 │
│          │                           │                              │
│          └─────── Tailscale ─────────┘ (optional encrypted tunnel)  │
└─────────────────────────────────────────────────────────────────────┘
```

### Hybrid Container + Native Design

| Component | Runtime | Reason |
|-----------|---------|--------|
| Ollama | Native (launchd) | Metal GPU acceleration, 2-4x faster than Docker on macOS |
| ChromaDB | Docker | Isolated persistence, reproducible |
| SearXNG | Docker | Complex dependencies, config isolation |
| Sherlock Web | Native (launchd) | Needs filesystem access (NAS mounts, SQLite, uploads) |
| Nginx | Native | TLS termination, static file serving |
| Telemetry Agent | Native | Needs psutil access to host metrics |
| Telemetry Server | Native | Can run on separate machine |

### Startup Order

```
1. Docker daemon
2. docker-compose up (Ollama → ChromaDB → SearXNG)
3. Wait for health checks (Ollama /api/tags, ChromaDB /api/v2/heartbeat)
4. Sherlock Web App (launchd: com.sherlock.web)
5. Nginx reverse proxy
6. Telemetry Agent (optional)
```

Managed by `restart.sh` which handles teardown and ordered startup with health polling.

---

## 3. Service Inventory

| Service | Port | Protocol | Health Check | Container |
|---------|------|----------|--------------|-----------|
| Sherlock Web | 3000 | HTTP | `/api/admin/status` | No (launchd) |
| Nginx | 80/443 | HTTP/HTTPS | TCP | No (native) |
| Ollama | 11434 | HTTP | `/api/tags` | No (native) |
| ChromaDB | 8000 | HTTP | `/api/v1/heartbeat` | Yes |
| SearXNG | 8888 | HTTP | `/healthz` | Yes |
| Telemetry Agent | 9100 | HTTP | `/health` | No (native) |
| Telemetry Server | 9200 | HTTP | `/` | No (native) |
| SQLite | N/A | File | N/A | N/A |

---

## 4. Backend Application (FastAPI)

**File**: `web/main.py` (~1850 lines)
**Framework**: FastAPI with Uvicorn ASGI server
**Database**: SQLAlchemy ORM → SQLite (WAL mode)

### Route Groups

| Route Prefix | Purpose | Auth | Key Operations |
|-------------|---------|------|----------------|
| `/api/setup/*` | First-run wizard | None (guarded by admin-exists check) | Create admin, configure NAS, pull models, initial index |
| `/api/auth/*` | Authentication | None | Login → JWT token |
| `/api/cases/*` | Case CRUD | JWT | Create/list/delete cases, trigger per-case reindex |
| `/api/matters/*` | Matter management | JWT | Create/list matters, chat (SSE streaming), briefs, deadlines |
| `/api/chat` | Stateless chat | JWT | Streaming RAG query (no matter persistence) |
| `/api/upload` | File upload | JWT | Upload + background index, poll status via job_id |
| `/api/audio` | Audio transcription | JWT | Upload audio → Whisper → transcript |
| `/api/files/*` | File management | JWT | List, delete, preview uploaded files |
| `/api/outputs/*` | Saved responses | JWT | List, download, delete outputs |
| `/api/export/memo` | DOCX export | JWT | Markdown → Word document with legal formatting |
| `/api/admin/*` | Admin panel | JWT + admin role | Users, system status, logs, usage dashboard, reindex |
| `/api/history` | Query history | JWT | Recent queries for sidebar |
| `/api/nas/status` | NAS health | JWT | Check mount accessibility |
| `/api/research/status` | SearXNG check | JWT | Verify web search availability |
| `/api/preview` | Document preview | JWT | Fetch file content for in-app viewing |

### Middleware Stack

1. **CORS** — Wide-open for LAN (`allow_origins=["*"]`); acceptable for air-gapped deployment
2. **RequestLoggingMiddleware** — JSON-structured request logging with request_id context var
3. **Rate Limiting** — Sliding-window per-user (30 RPM users, 120 RPM admins); in-memory deque

### Background Threads

| Thread | Interval | Purpose |
|--------|----------|---------|
| NAS Mount Monitor | 5 min | Check NAS path accessibility, update `_nas_status` dict |
| Ollama Keep-Alive | 4 min | Ping LLM + embed models to keep them resident in GPU memory |
| Embed Model Warmup | Startup | Pre-load embedding model on first request |

### Chat Streaming (SSE)

The chat endpoint uses Server-Sent Events for real-time token streaming:

```
Client                          Server
  │  POST /api/matters/{id}/chat  │
  │  {message, scope, query_type} │
  │──────────────────────────────▶│
  │                                │── Retrieve chunks (ChromaDB)
  │                                │── Optional web search (SearXNG)
  │                                │── Build prompt + system prompt
  │                                │── POST to Ollama /api/generate (stream=True)
  │  data: {"token":"The",...}     │
  │◀──────────────────────────────│
  │  data: {"token":" court",...}  │
  │◀──────────────────────────────│
  │  ...token by token...          │
  │  data: {"done":true,           │
  │         "message_id":42,       │
  │         "token_stats":{...}}   │
  │◀──────────────────────────────│
```

Token stats in the final SSE event include: `prompt_tokens`, `completion_tokens`, `total_tokens`, `tokens_per_sec`, `latency_llm_ms`, `latency_total_ms`.

### DOCX Export Pipeline

Markdown response → python-docx document with:
- Metadata table (matter, date, user, query)
- Bold/italic inline formatting preserved
- Bullet lists, headers, paragraph breaks
- Legal-style formatting (serif font, professional layout)

---

## 5. RAG Pipeline

**File**: `web/rag.py` (~620 lines)

### Architecture

```
User Query
    │
    ▼
┌──────────┐   ┌──────────────┐   ┌─────────────┐
│ Embed    │──▶│  ChromaDB    │──▶│  Top-N      │
│ Query    │   │  Retrieval   │   │  Chunks     │
│ (cached) │   │  (multi-     │   │  (scored)   │
└──────────┘   │   collection)│   └──────┬──────┘
               └──────────────┘          │
                                         ▼
                              ┌──────────────────┐
                              │  Optional Web    │
                              │  Search (SearXNG)│
                              └────────┬─────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │  Prompt Assembly │
                              │  (context + web  │
                              │   + system rules)│
                              └────────┬─────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │  Ollama LLM      │
                              │  (streaming)     │
                              │  token → client  │
                              └──────────────────┘
```

### Embedding

- **Model**: `mxbai-embed-large` (via Ollama `/api/embeddings`)
- **Input truncation**: 8192 chars max
- **Caching**: LRU cache (256 entries) on `embed_query()` — normalized to lowercase
- **Token batching**: Embed token counts accumulated in buffer; flushed to DB every 50 calls

### Multi-Scope Retrieval

The `retrieve()` function queries multiple ChromaDB collections based on scope:

| Scope | Collections Queried |
|-------|-------------------|
| `"all"` | `sherlock_cases` (global) + `user_{id}_docs` + all `case_*_docs` |
| `"global"` | `sherlock_cases` only |
| `"user"` | `user_{id}_docs` only |
| `"both"` | `sherlock_cases` + `user_{id}_docs` |
| `"case_{id}_docs"` | Direct collection name (case-scoped matter) |

- **Top-N**: Default 5 results (configurable via `RAG_TOP_N`)
- **Scoring**: `score = 1 - cosine_distance` (0-1 range, higher = more relevant)
- **Deduplication**: By collection name to avoid querying the same collection twice

### System Prompt Engineering

The system prompt is assembled from three layers:

1. **Base System** (`_BASE_SYSTEM`): ~450 words of absolute rules covering response structure, legal tone, citation requirements, prohibited behaviors
2. **Query Type Directive** (`_QUERY_TYPE_DIRECTIVES`): Injected per query type
   - `auto`: Sherlock determines format
   - `summary`: Document review focus
   - `timeline`: Chronological event extraction
   - `risk`: Red flags, deadlines, liability
   - `drafting`: Legal drafting support
3. **Verbosity Modifier** (`_VERBOSITY_MODIFIERS`): Adjusts depth/style
   - `attorney`: Dense, citation-heavy, assumes legal expertise
   - `associate`: Full IRAC analysis, educational
   - `paralegal`: Task-oriented, bullet format
   - `client`: Plain English, no jargon

### Prompt Assembly

```
[Context Documents]
[Doc: filename | Chunk 3 | Relevance: 0.87]
{chunk text}
...

[Web Results] (if research mode)
[Web: https://example.com]
Title: ...
{snippet}

[Question]
{user's query}
```

### Specialized Operations

| Function | Purpose | Streaming | Temperature |
|----------|---------|-----------|-------------|
| `stream_response()` | Main chat | Yes | Default |
| `extract_deadlines()` | Structured deadline JSON | No | 0.0 |
| `generate_brief()` | Matter summary + risks (2 passes) | No | 0.05 |
| `query_sync()` | Non-streaming query | No | Default |

### Web Search Integration

- **Engine**: Local SearXNG instance (localhost:8888)
- **Activation**: Per-query `research_mode` toggle
- **Graceful degradation**: Returns `[]` if SearXNG unreachable; query continues with doc-only context
- **Source attribution**: Web results marked with `"web": True` in sources array

---

## 6. Document Indexing Engine

**File**: `web/indexer.py` (~450 lines)

### Supported Formats

| Category | Extensions | Extraction Method |
|----------|-----------|-------------------|
| Text | .txt, .md, .rst, .log, .csv, .tsv | Direct read; CSV/TSV → pipe-delimited |
| PDF | .pdf | pypdf; OCR fallback (pytesseract) for blank pages |
| Word | .docx | python-docx (paragraphs + tables) |
| Legacy Word | .doc | LibreOffice subprocess (60s timeout) |
| Excel | .xlsx | openpyxl read-only |
| Legacy Excel | .xls | LibreOffice subprocess |
| PowerPoint | .pptx | python-pptx (slide-by-slide) |
| Legacy PPT | .ppt | LibreOffice subprocess |
| HTML | .html, .htm | Regex tag stripping (no BeautifulSoup) |
| RTF | .rtf | Regex control word stripping |
| Image | .jpg, .png, .tiff, .bmp, .gif | pytesseract OCR |
| Audio | .mp3, .wav, .m4a, .ogg, .flac, .aac | faster-whisper (beam=5, VAD filter) |
| Email | .eml | Python email module (headers + text/plain parts) |

### Chunking Strategy

- **Chunk size**: 1200 chars (~300 tokens)
- **Overlap**: 200 chars (17% overlap)
- **Method**: Sliding window, strips empty chunks

### Three-Stage Deduplication

```
For each file:
  1. stat() → mtime changed? (cheapest check — filesystem syscall)
     └─ No → skip entirely
  2. SHA-256 hash → content changed? (read file, but no processing)
     └─ No → skip entirely
  3. Extract → embed → upsert (only truly new/changed files)
```

This optimization order minimizes expensive operations: stat() is O(1), hashing is O(file_size), extraction+embedding is O(chunks × model_latency).

### Collection Isolation

| Collection | Contents | Created By |
|-----------|----------|------------|
| `sherlock_cases` | Global NAS documents (firm-wide) | Admin NAS reindex |
| `case_{id}_docs` | Per-case NAS folder | Case creation / reindex |
| `user_{id}_docs` | User-uploaded files | File upload |

### Job System

Long-running indexing operations use a job-based pattern:

```
Client                          Server
  │  POST /api/upload (file)      │
  │──────────────────────────────▶│
  │  {"job_id": "abc-123"}        │
  │◀──────────────────────────────│  (immediate response)
  │                                │
  │                                │── Background thread: extract → chunk → embed → store
  │                                │
  │  GET /api/upload/abc-123/status│
  │──────────────────────────────▶│
  │  {"done":false, "status":"indexing", "indexed":3}
  │◀──────────────────────────────│
  │                                │
  │  (poll every 2.5s)             │
  │                                │
  │  {"done":true, "indexed":12}   │
  │◀──────────────────────────────│
```

### Background Scheduler

**File**: `web/run_indexer.py` — Runs via launchd every 30 minutes

**Two-pass strategy**:
1. **Per-case indexing**: Queries DB for active cases with `nas_path`; indexes each into `case_{id}_docs`
2. **Global indexing**: Indexes `NAS_PATHS` into `sherlock_cases` (catch-all for unorganized files)

---

## 7. Database Schema

**Engine**: SQLite with WAL mode (better concurrent reads)
**ORM**: SQLAlchemy with declarative models
**File**: `web/models.py`
**Path**: `~/Sherlock/data/sherlock.db`

### Entity Relationship Diagram

```
┌──────────┐       ┌──────────┐       ┌──────────┐
│   User   │──1:N─▶│  Matter  │──1:N─▶│ Message  │
│          │       │          │       │          │
│ id (PK)  │       │ case_id ─┼──FK──▶│ sources  │
│ username │       │ user_id  │       │ (JSON)   │
│ role     │       └──────────┘       └──────────┘
│ active   │              │
└──────────┘              │
     │                    ▼
     │            ┌──────────────┐
     │            │ MatterBrief  │
     │            │ (1:1 Matter) │
     │            │ brief_md     │
     │            │ risks_md     │
     │            │ msg_count    │ ← staleness detector
     │            └──────────────┘
     │
     ├──1:N──▶┌──────────┐
     │        │  Upload   │
     │        │ chroma_ids│ ← enables bulk vector deletion
     │        │ status    │ ← pending|indexing|ready|error
     │        └──────────┘
     │
     ├──1:N──▶┌──────────┐
     │        │  Output   │
     │        │ file_path │ ← always points to primary (not mirror)
     │        └──────────┘
     │
     └──1:N──▶┌──────────┐
              │ QueryLog  │
              │ user_id   │
              │ source    │ ← "user" | "system:brief" | "system:embed" | ...
              │ *_tokens  │ ← prompt, completion, total, tok/s
              └──────────┘

┌──────────┐       ┌─────────────┐
│   Case   │──1:N─▶│   Deadline  │
│          │       │ dl_type     │
│ nas_path │       │ urgency     │
│ status   │       │ date_str    │
│ indexed_ │       └─────────────┘
│  count   │
└──────────┘

┌─────────────┐
│ IndexedFile  │ ← deduplication tracking
│ file_path   │ (unique, indexed)
│ file_hash   │ (SHA-256)
│ mtime       │
│ chunk_count │
│ collection  │
│ case_id     │
└─────────────┘
```

### Key Design Decisions

- **JSON-in-SQLite**: `Message.sources` and `Upload.chroma_ids` stored as JSON TEXT — avoids join tables, keeps schema simple
- **MatterBrief.msg_count**: Staleness detection without querying messages table; regenerate if count diverges
- **IndexedFile.mtime as string**: Simple equality check, no datetime parsing needed
- **SYSTEM_USER_ID = 999999**: Avoids autoincrement conflicts; `active=False` prevents login
- **Cascade deletes**: User → Matters → Messages, User → Uploads, User → Outputs

### Auto-Migration

`init_db()` handles schema evolution without Alembic:
- `Base.metadata.create_all()` for new tables
- `ALTER TABLE` for new columns on existing tables (token columns, source column)
- System user creation (id=999999, `_sherlock_system`)

---

## 8. Authentication & Authorization

**File**: `web/auth.py`

### Design

- **Stateless JWT**: No session table; all auth info in token payload (`sub`, `username`, `role`, `exp`)
- **Password hashing**: bcrypt with 12 rounds
- **Token expiry**: 8 hours (configurable via `JWT_EXPIRY_HOURS`)
- **Algorithm**: HS256 with symmetric secret

### FastAPI Dependency Chain

```
oauth2_scheme (extract Bearer token)
    └─▶ get_current_user (decode JWT, fetch User from DB, check active)
            └─▶ require_admin (check role == "admin")
```

### Rate Limiting

```python
# Sliding window: deque of timestamps per user_id
_rate_buckets: dict[int, deque[float]]

# Check: count entries in last 60 seconds
# Users: 30 RPM, Admins: 120 RPM
```

- In-memory only (resets on restart)
- Admin accounts have 4x the rate limit
- Returns HTTP 429 on breach

---

## 9. Frontend Architecture

**Stack**: Vanilla HTML + CSS + JavaScript (no framework, no build step)
**Files**: `web/static/index.html`, `web/static/app.js` (~1680 lines), `web/static/style.css` (~1660 lines)

### Design System

```css
--bg:       #0f1117    /* Dark slate background */
--surface:  #1a1d27    /* Card/panel surface */
--gold:     #c9a84c    /* Accent (Sherlock brand) */
--danger:   #e05c5c    /* Errors/destructive */
--success:  #4caf7a    /* Success states */
--font:     Georgia, serif           /* Document text */
--font-ui:  -apple-system, sans-serif /* UI chrome */
```

Professional legal aesthetic: dark theme, gold accents, serif for document content.

### Application Shell

```
┌─────────────────────────────────────────────────────────┐
│  Header: Logo | Nav Buttons | User Pill | Logout        │
├────────────┬────────────────────────────────────────────┤
│            │                                            │
│  Sidebar   │            Main Content Area               │
│  (chat     │  ┌────────────────────────────────────┐    │
│   view     │  │  View: chat | cases | upload |     │    │
│   only)    │  │         outputs | admin | config   │    │
│            │  │                                    │    │
│  Matters   │  │  (one visible at a time)           │    │
│  History   │  │                                    │    │
│            │  └────────────────────────────────────┘    │
└────────────┴────────────────────────────────────────────┘
```

### State Management

Single `state` object in `app.js`:

```javascript
const state = {
  token,               // JWT (localStorage)
  user,                // User object (localStorage)
  matters: [],         // Loaded on chat view
  cases: [],           // Loaded on cases view
  activeMatterId,      // Currently selected matter
  scope: 'all',        // Query scope: "all" | "case"
  queryType: 'auto',   // "auto"|"summary"|"timeline"|"risk"|"drafting" (localStorage)
  verbosityRole: 'attorney',  // (localStorage)
  researchMode: false, // Web search toggle
  streaming: false,    // Active chat stream
  abortController,     // For stop button
  uploadJobs: {},      // Active upload polling
};
```

### View Switching

```javascript
const VIEWS = ['chat', 'cases', 'upload', 'outputs', 'admin', 'config'];

function showView(name) {
  // 1. Hide all views
  // 2. Show selected view
  // 3. Load data for that view (loadMatters, loadCases, loadAdmin, etc.)
  // 4. Toggle sidebar visibility (only in chat view)
  // 5. Stop log refresh if leaving admin view
}
```

No URL routing — views are toggled by CSS class. This is intentional for LAN-only use (no bookmarking needed).

### Chat UI Flow

1. User types query → Enter or Send button
2. User bubble appended to chat
3. Typing indicator (animated dots)
4. `fetch()` with `AbortController.signal` for stop button
5. Stream response via `ReadableStream.getReader()`
6. Parse `data: {JSON}\n\n` lines, accumulate tokens
7. On `done`: render sources, action buttons, token stats bar
8. Auto-scroll to bottom, refresh history sidebar

### File Drag-and-Drop

When files are dropped into the chat area:
1. Upload all files via `/api/upload` (FormData)
2. Poll `/api/upload/{job_id}/status` until all indexed
3. Show interactive prompt card in chat:
   - File chips showing uploaded filenames
   - "What would you like me to do with these files?"
   - Quick-action pills: **Summarize**, **Compare**, **Timeline**, **Risk Review**
   - Free-form text input for custom instructions
4. Pill click or Enter → sends query referencing specific uploaded files

### Key Frontend Patterns

- **SSE streaming**: `fetch()` → `ReadableStream.getReader()` → line-by-line JSON parsing
- **Job polling**: `setInterval` with timeout for upload/index/transcription jobs
- **Modals**: `.modal-overlay.hidden` toggled by `openModal(id)` / `closeModal(id)`
- **Toast notifications**: Fixed bottom-right, auto-dismiss after 3s
- **Auto-resize textarea**: Grows with content, capped at viewport height
- **Preview panel**: Slide-in from right (52% width) for document viewing

---

## 10. Telemetry & Monitoring

### Agent (`telemetry/agent.py`, ~530 lines)

Runs on each Sherlock node, collects metrics, posts heartbeats to central server.

**Metrics collected every 30s**:

| Category | Metrics |
|----------|---------|
| System | CPU %, core count, frequency, RAM total/used/%, disk total/used/%, temperature, uptime, load averages |
| Services | HTTP health check for: sherlock_web (:3000), ollama (:11434), chromadb (:8000), searxng (:8888), nginx (:443 TCP) |
| App Metrics | 5-minute rolling window from JSON logs: request count, error count, warning count, RAG queries, avg/p95/max latency |

**Command API (port 9100)**:

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Current metrics snapshot (no auth) |
| `POST /cmd/service` | Start/stop/restart services via launchctl |
| `POST /cmd/reboot` | System reboot (1-minute delay) |
| `POST /cmd/reindex` | Trigger document reindexing |
| `GET /cmd/logs` | Fetch log streams with filters |
| `POST /cmd/file/push` | Upload file to node (restricted to Sherlock base dir) |
| `GET /cmd/file/pull` | Download file from node (restricted to Sherlock base dir) |

### Server (`telemetry/server/server.py`, ~440 lines)

Central aggregator with dashboard. Can run locally or on remote monitoring host.

**In-memory stores**:
- `nodes: dict[str, dict]` — Latest heartbeat per node
- `alerts: deque[dict]` — Last 1000 alerts (ring buffer)
- `_cpu_history: dict[str, deque[float]]` — Last 10 CPU readings per node
- `_error_ts: dict[str, deque[float]]` — Error timestamps per node

**Alert Rules**:

| Condition | Severity | Trigger |
|-----------|----------|---------|
| Service reported down | Warning | Any service `up: false` |
| CPU > 90% for 3+ heartbeats | Warning | Sustained high CPU |
| RAM > 85% | Warning | Single reading |
| Disk > 90% on any mount | Critical | Single reading |
| Error spike > 10 in 5 min | Warning | Rolling window |
| No heartbeat for 2-5 min | Warning | Dead-man switch |
| No heartbeat for > 5 min | Critical | Node offline |

**Webhook**: Configurable at runtime; fires async on every alert (Slack-compatible payload).

### Dashboard (`telemetry/server/static/dashboard.html`)

Dark theme matching Sherlock branding:
- Node grid with CPU/RAM/disk gauges and service health pills
- Alert sidebar (time-ordered, color-coded by severity)
- Modals: Log viewer, command console, file transfer, webhook config
- Auto-refresh every 5 seconds

---

## 11. Logging & Observability

**File**: `web/logging_config.py`

### Four Log Streams

| File | Size Limit | Backups | Content |
|------|-----------|---------|---------|
| `app.log` | 50 MB | 5 | HTTP requests, startup, general events |
| `audit.log` | 200 MB | 10 | Compliance trail: logins, file access, config changes |
| `rag.log` | 50 MB | 5 | Every RAG query: latency, scope, sources, scores, tokens |
| `indexer.log` | 100 MB | 5 | Indexing jobs: files, chunks, timings, errors |

### JSON Format

```json
{
  "ts": "2026-03-18T14:30:00.123Z",
  "level": "INFO",
  "logger": "sherlock.rag",
  "msg": "rag_query_done",
  "rid": "a1b2c3d4",
  "user_id": 1,
  "query": "What are the filing deadlines...",
  "latency_total_ms": 2340,
  "tokens_per_sec": 33.8,
  "prompt_tokens": 1200,
  "completion_tokens": 450
}
```

### Request Tracing

- `request_id_var`: Python `contextvars.ContextVar` set by middleware
- 8-character random hex ID per HTTP request
- Propagated to all log statements within that request
- Enables end-to-end debugging across log streams

### Console Formatter

Colorized output for development:
```
14:30:00 [INFO] sherlock.rag: rag_query_done user_id=1 latency_total_ms=2340
```

Colors: DEBUG=white, INFO=cyan, WARNING=yellow, ERROR=red, CRITICAL=magenta

### Log Viewer (Admin UI)

The admin panel includes a real-time log viewer:
- Stream selector: app | audit | rag | indexer
- Level filter: All | INFO | WARNING | ERROR
- Search filter (debounced)
- Live mode: polls every 3s
- Download button for full log files

---

## 12. Configuration Management

**File**: `web/config.py`

### Configuration Cascade

```
Priority: Environment Variable > sherlock.conf > Hardcoded Default
```

**Main config file**: `~/Sherlock/sherlock.conf` (KEY=VALUE format)

### Key Configuration

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `CHROMA_URL` | `http://localhost:8000` | ChromaDB endpoint |
| `DB_PATH` | `~/Sherlock/data/sherlock.db` | SQLite database |
| `OUTPUTS_DIR` | `~/Sherlock/outputs` | Primary output directory |
| `UPLOADS_DIR` | `~/Sherlock/uploads` | Upload staging directory |
| `NAS_PATHS` | (empty) | Comma-separated NAS mount paths |
| `OUTPUT_MIRROR_PATHS` | (empty) | Additional output destinations (NAS) |
| `LLM_MODEL` | `sherlock-rag` | Ollama model for chat/generation |
| `EMBED_MODEL` | `mxbai-embed-large` | Ollama model for embeddings |
| `WHISPER_MODEL` | `medium` | faster-whisper model size |
| `RAG_TOP_N` | `5` | Number of context chunks per query |
| `JWT_SECRET` | (insecure default) | JWT signing key |
| `JWT_EXPIRY_HOURS` | `8` | Token lifetime |
| `RATE_LIMIT_RPM` | `30` | User rate limit (requests/min) |
| `RATE_LIMIT_ADMIN_RPM` | `120` | Admin rate limit |
| `MAX_UPLOAD_MB` | `500` | Maximum upload file size |

### Telemetry Configuration

**Agent** (`telemetry/agent.conf`, INI format):
```ini
[agent]
TELEMETRY_SERVER_URL = http://localhost:9200
AGENT_TOKEN = <shared secret>
NODE_NAME = sherlock-local
HEARTBEAT_INTERVAL = 30
AGENT_PORT = 9100
```

**Server** (`telemetry/server/server.conf`, KEY=VALUE):
```
AGENT_TOKEN=<shared secret>
LISTEN_PORT=9200
AGENT_SCHEME=http
AGENT_PORT=9100
```

---

## 13. Token Tracking & Billing

### Architecture

Every Ollama API call that produces tokens is tracked in `QueryLog`:

| Source | Operations | Tracking Method |
|--------|-----------|-----------------|
| `user` | Chat queries (streaming) | Token stats from final SSE chunk |
| `system:brief` | Auto-generated matter briefs | `log_system_tokens()` after each LLM call |
| `system:deadline` | Deadline extraction | `log_system_tokens()` after LLM call |
| `system:sync` | Non-streaming queries | `log_system_tokens()` after LLM call |
| `system:embed` | Document embedding (indexer + rag) | Batched — accumulates 50 calls then flushes |

### Ollama Metrics Captured

From the final response of each `/api/generate` call:
- `prompt_eval_count` → prompt tokens
- `eval_count` → completion tokens
- `eval_duration` → nanoseconds for generation → derived `tokens_per_sec`

### Per-User Attribution

- Every `QueryLog` row has `user_id` (FK to users table)
- System operations use `SYSTEM_USER_ID = 999999` (`_sherlock_system` user, `active=False`)
- The `source` column distinguishes user queries from system operations

### Usage Dashboard

The `/api/admin/usage` endpoint returns:
- Total queries and tokens for time window
- Per-user breakdown (queries + prompt/completion/total tokens)
- Daily volume chart
- Query type breakdown
- Source breakdown (user vs. system:brief vs. system:embed vs. ...)
- Average tokens/sec (generation speed)

### Frontend Display

- **Per-response**: Subtle stats bar below each AI response: `tokens in | tokens out | total | tok/s | latency`
- **Admin dashboard**: Aggregated views with per-user token ranking, source breakdown pills

### Billing Readiness

The schema supports future billing models:
- Per-1K completion tokens (user queries)
- Per-1K prompt tokens (context window cost)
- Flat per-query pricing
- System operations as overhead or separate line item
- Per-user invoicing via `user_id` + time window

---

## 14. Network & Security

### Air-Gap Model

- **No outbound internet**: All AI models, search, databases run locally
- **LAN-only access**: Nginx on 443, no port forwarding
- **Optional Tailscale**: Only for telemetry (100.x.x.x mesh VPN)

### TLS Configuration

```
Nginx → self-signed certificate
TLS 1.2 + 1.3
Ciphers: HIGH:!aNULL:!MD5
HSTS, X-Frame-Options, X-XSS-Protection headers
```

Certificate generated via `nginx/gen-cert.sh`.

### Authentication Layers

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| Web App | JWT Bearer tokens (HS256) | All `/api/*` routes |
| Telemetry | Shared Bearer token | Agent ↔ Server communication |
| Nginx | TLS only (no basic auth) | Transport encryption |
| File Transfer | Bearer token + path restriction | Agent file push/pull (Sherlock base dir only) |

### Data Isolation

- **ChromaDB collections**: Case documents isolated in `case_{id}_docs`
- **User uploads**: Isolated in `user_{id}_docs`
- **Matters**: Scoped to `user_id` — users only see their own matters
- **Admin override**: Admin can view all outputs, users, system status

---

## 15. Cross-Cutting Patterns

### 1. Background Jobs + Polling

All long-running operations (indexing, transcription, model pull) follow:
- Return `job_id` immediately
- Background thread processes work
- Client polls `GET /status/{job_id}` every 2-3s
- Job dict in memory (not persisted — lost on restart)

### 2. Graceful Degradation

| Component | Failure Mode | Behavior |
|-----------|-------------|----------|
| NAS mount | Disconnected | Warning logged, continues with cached data |
| SearXNG | Unreachable | Falls back to doc-only query (no web results) |
| LibreOffice | Timeout | Skips file, logs error, continues |
| Ollama model | Not loaded | Setup wizard offers pull button |
| Mirror path | Write failure | Logs warning, primary save still succeeds |

### 3. Hash-Based Deduplication

Files: `stat()` → `SHA-256 hash` → `extract+embed` (increasingly expensive checks)

Queries: LRU cache on embedding function (256 entries, normalized to lowercase)

### 4. Streaming SSE

Used for: chat responses, model pulling status
Pattern: `async for chunk in generator: yield f"data: {json}\n\n"`
Frontend: `ReadableStream.getReader()` → line buffer → JSON parse

### 5. Dependency Injection (FastAPI)

```
oauth2_scheme → get_current_user → require_admin
                                  → rate_limit
SessionLocal → get_db
```

Clean composition of auth, authorization, rate limiting, and database session management.

### 6. Request Tracing

`contextvars.ContextVar` → set per-request by middleware → available in all log calls → enables correlating logs across streams for a single request.

---

## 16. Known Design Decisions & Tradeoffs

### Intentional Decisions

| Decision | Rationale |
|----------|-----------|
| SQLite (not Postgres) | Single-node deployment; WAL mode handles concurrent reads; zero config |
| Vanilla JS (no React/Vue) | No build step; LAN-only means no CDN concerns; reduces complexity |
| Docker for AI services only | Ollama/ChromaDB benefit from isolation; web app needs host filesystem access |
| Self-signed TLS | Air-gapped LAN has no CA; browsers will warn but it's acceptable |
| In-memory rate limiting | Resets on restart are acceptable; no Redis dependency |
| Job tracking in-memory | Simple; jobs are ephemeral; polling timeout handles lost jobs |
| JSON-in-SQLite columns | Avoids join tables for sources/chroma_ids; acceptable for read patterns |
| No Alembic | `ALTER TABLE` in `init_db()` for simple column additions; schema is young |
| Regex HTML/RTF parsing | Avoids BeautifulSoup/lxml dependency; good enough for text extraction |
| 1200-char chunks | ~300 tokens; balances context quality vs. retrieval granularity |
| LRU embed cache | 256 entries; prevents redundant Ollama calls on repeated queries |

### Known Limitations

| Area | Limitation |
|------|-----------|
| Scaling | Single SQLite DB; no horizontal scaling |
| Job persistence | In-memory only; lost on restart |
| Rate limiting | In-memory; lost on restart |
| Audio | Hardcoded `language="en"` |
| Search | No BM25/hybrid search; pure vector similarity |
| Frontend | No URL routing; can't bookmark/share deep links |
| Config | No hot-reload; requires service restart |
| CORS | Wide open (`*`); acceptable for LAN-only |
| Session management | No logout invalidation (JWT is stateless) |

---

## 17. File Manifest

### Backend (`web/`)

| File | Lines | Purpose |
|------|-------|---------|
| `main.py` | ~1850 | FastAPI application, all HTTP routes, middleware |
| `rag.py` | ~620 | RAG pipeline: embed, retrieve, LLM stream, briefs, deadlines |
| `models.py` | ~320 | SQLAlchemy models, DB init, migrations, token logging helper |
| `config.py` | ~80 | Configuration management (env + file + defaults) |
| `auth.py` | ~120 | JWT auth, bcrypt passwords, FastAPI dependencies |
| `indexer.py` | ~450 | Multi-format document ingestion, chunking, job tracking |
| `outputs.py` | ~100 | Output saving, NAS mirroring |
| `logging_config.py` | ~180 | 4-stream JSON logging, request tracing, console formatter |
| `audio.py` | ~120 | faster-whisper transcription with job tracking |
| `create_admin.py` | ~100 | CLI user management tool |
| `run_indexer.py` | ~60 | Background indexer scheduler (launchd) |

### Frontend (`web/static/`)

| File | Lines | Purpose |
|------|-------|---------|
| `index.html` | ~450 | Main app shell, all views, modals |
| `app.js` | ~1680 | All application logic, API calls, state management |
| `style.css` | ~1660 | Complete design system, all component styles |
| `setup.html` | ~670 | 5-step setup wizard |
| `login.html` | ~75 | Login page |

### Telemetry (`telemetry/`)

| File | Lines | Purpose |
|------|-------|---------|
| `agent.py` | ~530 | Metrics collector, command API, heartbeat sender |
| `agent.conf` | ~10 | Agent configuration |
| `server/server.py` | ~440 | Central aggregator, alert engine, dashboard server |
| `server/server.conf` | ~5 | Server configuration |
| `server/static/dashboard.html` | ~920 | Monitoring dashboard UI |
| `setup-tailscale.sh` | ~160 | Tailscale VPN setup script |
| `com.sherlock.telemetry.plist` | ~25 | launchd service definition |

### Infrastructure (root)

| File | Purpose |
|------|---------|
| `docker-compose.yaml` | Container orchestration (Ollama, ChromaDB, SearXNG) |
| `Dockerfile` | ChromaDB image extension |
| `setup.sh` | Full interactive setup script |
| `restart.sh` | Ordered service restart with health checks |
| `nginx/sherlock.conf` | Nginx reverse proxy configuration |
| `nginx/gen-cert.sh` | Self-signed TLS certificate generator |
| `requirements.txt` | Python dependencies |
| `sherlock.conf` | Main application configuration |

---

## Python Dependencies

```
fastapi, uvicorn           # Web framework + ASGI server
sqlalchemy                 # ORM (SQLite backend)
python-jose[cryptography]  # JWT tokens
bcrypt                     # Password hashing
chromadb                   # Vector database client
requests, httpx            # HTTP clients (sync + async)
pypdf                      # PDF text extraction
python-docx                # Word document read/write
openpyxl                   # Excel read
python-pptx                # PowerPoint read
pytesseract, Pillow        # OCR (images + scanned PDFs)
faster-whisper             # Audio transcription
psutil                     # System metrics (telemetry agent)
```

---

*End of Architecture Document*
