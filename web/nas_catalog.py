"""
NAS File Catalog — Tier 1: fast metadata-only scan.

Walks the NAS and stores file metadata (name, path, size, mtime, type, client folder)
in SQLite for instant filename/folder search. No text extraction or embedding.

Designed to catalog 245K+ files in ~30 minutes over SMB.
"""

# This gets added as ~/Sherlock/web/nas_catalog.py

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH, NAS_PATHS
from logging_config import get_logger

log = get_logger("sherlock.catalog")

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nas_catalog (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT    UNIQUE NOT NULL,
    filename    TEXT    NOT NULL,
    extension   TEXT,
    size_bytes  INTEGER,
    mtime       REAL,
    mtime_date  TEXT,
    client_folder TEXT,
    category    TEXT,
    parent_dir  TEXT,
    scanned_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalog_filename  ON nas_catalog(filename);
CREATE INDEX IF NOT EXISTS idx_catalog_ext       ON nas_catalog(extension);
CREATE INDEX IF NOT EXISTS idx_catalog_client    ON nas_catalog(client_folder);
CREATE INDEX IF NOT EXISTS idx_catalog_category  ON nas_catalog(category);
CREATE INDEX IF NOT EXISTS idx_catalog_mtime     ON nas_catalog(mtime DESC);

-- FTS5 virtual table for fast filename/path search
CREATE VIRTUAL TABLE IF NOT EXISTS nas_catalog_fts USING fts5(
    filename, client_folder, parent_dir, category,
    content=nas_catalog,
    content_rowid=id
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS catalog_fts_insert AFTER INSERT ON nas_catalog BEGIN
    INSERT INTO nas_catalog_fts(rowid, filename, client_folder, parent_dir, category)
    VALUES (new.id, new.filename, new.client_folder, new.parent_dir, new.category);
END;

CREATE TRIGGER IF NOT EXISTS catalog_fts_delete AFTER DELETE ON nas_catalog BEGIN
    INSERT INTO nas_catalog_fts(nas_catalog_fts, rowid, filename, client_folder, parent_dir, category)
    VALUES ('delete', old.id, old.filename, old.client_folder, old.parent_dir, old.category);
END;

CREATE TRIGGER IF NOT EXISTS catalog_fts_update AFTER UPDATE ON nas_catalog BEGIN
    INSERT INTO nas_catalog_fts(nas_catalog_fts, rowid, filename, client_folder, parent_dir, category)
    VALUES ('delete', old.id, old.filename, old.client_folder, old.parent_dir, old.category);
    INSERT INTO nas_catalog_fts(rowid, filename, client_folder, parent_dir, category)
    VALUES (new.id, new.filename, new.client_folder, new.parent_dir, new.category);
END;
"""

# ── Initialization ────────────────────────────────────────────────────────────

_conn_lock = threading.Lock()

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def init_catalog():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.close()
    log.info("catalog_initialized")


# ── Category detection ────────────────────────────────────────────────────────

def _detect_category(path_parts: list[str]) -> str:
    """Detect case category from path components."""
    categories = {
        "INJURY", "CRIMINAL", "DIVORCE", "BUSINESS", "PROBATE",
        "WILLS", "WORKERS COMPENSATION", "MEDICAL MALPRACTICE", "FORMS",
    }
    for part in path_parts:
        upper = part.upper()
        if upper in categories:
            return part
    return ""


def _detect_client_folder(file_path: str, nas_root: str) -> tuple[str, str]:
    """Extract client folder name and category from file path relative to NAS root."""
    rel = os.path.relpath(file_path, nas_root)
    parts = rel.split(os.sep)

    category = _detect_category(parts)

    # Client folder is typically 2 levels deep: CATEGORY/ClientName/...
    # or 1 level: ClientName/...
    if len(parts) >= 3 and parts[0].upper() in {
        "INJURY", "CRIMINAL", "DIVORCE", "BUSINESS", "PROBATE",
        "WILLS", "WORKERS COMPENSATION", "MEDICAL MALPRACTICE",
    }:
        return parts[1], category
    elif len(parts) >= 2:
        return parts[0], category
    return "", category


# ── Scanning ──────────────────────────────────────────────────────────────────

_scan_status = {
    "active": False,
    "total_found": 0,
    "total_inserted": 0,
    "total_skipped": 0,
    "errors": 0,
    "stage": "idle",
    "started_at": None,
    "elapsed_s": 0,
}


def get_scan_status() -> dict:
    """Return current scan status."""
    s = dict(_scan_status)
    if s["started_at"]:
        s["elapsed_s"] = int(time.time() - s["started_at"])
    return s


def _scan_nas_paths(nas_paths: list[str], incremental: bool = True):
    """
    Walk NAS paths and catalog all files.

    If incremental=True (default), skip files already in catalog with same mtime.
    """
    global _scan_status
    _scan_status.update({
        "active": True,
        "total_found": 0,
        "total_inserted": 0,
        "total_skipped": 0,
        "errors": 0,
        "stage": "scanning",
        "started_at": time.time(),
    })

    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    batch = []
    BATCH_SIZE = 500

    def _flush_batch():
        if not batch:
            return
        try:
            cur.executemany("""
                INSERT OR REPLACE INTO nas_catalog
                (file_path, filename, extension, size_bytes, mtime, mtime_date,
                 client_folder, category, parent_dir, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)
            conn.commit()
            _scan_status["total_inserted"] += len(batch)
        except Exception as e:
            log.warning("catalog_flush_error: %s", e)
            _scan_status["errors"] += 1
        batch.clear()

    # Build existing file set for incremental mode
    existing = {}
    if incremental:
        _scan_status["stage"] = "loading_existing"
        try:
            cur.execute("SELECT file_path, mtime FROM nas_catalog")
            existing = {row[0]: row[1] for row in cur.fetchall()}
            log.info("catalog_existing: %d files in catalog", len(existing))
        except Exception:
            pass

    _scan_status["stage"] = "walking"

    for nas_path in nas_paths:
        root = nas_path.rstrip("/")
        if not os.path.isdir(root):
            log.warning("catalog_skip_root: %s not accessible", root)
            continue

        log.info("catalog_walking: %s", root)

        for dirpath, dirnames, filenames in os.walk(root):
            # Skip hidden dirs and recycle bins
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith('.') and d != '#recycle' and d != 'Thumbs.db'
            ]

            for fname in filenames:
                if fname.startswith('.') or fname == 'Thumbs.db' or fname == 'AUTORUN.INF':
                    continue

                _scan_status["total_found"] += 1
                fp = os.path.join(dirpath, fname)

                try:
                    st = os.stat(fp)
                except Exception:
                    _scan_status["errors"] += 1
                    continue

                mtime = st.st_mtime

                # Skip if unchanged (incremental)
                if incremental and fp in existing and abs(existing[fp] - mtime) < 1:
                    _scan_status["total_skipped"] += 1
                    continue

                ext = os.path.splitext(fname)[1].lower()
                client_folder, category = _detect_client_folder(fp, root)
                parent_dir = os.path.basename(dirpath)
                mtime_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")

                batch.append((
                    fp, fname, ext, st.st_size, mtime, mtime_date,
                    client_folder, category, parent_dir, now,
                ))

                if len(batch) >= BATCH_SIZE:
                    _flush_batch()

                # Log progress every 10K files
                if _scan_status["total_found"] % 10000 == 0:
                    log.info("catalog_progress: %d found, %d inserted, %d skipped",
                             _scan_status["total_found"],
                             _scan_status["total_inserted"],
                             _scan_status["total_skipped"])

    _flush_batch()
    conn.close()

    _scan_status.update({
        "active": False,
        "stage": "done",
        "elapsed_s": int(time.time() - _scan_status["started_at"]),
    })

    log.info("catalog_done: %d found, %d inserted, %d skipped, %d errors, %ds",
             _scan_status["total_found"], _scan_status["total_inserted"],
             _scan_status["total_skipped"], _scan_status["errors"],
             _scan_status["elapsed_s"])


def start_catalog_scan(incremental: bool = True) -> dict:
    """Start a background NAS catalog scan."""
    if _scan_status["active"]:
        return {"status": "already_running", **get_scan_status()}

    t = threading.Thread(
        target=_scan_nas_paths,
        args=(NAS_PATHS, incremental),
        daemon=True,
        name="nas-catalog-scan",
    )
    t.start()
    return {"status": "started"}


# ── Search ────────────────────────────────────────────────────────────────────

def search_catalog(
    query: str = "",
    client: str = "",
    category: str = "",
    extension: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Search the NAS catalog.

    - query: FTS search against filenames and paths
    - client: filter by client folder name
    - category: filter by case category (INJURY, CRIMINAL, etc.)
    - extension: filter by file extension (.pdf, .docx, etc.)
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    conditions = []
    params = []

    if query:
        # Use FTS5 for filename search
        import re
        tokens = re.findall(r"[a-zA-Z0-9']+", query)
        if tokens:
            fts_query = " OR ".join(f'"{t}"' for t in tokens)
            conditions.append(
                "id IN (SELECT rowid FROM nas_catalog_fts WHERE nas_catalog_fts MATCH ?)"
            )
            params.append(fts_query)

    if client:
        conditions.append("client_folder LIKE ?")
        params.append(f"%{client}%")

    if category:
        conditions.append("category = ?")
        params.append(category)

    if extension:
        ext = extension if extension.startswith('.') else f'.{extension}'
        conditions.append("extension = ?")
        params.append(ext.lower())

    where = " AND ".join(conditions) if conditions else "1=1"

    # Count total matches
    cur.execute(f"SELECT COUNT(*) FROM nas_catalog WHERE {where}", params)
    total = cur.fetchone()[0]

    # Fetch page
    cur.execute(
        f"SELECT * FROM nas_catalog WHERE {where} ORDER BY mtime DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    results = [dict(row) for row in cur.fetchall()]

    conn.close()
    return {"total": total, "results": results, "limit": limit, "offset": offset}


def get_catalog_stats() -> dict:
    """Return summary stats about the catalog."""
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM nas_catalog")
    total = cur.fetchone()[0]

    cur.execute("SELECT category, COUNT(*) FROM nas_catalog GROUP BY category ORDER BY COUNT(*) DESC")
    by_category = {row[0] or "Uncategorized": row[1] for row in cur.fetchall()}

    cur.execute("SELECT extension, COUNT(*) FROM nas_catalog GROUP BY extension ORDER BY COUNT(*) DESC LIMIT 15")
    by_ext = {row[0] or "none": row[1] for row in cur.fetchall()}

    cur.execute("SELECT COUNT(DISTINCT client_folder) FROM nas_catalog WHERE client_folder != ''")
    unique_clients = cur.fetchone()[0]

    cur.execute("SELECT SUM(size_bytes) FROM nas_catalog")
    total_size = cur.fetchone()[0] or 0

    conn.close()
    return {
        "total_files": total,
        "unique_clients": unique_clients,
        "total_size_bytes": total_size,
        "total_size_gb": round(total_size / (1024**3), 2),
        "by_category": by_category,
        "by_extension": by_ext,
    }


def get_client_list(category: str = "", limit: int = 500) -> list[dict]:
    """Return list of client folders with file counts."""
    conn = _get_conn()
    cur = conn.cursor()

    if category:
        cur.execute("""
            SELECT client_folder, category, COUNT(*) as file_count,
                   SUM(size_bytes) as total_size, MAX(mtime_date) as latest_file
            FROM nas_catalog
            WHERE client_folder != '' AND category = ?
            GROUP BY client_folder, category
            ORDER BY client_folder
            LIMIT ?
        """, [category, limit])
    else:
        cur.execute("""
            SELECT client_folder, category, COUNT(*) as file_count,
                   SUM(size_bytes) as total_size, MAX(mtime_date) as latest_file
            FROM nas_catalog
            WHERE client_folder != ''
            GROUP BY client_folder, category
            ORDER BY client_folder
            LIMIT ?
        """, [limit])

    results = [
        {
            "client_folder": row[0],
            "client": row[0],
            "category": row[1],
            "file_count": row[2],
            "total_size_mb": round((row[3] or 0) / (1024**2), 1),
            "latest_file": row[4],
        }
        for row in cur.fetchall()
    ]
    conn.close()
    return results
