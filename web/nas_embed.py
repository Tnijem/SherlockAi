"""
Sherlock Tier 3 — Smart NAS Embedding.

Background process that selects high-priority NAS files and creates
ChromaDB embeddings for semantic search. Priority order:

  1. Active case files (linked via case.nas_path)
  2. Recently modified legal documents
  3. Large text-rich files (high char_count in nas_text)
  4. All remaining extractable files

Runs incrementally: only embeds files not yet in ChromaDB.
Uses existing indexer pipeline for chunking + embedding.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from logging_config import get_logger

log = get_logger("sherlock.embed")

# ── Configuration ─────────────────────────────────────────────────────────────

# Max files per embedding run (to avoid monopolizing Ollama)
MAX_PER_RUN = 200

# Legal document extensions (prioritized for embedding)
LEGAL_EXTS = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.txt', '.rtf'}

# Min text content to bother embedding
MIN_CHARS = 100

# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_path() -> str:
    from config import DB_PATH
    return DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ── Embedding status ──────────────────────────────────────────────────────────

_embed_status = {
    "active": False,
    "total_queued": 0,
    "processed": 0,
    "embedded_ok": 0,
    "errors": 0,
    "skipped": 0,
    "current_file": "",
    "stage": "idle",
    "started_at": None,
    "elapsed_s": 0,
}
_status_lock = threading.Lock()


def get_embed_status() -> dict:
    with _status_lock:
        s = dict(_embed_status)
        if s["started_at"]:
            s["elapsed_s"] = round(time.time() - s["started_at"])
        return s


# ── Priority queue ────────────────────────────────────────────────────────────

def _get_priority_files(limit: int = MAX_PER_RUN) -> list[dict]:
    """
    Get NAS files prioritized for embedding.
    Returns files that have text content but aren't yet in ChromaDB.
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get files that:
    # 1. Have text content (from Tier 2 extraction)
    # 2. Are NOT already indexed via uploads table (which means they're in ChromaDB)
    # Priority scoring:
    #   - Active case files get score 100
    #   - Recent legal docs get score 50
    #   - Large text content gets score 25
    #   - Everything else gets score 1
    cur.execute("""
        SELECT
            t.file_path,
            c.filename,
            c.extension,
            c.client_folder,
            c.category,
            c.size_bytes,
            c.mtime,
            t.char_count,
            CASE
                -- Files in active case NAS paths get highest priority
                WHEN EXISTS (
                    SELECT 1 FROM cases cs
                    WHERE cs.status = 'active'
                    AND t.file_path LIKE cs.nas_path || '%'
                ) THEN 100
                -- Legal documents modified in last 90 days
                WHEN c.extension IN ('.pdf','.docx','.doc','.xlsx','.pptx','.rtf')
                    AND c.mtime > strftime('%s', 'now', '-90 days') THEN 50
                -- Files with substantial text content
                WHEN t.char_count > 5000 THEN 25
                -- Recent files
                WHEN c.mtime > strftime('%s', 'now', '-180 days') THEN 10
                ELSE 1
            END AS priority
        FROM nas_text t
        JOIN nas_catalog c ON c.file_path = t.file_path
        LEFT JOIN uploads u ON u.original_name = c.filename
        WHERE t.status = 'ok'
          AND t.char_count >= ?
          AND u.id IS NULL
        ORDER BY priority DESC, t.char_count DESC
        LIMIT ?
    """, [MIN_CHARS, limit])

    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    return results


