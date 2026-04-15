# Sherlock Install Guide

This guide takes you from a blank Mac Mini (or any Apple Silicon / Linux
host) to a running Sherlock deployment serving a law firm, including the
new `primary_law` knowledge base.

Target hardware: **Mac Mini M4, 32 GB** (current reference).
Linux (Debian/Ubuntu/Fedora) is supported with the same Python stack; the
docker-compose path works identically.

---

## 1. Prerequisites

| Component      | Version       | Why                               |
| -------------- | ------------- | --------------------------------- |
| Python         | 3.11+         | Sherlock web app                  |
| Docker Desktop | current       | ChromaDB container                |
| Ollama         | 0.1.40+       | Local LLM + embeddings runtime    |
| Git            | any           | Deploy pulls from git             |
| Homebrew       | current (Mac) | Installs above quickly            |

```bash
# macOS one-liner:
brew install python@3.11 docker ollama git

# Or Linux equivalent:
# apt install python3.11 python3.11-venv docker.io git && curl -fsSL https://ollama.com/install.sh | sh
```

Start Docker Desktop and Ollama (`ollama serve &`).

---

## 2. Clone & Python environment

```bash
cd ~
git clone https://github.com/<YOUR-ORG>/Sherlock.git
cd Sherlock

python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Requirements already include: `fastapi`, `uvicorn`, `chromadb`,
`pdfplumber`, `pypdf`, `pyyaml`, `python-docx`, `openpyxl`, `requests`,
`tiktoken`, and everything else the web app needs.

---

## 3. Models

Sherlock uses two Ollama models:

```bash
# Chat LLM - gemma3:12b is the current default for Mac Mini M4
ollama pull gemma3:12b

# Embeddings - mxbai-embed-large, 1024-dim, L2-normalized
ollama pull mxbai-embed-large
```

Override either via environment variables in `web/config.py` or the
admin Configuration page.

---

## 4. ChromaDB

Sherlock assumes a Chroma HTTP server at `localhost:8000`. The shipped
`docker-compose.yaml` brings it up with the right volume mount:

```bash
docker compose up -d chromadb
# verify
curl -s http://localhost:8000/api/v2/heartbeat
```

Data persists under `~/Sherlock/data/chroma/`.

---

## 5. Firm configuration

Edit two files before the first ingest:

### 5.1 `config/firm.yaml`

This is the **tenant config**. Open it and set:

```yaml
firm:
  name: "Your Firm Name"
  primary_jurisdiction: GA          # USPS code
  jurisdictions: [GA, FL]           # all states you work in
  practice_areas:                   # keys from the jurisdiction map
    - personal_injury
    - contracts
    - civil_procedure
    - estates
  case_law:
    lookback_years: 10
    max_per_court: 2000
```

See [PRIMARY_LAW.md](PRIMARY_LAW.md#21-configfirmyaml) for the full
`firm.yaml` reference.

### 5.2 `config/jurisdictions/<CODE>.yaml`

Sherlock ships with `GA.yaml` and `FL.yaml` preconfigured. If your firm
works in another state, see
[PRIMARY_LAW.md §5](PRIMARY_LAW.md#5-adding-a-new-jurisdiction) for how to
add one.

### 5.3 NAS paths

Edit `~/Sherlock/nas_paths.txt` (one path per line) with the mount points
of the firm's document share(s). These are indexed into the
`sherlock_global` collection.

---

## 6. First ingest

### 6.1 Primary law (statutes, rules, legislation)

```bash
cd ~/Sherlock
./venv/bin/python scripts/ingest_primary_law.py \
    --source-types statute,rule,legislation
