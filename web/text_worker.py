#!/usr/bin/env python3
"""Standalone text extraction worker — runs outside uvicorn to avoid GIL contention."""
import os, sys, time, sqlite3, logging

# Add web dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
from nas_text import ALL_TEXT_EXTS, MAX_FILE_SIZE, _get_conn, init_text_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("text_worker")

# Status file for the API to read
STATUS_FILE = os.path.join(os.path.dirname(__file__), '..', 'data', 'text_extract_status.json')

def write_status(status):
    import json
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status, f)
    except Exception:
        pass

def get_pending(limit=50000):
    conn = _get_conn()
    cur = conn.cursor()
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
                ELSE 6
            END,
            c.size_bytes ASC
        LIMIT ?
    """, list(ext_list) + [MAX_FILE_SIZE, limit])
    results = cur.fetchall()
    conn.close()
    return results

def main():
    init_text_db()
    from indexer import extract_text

    pending = get_pending()
    total = len(pending)
    log.info("Text extraction worker started: %d files pending", total)

    status = {
        "active": True,
        "total_queued": total,
        "processed": 0,
        "extracted_ok": 0,
        "extracted_empty": 0,
        "errors": 0,
        "current_file": "",
        "stage": "extracting",
        "started_at": time.time(),
        "pid": os.getpid(),
    }
    write_status(status)

    conn = _get_conn()

    for i, (file_path, ext, size_bytes) in enumerate(pending):
        status["processed"] = i
        status["current_file"] = os.path.basename(file_path)
        status["elapsed_s"] = round(time.time() - status["started_at"])
        if i % 10 == 0:
            write_status(status)

        try:
            fp = Path(file_path)
            if not fp.exists():
                continue

            start = time.time()
            text = extract_text(fp)
            ms = int((time.time() - start) * 1000)

            if text and text.strip():
                char_count = len(text)
                conn.execute(
                    "INSERT OR REPLACE INTO nas_text (file_path, text_content, char_count, extracted_at, extract_ms, status) VALUES (?, ?, ?, datetime('now'), ?, 'ok')",
                    (file_path, text[:500000], char_count, ms)
                )
                status["extracted_ok"] += 1
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO nas_text (file_path, text_content, char_count, extracted_at, extract_ms, status) VALUES (?, '', 0, datetime('now'), ?, 'empty')",
                    (file_path, ms)
                )
                status["extracted_empty"] += 1

            if i % 20 == 0:
                conn.commit()
            if i % 100 == 0 and i > 0:
                log.info("progress: %d/%d (ok=%d empty=%d err=%d) [%ds]",
                         i, total, status["extracted_ok"], status["extracted_empty"],
                         status["errors"], status["elapsed_s"])

        except Exception as e:
            status["errors"] += 1
            log.warning("extract failed: %s: %s", os.path.basename(file_path), str(e)[:100])
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO nas_text (file_path, text_content, char_count, extracted_at, extract_ms, status, error_msg) VALUES (?, '', 0, datetime('now'), 0, 'error', ?)",
                    (file_path, str(e)[:500])
                )
            except Exception:
                pass

    conn.commit()
    conn.close()

    status["active"] = False
    status["processed"] = total
    status["elapsed_s"] = round(time.time() - status["started_at"])
    status["stage"] = "done"
    write_status(status)
    log.info("Text extraction complete: %d ok, %d empty, %d errors in %ds",
             status["extracted_ok"], status["extracted_empty"], status["errors"], status["elapsed_s"])

if __name__ == "__main__":
    main()