def _get_active_case_paths() -> list[str]:
    """Get NAS paths from active cases."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT nas_path FROM cases WHERE status='active' AND nas_path IS NOT NULL AND nas_path != ''")
        return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


# ── Embedding loop ────────────────────────────────────────────────────────────

def _run_embedding():
    """Background embedding loop."""
    from indexer import extract_text, chunk_text
    import rag

    with _status_lock:
        if _embed_status["active"]:
            log.info("embed_already_running")
            return
        _embed_status.update({
            "active": True,
            "total_queued": 0,
            "processed": 0,
            "embedded_ok": 0,
            "errors": 0,
            "skipped": 0,
            "current_file": "",
            "stage": "queuing",
            "started_at": time.time(),
        })

    try:
        files = _get_priority_files()
        total = len(files)

        with _status_lock:
            _embed_status["total_queued"] = total
            _embed_status["stage"] = "embedding"

        if total == 0:
            log.info("embed_nothing_pending")
            return

        log.info("embed_start: %d files queued", total)

        client = rag._chroma_client()
        from config import GLOBAL_COLLECTION
        try:
            collection = client.get_or_create_collection(
                name=GLOBAL_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            log.error("embed_collection_error: %s", e)
            return

        for f in files:
            fp = f["file_path"]
            fname = f["filename"]

            with _status_lock:
                _embed_status["current_file"] = fname
                _embed_status["processed"] += 1

            if not os.path.exists(fp):
                with _status_lock:
                    _embed_status["skipped"] += 1
                continue

            try:
                # Get text (already extracted in Tier 2, but read fresh for chunking)
                text = extract_text(Path(fp))
                if not text or len(text.strip()) < MIN_CHARS:
                    with _status_lock:
                        _embed_status["skipped"] += 1
                    continue

                # Chunk the text
                chunks = chunk_text(text)
                if not chunks:
                    with _status_lock:
                        _embed_status["skipped"] += 1
                    continue

                # Embed each chunk and upsert to ChromaDB
                for i, chunk in enumerate(chunks):
                    chunk_id = f"nas_{hash(fp)}_{i}"
                    embedding = rag.embed_query(chunk["text"])

                    collection.upsert(
                        ids=[chunk_id],
                        documents=[chunk["text"]],
                        embeddings=[embedding],
                        metadatas=[{
                            "source": fname,
                            "path": fp,
                            "chunk_index": i,
                            "total_chunks": len(chunks),
                            "client_folder": f.get("client_folder", ""),
                            "category": f.get("category", ""),
                            "nas_indexed": True,
                        }],
                    )

                with _status_lock:
                    _embed_status["embedded_ok"] += 1

                # Log progress every 10 files
                if _embed_status["processed"] % 10 == 0:
                    log.info("embed_progress: %d/%d (ok=%d, skip=%d, err=%d)",
                             _embed_status["processed"], total,
                             _embed_status["embedded_ok"],
                             _embed_status["skipped"],
                             _embed_status["errors"])

            except Exception as e:
                log.warning("embed_file_error: %s: %s", fname, str(e)[:200])
                with _status_lock:
                    _embed_status["errors"] += 1

        log.info("embed_done: %d processed, %d embedded, %d skipped, %d errors",
                 _embed_status["processed"],
                 _embed_status["embedded_ok"],
                 _embed_status["skipped"],
                 _embed_status["errors"])

    except Exception as e:
        log.error("embed_fatal: %s", e)
    finally:
        with _status_lock:
            _embed_status["active"] = False
            _embed_status["stage"] = "done"
            _embed_status["current_file"] = ""


# ── Public API ────────────────────────────────────────────────────────────────

def start_embedding(limit: int = MAX_PER_RUN) -> dict:
    """Start background embedding. Returns immediately."""
    t = threading.Thread(target=_run_embedding, daemon=True, name="nas-embed")
    t.start()
    return {"started": True, "message": f"Embedding started (up to {limit} files)"}


def get_embed_stats() -> dict:
    """Return embedding statistics."""
    conn = _get_conn()
    cur = conn.cursor()

    try:
        # Total files with text that could be embedded
        cur.execute("""
            SELECT COUNT(*) FROM nas_text
            WHERE status='ok' AND char_count >= ?
        """, [MIN_CHARS])
        embeddable = cur.fetchone()[0]

        # Files already embedded (approximate — check uploads or nas_indexed metadata)
        # We track via ChromaDB metadata, but for speed check the embed_status
        cur.execute("SELECT COUNT(DISTINCT original_name) FROM uploads WHERE status='ready'")
        already_embedded = cur.fetchone()[0]

    except Exception:
        embeddable = 0
        already_embedded = 0

    conn.close()

    return {
        "embeddable_files": embeddable,
        "already_embedded": already_embedded,
        "max_per_run": MAX_PER_RUN,
    }
