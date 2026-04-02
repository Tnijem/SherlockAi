"""
Sherlock Tier 2 — NAS Text Extraction.

Background process that extracts text from cataloged NAS files
and stores it in FTS5 for full-text keyword search.

Priority order:
  1. Small text-based files (.txt, .csv, .html) — instant extraction
  2. Office documents (.docx, .xlsx, .pptx) — fast extraction
  3. PDFs — slower, may need OCR
  4. Images — slowest (Whisper/OCR)

Runs incrementally: only processes files not yet in nas_text table.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from logging_config import get_logger

log = get_logger("sherlock.text")

# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nas_text (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT UNIQUE NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    char_count  INTEGER NOT NULL DEFAULT 0,
    extracted_at TEXT NOT NULL,
    extract_ms  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ok',  -- ok, error, empty, skipped
    error_msg   TEXT DEFAULT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS nas_text_fts USING fts5(
    file_path, text_content,
    content='nas_text',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS nas_text_ai AFTER INSERT ON nas_text BEGIN
    INSERT INTO nas_text_fts(rowid, file_path, text_content)
    VALUES (new.id, new.file_path, new.text_content);
END;

CREATE TRIGGER IF NOT EXISTS nas_text_ad AFTER DELETE ON nas_text BEGIN
    INSERT INTO nas_text_fts(nas_text_fts, rowid, file_path, text_content)
    VALUES ('delete', old.id, old.file_path, old.text_content);
END;

CREATE TRIGGER IF NOT EXISTS nas_text_au AFTER UPDATE ON nas_text BEGIN
    INSERT INTO nas_text_fts(nas_text_fts, rowid, file_path, text_content)
    VALUES ('delete', old.id, old.file_path, old.text_content);
    INSERT INTO nas_text_fts(rowid, file_path, text_content)
    VALUES (new.id, new.file_path, new.text_content);
END;
"""

# Extension priority tiers (lower = process first)
TIER_1_EXTS = {'.txt', '.csv', '.tsv', '.log', '.md', '.json', '.xml', '.html', '.htm'}
TIER_2_EXTS = {'.docx', '.xlsx', '.pptx', '.rtf', '.doc', '.xls'}
TIER_3_EXTS = {'.pdf'}
TIER_4_EXTS = {'.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.heic'}  # OCR — slow

ALL_TEXT_EXTS = TIER_1_EXTS | TIER_2_EXTS | TIER_3_EXTS | TIER_4_EXTS

# Max file size to attempt extraction (skip huge files)
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

# Batch size for commits
BATCH_SIZE = 50

# ── DB connection ─────────────────────────────────────────────────────────────

def _db_path() -> str:
    from config import DB_PATH
    return DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_text_db():
    """Create nas_text table and FTS index if they don't exist."""
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    conn.close()
    log.info("text_db_initialized")


# ── Extraction status ─────────────────────────────────────────────────────────

_extract_status = {
    "active": False,
    "total_queued": 0,
    "processed": 0,
    "extracted_ok": 0,
    "extracted_empty": 0,
    "errors": 0,
    "skipped": 0,
    "current_file": "",
    "stage": "idle",
    "started_at": None,
    "elapsed_s": 0,
}
_status_lock = threading.Lock()


def get_extract_status() -> dict:
    with _status_lock:
        s = dict(_extract_status)
        if s["started_at"]:
            s["elapsed_s"] = round(time.time() - s["started_at"])
        return s


# ── Priority queue builder ────────────────────────────────────────────────────

