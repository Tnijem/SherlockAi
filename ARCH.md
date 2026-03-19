# Sherlock — System Architecture & Requirements

> Air-gapped, offline RAG engine for law firms.
> Analyzes 100GB+ case files on Mac Mini. No internet. No cloud.

---

## Table of Contents
1. [Overview](#overview)
2. [Hardware Requirements](#hardware-requirements)
3. [System Architecture](#system-architecture)
4. [Services](#services)
5. [Feature Specifications](#feature-specifications)
6. [Data Model](#data-model)
7. [API Surface](#api-surface)
8. [Setup & Installation](#setup--installation)
9. [Air-Gap Deployment](#air-gap-deployment)
10. [File & Directory Layout](#file--directory-layout)
11. [Open Questions / Future Work](#open-questions--future-work)

---

## Overview

Sherlock is a fully local, air-gapped AI assistant for law firm staff. It indexes case files from NAS storage and allows attorneys and staff to query those files in natural language — with no data ever leaving the premises.

**Core principles:**
- 100% offline after initial setup
- All AI inference runs locally (Ollama)
- Per-user privacy: each user's uploaded docs and conversation history are isolated
- Outputs are written to a firm-designated folder for record-keeping
- No dependency on external APIs, cloud services, or internet connectivity

---

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | Apple M2 | Apple M3 Pro+ |
| RAM | 16 GB | 32 GB |
| Storage | 512 GB SSD | 2 TB SSD |
| Network | LAN only (air-gapped) | LAN only (air-gapped) |
| NAS | SMB/NFS accessible | SMB/NFS accessible |

> Mac Mini M-series is the target deployment platform.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LAN (air-gapped)                             │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │              Sherlock Web App  (port 3000)                   │  │
│  │         FastAPI backend  +  HTML/CSS/JS frontend             │  │
│  │                                                              │  │
│  │  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌──────────┐  │  │
│  │  │   Auth   │  │  Chat UI   │  │  Upload  │  │  Audio   │  │  │
│  │  │  (JWT)   │  │ (per-user) │  │  Handler │  │  Handler │  │  │
│  │  └──────────┘  └────────────┘  └──────────┘  └──────────┘  │  │
│  │        │              │               │              │        │  │
│  │        └──────────────┴───────────────┴──────────────┘       │  │
│  │                              │                                │  │
│  │                     ┌────────▼────────┐                       │  │
│  │                     │  RAG Engine     │                       │  │
│  │                     │  (query_rag.py) │                       │  │
│  │                     └────────┬────────┘                       │  │
│  └──────────────────────────────┼────────────────────────────────┘  │
│                                 │                                   │
│         ┌───────────────────────┼───────────────────────┐          │
│         │                       │                       │          │
│  ┌──────▼──────┐       ┌────────▼────────┐    ┌────────▼───────┐  │
│  │   Ollama    │       │    ChromaDB      │    │  SQLite (app)  │  │
│  │  :11434     │       │    :8000         │    │  users, convos │  │
│  │             │       │                  │    │  sessions      │  │
│  │  llama3.1:8b│       │  Global NAS coll │    └────────────────┘  │
│  │  mxbai-emb  │       │  Per-user colls  │                        │
│  │  whisper    │       │                  │                        │
│  └─────────────┘       └─────────────────┘                        │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  NAS Mounts (read-only)                  Outputs Dir         │  │
│  │  ~/Sherlock/docs/ → launchd watcher  │  (firm-configured)   │  │
│  │  Incremental indexer daemon           │  /path/to/outputs/   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Request Flow: Chat Query
```
User types query
  → Web UI (POST /api/chat)
  → Auth middleware (validate JWT)
  → RAG Engine:
      1. Embed query via Ollama (mxbai-embed-large)
      2. Query ChromaDB: user's private collection + global NAS collection
      3. Build context from top-N results
      4. Send context + query to Ollama (llama3.1:8b)
      5. Stream response back to UI
  → Append exchange to user's conversation history (SQLite)
  → If user clicks "Save Output" → write to Outputs dir
```

### Request Flow: Audio Command
```
User uploads .mp3/.wav/.m4a
  → Web UI (POST /api/audio)
  → Sherlock Web App:
      1. Save audio to temp file
      2. Transcribe via Whisper (local Ollama or faster-whisper)
      3. Display transcript to user for confirmation
      4. Treat transcript as chat query → RAG Engine (same as above)
```

### Request Flow: File Upload for Analysis
```
User uploads PDF/DOCX/TXT/image
  → Web UI (POST /api/upload)
  → Sherlock Web App:
      1. Save to user's upload staging dir
      2. Extract text (pypdf / python-docx / pytesseract)
      3. Chunk text, embed via Ollama, store in user's private ChromaDB collection
      4. Confirm to UI: "File indexed, ready to query"
  → User can now query against this file in their chat session
```

---

## Services

### Docker Compose Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `sherlock-ollama` | `ollama/ollama` | 11435 | LLM inference + embeddings + Whisper |
| `sherlock-chroma` | `chromadb/chroma` | 8000 | Vector store |
| `sherlock-web` | custom (FastAPI) | 3000 | Web UI + API + auth |

> OpenWebUI is **removed** from the stack. The custom `sherlock-web` service replaces it
> with Sherlock-branded UI, per-user isolation, audio handling, and output management.

### sherlock-web (Custom FastAPI Service)
- **Backend**: Python/FastAPI — REST API + WebSocket for streaming
- **Frontend**: Vanilla HTML/CSS/JS (no build step, no npm, fully offline)
- **Auth DB**: SQLite at `/app/data/sherlock.db`
- **Branding**: Uses existing SVG logo/graphics from `branding/graphics/`

---

## Feature Specifications

### F1: Authentication

**Mechanism**: Username + password → JWT access token (stored in localStorage)

**User roles**:
| Role | Capabilities |
|------|-------------|
| `admin` | Create/deactivate users, view all output files, reset passwords |
| `user` | Chat, upload files, save outputs — all scoped to own account |

**Details**:
- Passwords hashed with bcrypt (min 12 rounds)
- JWT expiry: configurable, default 8 hours (a workday)
- No password recovery flow (air-gapped — admin resets via CLI: `sherlock user reset <username>`)
- First-run: setup.sh creates the initial admin account interactively
- Session activity extends token; idle timeout after configurable period

**What is NOT in scope**:
- SSO / LDAP / Active Directory (future work)
- MFA (future work)
- Email-based password reset

---

### F2: Per-User Private Sessions & Persistent Memory

Each user has fully isolated:

1. **Conversation history** — stored in SQLite (`conversations` table, keyed by `user_id`)
   - Full message log (role, content, timestamp)
   - Grouped into named "matters" or sessions (user can create/rename/delete)
   - Persists across browser sessions and server restarts

2. **Private document collection** — ChromaDB collection named `user_{user_id}_docs`
   - Documents uploaded by this user are indexed here
   - Only this user's queries search this collection
   - Admin can optionally merge a user's collection into the global NAS index

3. **Search scope** (user-configurable per query):
   - Global NAS index only
   - My uploaded documents only
   - Both (default)

---

### F3: Web UI

**Pages/Views**:

| View | Description |
|------|-------------|
| Login | Username + password, Sherlock branding |
| Chat | Main interface — message thread, source citations, streaming response |
| Matters | List of named conversation threads; create/rename/archive |
| Upload | Drag-and-drop or file picker; shows indexing status |
| Outputs | Browse/download files saved to the Outputs dir |
| Admin | (admin only) User management, system status, index stats |

**Chat UI details**:
- Streaming responses (WebSocket or SSE)
- Source citations shown per response (filename, excerpt)
- "Copy" and "Save to Outputs" buttons on each AI response
- Search scope toggle (NAS / My Files / Both)
- Markdown rendering in responses

**Design**:
- Uses existing Sherlock branding (logo, color palette from `branding/graphics/`)
- Clean, minimal — attorneys aren't developers
- No external CDN dependencies (offline-safe); bundle any CSS/JS libs locally

---

### F4: File Upload & Analysis

**Supported formats**:
| Format | Extraction method |
|--------|------------------|
| PDF | pypdf (text layer) + pytesseract fallback for scanned pages |
| DOCX | python-docx |
| TXT / MD | direct read |
| JPG / PNG / TIFF | pytesseract OCR |
| XLSX | openpyxl → plain text extraction |
| MP3 / WAV / M4A | Whisper transcription → treated as text document |

**Upload flow**:
1. File dropped or selected in UI
2. Immediate upload to server (`/api/upload`)
3. Server returns a job ID
4. UI polls `/api/upload/{job_id}/status` for progress
5. On completion: file appears in "My Files" sidebar; user notified

**Limits** (configurable in `sherlock.conf`):
- Max file size: 500 MB per file
- Max total per user: 10 GB (admin-configurable)

**Storage**: Uploaded files saved to `{uploads_dir}/{user_id}/` (path configured at setup)

---

### F5: Outputs Folder

- Path configured during `setup.sh` (e.g., `/Volumes/NAS/Sherlock_Outputs/` or `~/Sherlock/outputs/`)
- Stored in `sherlock.conf` as `OUTPUTS_DIR`
- Structure: `{OUTPUTS_DIR}/{username}/{YYYY-MM-DD}/`

**What gets written**:
- User-saved AI responses (user clicks "Save to Outputs")
- Structured analysis exports (future: deposition summaries, timeline extracts)
- Audit log of all saves (metadata only) at `{OUTPUTS_DIR}/.audit/`

**Web UI**: "Outputs" view lets users browse and download their saved files. Admins see all users.

---

### F6: Audio File Processing

**Use case**: Attorney dictates a query ("Sherlock, pull all precedents on chain-of-custody violations in drug cases") → uploads the recording → Sherlock transcribes and responds.

**Transcription engine**:
- Primary: `whisper` model via Ollama (if Ollama adds Whisper support — check at deploy time)
- Fallback: `faster-whisper` Python library (runs locally, no internet, GPU optional)
- Model size: `medium` (good English accuracy, runs on M-series)

**Flow**:
1. User uploads audio via Upload view or dedicated audio button in Chat
2. Server transcribes (async job, shows progress)
3. Transcript displayed to user with "Edit" option before submitting
4. User confirms → transcript submitted as a chat query
5. Response returned normally; both transcript and response saved to conversation history

**Audio stored**: Yes — kept in `{uploads_dir}/{user_id}/audio/` so transcript can be regenerated if needed

---

## Data Model

### SQLite: `sherlock.db`

```sql
-- Users
CREATE TABLE users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    display_name TEXT,
    password_hash TEXT NOT NULL,           -- bcrypt
    role        TEXT DEFAULT 'user',       -- 'admin' | 'user'
    active      INTEGER DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login  DATETIME
);

-- Matters (named conversation threads)
CREATE TABLE matters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),
    name        TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    archived    INTEGER DEFAULT 0
);

-- Messages
CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id   INTEGER REFERENCES matters(id),
    user_id     INTEGER REFERENCES users(id),
    role        TEXT NOT NULL,             -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    sources     TEXT,                      -- JSON array of {file, excerpt}
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Uploaded files
CREATE TABLE uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_type   TEXT,
    status      TEXT DEFAULT 'pending',   -- 'pending'|'indexing'|'ready'|'error'
    chroma_ids  TEXT,                      -- JSON array of ChromaDB doc IDs
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Saved outputs
CREATE TABLE outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id),
    matter_id   INTEGER REFERENCES matters(id),
    message_id  INTEGER REFERENCES messages(id),
    file_path   TEXT NOT NULL,
    saved_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### ChromaDB Collections

| Collection name | Contents | Scope |
|-----------------|----------|-------|
| `sherlock_cases` | All NAS-indexed firm documents | Global (all users) |
| `user_{id}_docs` | Per-user uploaded documents | Private to that user |

---

## API Surface

All endpoints require `Authorization: Bearer {jwt}` except `/api/auth/login`.

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/login` | Login → returns JWT |
| POST | `/api/auth/logout` | Invalidate token |
| GET | `/api/auth/me` | Current user info |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/matters` | List user's matters |
| POST | `/api/matters` | Create new matter |
| PATCH | `/api/matters/{id}` | Rename/archive matter |
| GET | `/api/matters/{id}/messages` | Full conversation history |
| POST | `/api/matters/{id}/chat` | Send query → SSE stream of response |

### Files
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/upload` | Upload file → returns job_id |
| GET | `/api/upload/{job_id}/status` | Poll indexing progress |
| GET | `/api/files` | List user's uploaded files |
| DELETE | `/api/files/{id}` | Remove file from user's collection |

### Audio
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/audio` | Upload audio → returns job_id |
| GET | `/api/audio/{job_id}/status` | Poll transcription progress |

### Outputs
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/outputs` | List saved output files |
| POST | `/api/outputs` | Save a message as output file |
| GET | `/api/outputs/{id}/download` | Download output file |

### Admin (role: admin only)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/admin/users` | List all users |
| POST | `/api/admin/users` | Create user |
| PATCH | `/api/admin/users/{id}` | Activate/deactivate, reset password |
| GET | `/api/admin/status` | System health (Ollama, Chroma, index stats) |
| POST | `/api/admin/reindex` | Trigger NAS re-index |

---

## Setup & Installation

### `setup.sh` Interactive Prompts

```
Welcome to Sherlock Setup
─────────────────────────
System name (shown in UI)  [Sherlock]: _
NAS mount path(s)          [/Volumes/Cases]: _
Outputs directory          [~/Sherlock/outputs]: _
Admin username             [admin]: _
Admin password             : ****
Confirm password           : ****

Pre-downloading models (internet required — do this before air-gap):
  ✓ ollama pull llama3.1:8b
  ✓ ollama pull mxbai-embed-large
  ✓ faster-whisper model: medium (downloaded to ./models/whisper/)

Installing launchd NAS watcher...  ✓
Starting Docker services...        ✓
Running initial NAS index...       ✓

Sherlock is ready at http://localhost:3000
```

### `sherlock.conf` (generated by setup.sh)

```ini
SYSTEM_NAME=Sherlock
NAS_PATHS=/Volumes/Cases,/Volumes/Archive
OUTPUTS_DIR=/Volumes/NAS/Sherlock_Outputs
UPLOADS_DIR=~/Sherlock/uploads
JWT_SECRET=<generated 64-char hex>
JWT_EXPIRY_HOURS=8
OLLAMA_URL=http://localhost:11434
CHROMA_URL=http://localhost:8000
WHISPER_MODEL=medium
MAX_UPLOAD_MB=500
```

---

## Air-Gap Deployment

All dependencies must be pre-downloaded on an internet-connected machine and transferred via USB.

### Checklist
- [ ] `docker save sherlock-web sherlock-ollama sherlock-chroma > sherlock-images.tar`
- [ ] Ollama models exported: `llama3.1:8b`, `mxbai-embed-large`
- [ ] faster-whisper model: `medium` (download via `faster-whisper` CLI before air-gap)
- [ ] Python venv with all packages (`pip download -r requirements.txt -d ./pip-cache`)
- [ ] No CDN links in frontend HTML — all JS/CSS bundled locally
- [ ] `docker load < sherlock-images.tar` on target machine

---

## File & Directory Layout

```
~/Sherlock/
├── ARCH.md                    # This document
├── docker-compose.yaml        # Ollama + ChromaDB + sherlock-web
├── sherlock.conf              # Generated by setup.sh
├── setup.sh                   # Interactive installer (idempotent)
├── uninstall.sh               # Stops services, removes containers
│
├── web/                       # Custom web app (sherlock-web service)
│   ├── main.py                # FastAPI app entrypoint
│   ├── auth.py                # JWT auth, bcrypt, user management
│   ├── rag.py                 # RAG engine (embed → Chroma → Ollama)
│   ├── indexer.py             # File upload indexing (replaces chroma_indexer.py)
│   ├── audio.py               # Whisper transcription handler
│   ├── outputs.py             # Output file writer
│   ├── models.py              # SQLite data models (SQLAlchemy)
│   ├── static/                # Frontend (HTML/CSS/JS — no build step)
│   │   ├── index.html
│   │   ├── login.html
│   │   ├── app.js
│   │   ├── style.css
│   │   └── assets/            # Logo, icons (from branding/graphics/)
│   └── requirements.txt
│
├── branding/                  # Marketing, graphics, sizing
├── chromadb/                  # ChromaDB persistent data
├── ollama/                    # Ollama model storage
├── uploads/                   # User-uploaded files (per-user subdirs)
├── outputs/                   # Default outputs dir (if not NAS)
├── models/
│   └── whisper/               # faster-whisper model files
├── samples/                   # Test case files
└── logs/
    └── sherlock.log
```

---

## Open Questions / Future Work

| Topic | Notes |
|-------|-------|
| **LDAP/AD auth** | Law firms often have Active Directory — future integration |
| **MFA** | TOTP would add security without internet dependency |
| **Multi-node** | Single Mac Mini assumed; NAS can serve multiple offices but indexer is single-node |
| **Model upgrades** | llama3.1:8b is current choice; llama3.2 or Mistral variants may improve legal reasoning |
| **Structured outputs** | Auto-generate deposition summaries, chronologies, privilege logs as formatted docs |
| **Case-level isolation** | Currently global NAS index + per-user upload index; could add per-matter ChromaDB collections |
| **Audit logging** | Track who queried what (for ethics/compliance) — foundation is in outputs audit log |
| **Whisper via Ollama** | Ollama does not yet support Whisper natively; using faster-whisper as standalone — revisit |