```

Expected runtime on M4 32 GB:

| Source                               | Chunks   | Wall time |
| ------------------------------------ | -------- | --------- |
| OCGA titles 9, 13, 51, 53            | ~2,400   | 4–5 min   |
| FL statutes (PI / contracts / trusts)| ~1,200   | 2–3 min   |
| FL Rules of Civil Procedure          | ~500     | 10–12 min |
| FL HB 837                            | ~40      | 30 s      |

(FRCP/legislation are slow because each PDF chunk goes to Ollama one at
a time. Statute chapters fetch in parallel.)

### 6.2 Case law (CourtListener)

Case law is separated because it's rate-limited (anonymous: ~60 req/hr;
authenticated: 5,000 req/day). Register at
https://www.courtlistener.com/sign-up/ for a token.

```bash
export COURTLISTENER_TOKEN=your_token_here
./venv/bin/python scripts/ingest_primary_law.py --source-types case
```

Runtime scales linearly with `case_law.max_per_court × len(courts)`.
At 2,000 per court × 4 courts (GA + FL appellate), count on ~90 min the
first time. Subsequent runs are much faster — the fetcher caches opinion
JSON on disk.

### 6.3 NAS documents

The existing NAS indexer handles the firm's own documents:

```bash
./venv/bin/python web/indexer.py --scan ~/Sherlock/nas_paths.txt
```

Or trigger from the Configuration page after the app is running.

---

## 7. Launch the app

```bash
./venv/bin/python web/app.py
# or for production:
./venv/bin/uvicorn web.app:app --host 0.0.0.0 --port 8080 --workers 2
```

Open http://localhost:8080 — the Sherlock UI should load.

Verify primary-law retrieval is wired in with a quick smoke query like:

> *"What is the Georgia statute of limitations on a written contract?"*

The assistant should cite **O.C.G.A. § 9-3-24** (6 years) and name the
section in the Sources panel. If it cites something like "O.C.G.A. §
9-12-21" or gets the years wrong, primary-law retrieval isn't hitting —
check the server logs for `primary_law:` lines and see
[Troubleshooting](PRIMARY_LAW.md#6-troubleshooting).

---

## 8. Scheduled refresh

Primary-law sources change slowly but they *do* change (new legislative
sessions, rule amendments, new appellate opinions). Schedule a nightly
refresh via cron or launchd:

```cron
# Nightly 3 AM: re-pull everything. Idempotent upserts, safe to run daily.
0 3 * * *  cd ~/Sherlock && ./venv/bin/python scripts/ingest_primary_law.py >> logs/primary_law.log 2>&1
```

Fetches hit disk cache first, so a no-change run takes seconds.

---

## 9. Upgrading Sherlock

```bash
cd ~/Sherlock
git pull
./venv/bin/pip install -r requirements.txt
# Restart the web server (uvicorn / launchd / docker, whichever you use)
```

Config files in `config/` are **never overwritten by git pull** — they're
yours. If a schema change is needed, the release notes will call it out.

---

## 10. Productization: shipping to a new firm

Sherlock is built so you can drop the same binary into a new law firm and
reconfigure it for their state and practice areas without touching code:

1. Copy `config/firm.yaml.template` → `config/firm.yaml` and fill in the
   new firm's name, state(s), practice areas.
2. Make sure `config/jurisdictions/<state>.yaml` exists for each of their
   states. If not, add one (see
   [PRIMARY_LAW.md §5](PRIMARY_LAW.md#5-adding-a-new-jurisdiction)).
3. Edit `nas_paths.txt` to point at the new firm's document share.
4. Run `scripts/ingest_primary_law.py` + NAS indexer.
5. Launch the app.

Nothing under `web/primary_law/` needs to change unless you're adding
support for a fetcher type that doesn't exist yet.

---

## 11. Known issues

- **BM25 error on startup** (`no such table: chunk_fts`): cosmetic; vector
  search still runs. The hybrid scorer falls back gracefully.
- **Ollama 500 on some PDF chunks**: `mxbai-embed-large` chokes on
  table-dense or long-form-language chunks. `_embed_text()` has a 4-stage
  length-fallback that catches most; residual failures are logged and
  skipped (typically <1% of primary-law chunks).
- **CourtListener anonymous limit**: ~60 req/hr. Always set
  `COURTLISTENER_TOKEN` before full ingests.