def _get_pending_files(limit: int = 50000) -> list[tuple[str, str, int]]:
    """
    Get files from nas_catalog that haven't been text-extracted yet.
    Returns list of (file_path, extension, size_bytes) sorted by priority.
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Get files from catalog that are NOT in nas_text
    # Filter to extractable extensions and reasonable size
    ext_list = tuple(ALL_TEXT_EXTS)
    placeholders = ",".join("?" * len(ext_list))

    cur.execute(f"""
        SELECT c.file_path, c.extension, c.size_bytes
        FROM nas_catalog c
        LEFT JOIN nas_text t ON c.file_path = t.file_path
        WHERE t.file_path IS NULL
          AND c.extension IN ({placeholders})
          AND c.size_bytes <= ?
          AND c.size_bytes > 0
          AND c.filename NOT LIKE '~$%'
        ORDER BY
            CASE
                WHEN c.extension IN ('.txt','.csv','.tsv','.log','.md','.json','.xml','.html','.htm') THEN 1
                WHEN c.extension IN ('.docx','.xlsx','.pptx','.rtf') THEN 2
                WHEN c.extension IN ('.doc','.xls') THEN 3
                WHEN c.extension = '.pdf' THEN 4
                WHEN c.extension IN ('.jpg','.jpeg','.png','.tiff','.bmp','.heic') THEN 5
                ELSE 6
            END,
            c.size_bytes ASC
        LIMIT ?
    """, list(ext_list) + [MAX_FILE_SIZE, limit])

    results = cur.fetchall()
    conn.close()
    return results


# ── Main extraction loop ─────────────────────────────────────────────────────

def _run_extraction():
    """Background extraction loop."""
    log.info("text_extraction_thread_starting")
    try:
        from indexer import extract_text
        log.info("text_extraction_indexer_imported")
    except Exception as e:
        log.error("text_extraction_import_failed: %s", e)
        return

    with _status_lock:
        if _extract_status["active"]:
            log.info("text_extraction_already_running")
            return
        _extract_status.update({
            "active": True,
            "total_queued": 0,
            "processed": 0,
            "extracted_ok": 0,
            "extracted_empty": 0,
            "errors": 0,
            "skipped": 0,
            "current_file": "",
            "stage": "queuing",
            "started_at": time.time(),
        })

    try:
        pending = _get_pending_files()
        total = len(pending)

        with _status_lock:
            _extract_status["total_queued"] = total
            _extract_status["stage"] = "extracting"

        if total == 0:
            log.info("text_extraction_nothing_pending")
            return

        log.info("text_extraction_start: %d files queued", total)

        conn = _get_conn()
        now = datetime.now().isoformat()
        batch_count = 0

        for file_path, ext, size_bytes in pending:
            with _status_lock:
                _extract_status["current_file"] = os.path.basename(file_path)
                _extract_status["processed"] += 1

            # Skip files that no longer exist (NAS disconnected, deleted)
            if not os.path.exists(file_path):
                with _status_lock:
                    _extract_status["skipped"] += 1
                _insert_result(conn, file_path, "", 0, 0, "skipped", "file not found", now)
                batch_count += 1
                if batch_count >= BATCH_SIZE:
                    conn.commit()
                    batch_count = 0
                continue

            # Extract text (yield GIL before heavy I/O)
            time.sleep(0.05)
            t0 = time.time()
            try:
                text = extract_text(Path(file_path))
                elapsed_ms = int((time.time() - t0) * 1000)

                if text and len(text.strip()) > 10:
                    _insert_result(conn, file_path, text, len(text), elapsed_ms, "ok", None, now)
                    with _status_lock:
                        _extract_status["extracted_ok"] += 1
                else:
                    _insert_result(conn, file_path, "", 0, elapsed_ms, "empty", None, now)
                    with _status_lock:
                        _extract_status["extracted_empty"] += 1

            except Exception as e:
                elapsed_ms = int((time.time() - t0) * 1000)
                _insert_result(conn, file_path, "", 0, elapsed_ms, "error", str(e)[:500], now)
                with _status_lock:
                    _extract_status["errors"] += 1

            batch_count += 1
            if batch_count >= BATCH_SIZE:
                conn.commit()
                batch_count = 0

            # Yield GIL to keep uvicorn responsive
            time.sleep(0.1)

            # Log progress every 500 files
            if _extract_status["processed"] % 500 == 0:
                log.info("text_extraction_progress: %d/%d (ok=%d, empty=%d, err=%d, skip=%d)",
                         _extract_status["processed"], total,
                         _extract_status["extracted_ok"],
                         _extract_status["extracted_empty"],
                         _extract_status["errors"],
                         _extract_status["skipped"])

        conn.commit()
        conn.close()

        log.info("text_extraction_done: %d processed, %d ok, %d empty, %d errors, %d skipped",
                 _extract_status["processed"],
                 _extract_status["extracted_ok"],
                 _extract_status["extracted_empty"],
                 _extract_status["errors"],
                 _extract_status["skipped"])

    except Exception as e:
        log.error("text_extraction_fatal: %s", e)
    finally:
        with _status_lock:
            _extract_status["active"] = False
            _extract_status["stage"] = "done"
            _extract_status["current_file"] = ""


def _insert_result(conn, file_path, text, char_count, elapsed_ms, status, error_msg, now):
    """Insert or update a text extraction result."""
    try:
        conn.execute("""
            INSERT OR REPLACE INTO nas_text
                (file_path, text_content, char_count, extracted_at, extract_ms, status, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (file_path, text, char_count, now, elapsed_ms, status, error_msg))
    except Exception as e:
        log.warning("text_insert_error: %s: %s", os.path.basename(file_path), e)


# ── Public API ────────────────────────────────────────────────────────────────

def start_text_extraction() -> dict:
    """Start background text extraction. Returns immediately."""
    t = threading.Thread(target=_run_extraction, daemon=True, name="text-extract")
    t.start()
    return {"started": True, "message": "Text extraction started in background"}


def search_text(
    query: str,
    client: str = "",
    extension: str = "",
    limit: int = 30,
    offset: int = 0,
) -> dict:
    """Full-text search across extracted NAS file content."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    import re
    tokens = re.findall(r"[a-zA-Z0-9']+", query)
    if not tokens:
        conn.close()
        return {"total": 0, "results": [], "limit": limit, "offset": offset}

    fts_query = " AND ".join(f'"{t}"' for t in tokens)

    # Join with catalog for metadata
    extra_where = ""
    params = [fts_query]

    if client:
        extra_where += " AND c.client_folder LIKE ?"
        params.append(f"%{client}%")

    if extension:
        ext = extension if extension.startswith('.') else f'.{extension}'
        extra_where += " AND c.extension = ?"
        params.append(ext.lower())

    # Count
    count_sql = f"""
        SELECT COUNT(*) FROM nas_text_fts f
        JOIN nas_catalog c ON c.file_path = f.file_path
        WHERE nas_text_fts MATCH ? {extra_where}
    """
    cur.execute(count_sql, params)
    total = cur.fetchone()[0]

    # Results with snippets
    result_sql = f"""
        SELECT f.file_path,
               snippet(nas_text_fts, 1, '<b>', '</b>', '...', 40) as snippet,
               c.filename, c.extension, c.size_bytes, c.client_folder, c.category, c.mtime_date
        FROM nas_text_fts f
        JOIN nas_catalog c ON c.file_path = f.file_path
        WHERE nas_text_fts MATCH ? {extra_where}
        ORDER BY rank
        LIMIT ? OFFSET ?
    """
    cur.execute(result_sql, params + [limit, offset])
    results = [dict(row) for row in cur.fetchall()]

    conn.close()
    return {"total": total, "results": results, "limit": limit, "offset": offset}


def get_text_stats() -> dict:
    """Return text extraction statistics."""
    conn = _get_conn()
    cur = conn.cursor()

    try:
        cur.execute("SELECT COUNT(*) FROM nas_text")
        total = cur.fetchone()[0]

        cur.execute("SELECT status, COUNT(*) FROM nas_text GROUP BY status")
        by_status = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT SUM(char_count) FROM nas_text WHERE status='ok'")
        total_chars = cur.fetchone()[0] or 0

        cur.execute("SELECT AVG(extract_ms) FROM nas_text WHERE status='ok' AND extract_ms > 0")
        avg_ms = cur.fetchone()[0] or 0
    except Exception:
        total = 0
        by_status = {}
        total_chars = 0
        avg_ms = 0

    conn.close()
    return {
        "total_files": total,
        "by_status": by_status,
        "total_chars": total_chars,
        "total_chars_mb": round(total_chars / (1024 * 1024), 1),
        "avg_extract_ms": round(avg_ms),
    }
