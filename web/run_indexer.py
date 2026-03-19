#!/usr/bin/env python3
"""
Sherlock incremental indexer — called by launchd every 30 min.

Two-pass strategy:
  1. Cases: index each active case's NAS path into its own ChromaDB collection.
  2. Global: index any NAS_PATHS configured in sherlock.conf (historical/unorganized files)
             into the shared global collection.

Both passes use mtime-first skipping — only new/changed files are processed.
Safe to run while the web app is running.
"""

import os
import sys
import logging
from pathlib import Path

os.chdir(Path(__file__).parent)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [indexer] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sherlock.run_indexer")

from config import NAS_PATHS, GLOBAL_COLLECTION
from models import Case, init_db, SessionLocal
from indexer import start_case_index, start_nas_index, get_job_status
import time


def _wait_for_job(job_id: str, label: str):
    while True:
        status = get_job_status(job_id)
        if not status:
            break
        log.info("[%s] %s | indexed=%s skipped=%s errors=%s total=%s",
                 label,
                 status.get("status"),
                 status.get("indexed", 0),
                 status.get("skipped", 0),
                 status.get("errors", 0),
                 status.get("total", "?"))
        if status.get("done"):
            break
        time.sleep(10)


def main():
    init_db()
    db = SessionLocal()

    try:
        # ── Pass 1: Per-case indexing ─────────────────────────────────────────
        cases = (
            db.query(Case)
            .filter(Case.status == "active", Case.nas_path.isnot(None))
            .all()
        )

        if cases:
            log.info("Pass 1: indexing %d active case(s).", len(cases))
            for case in cases:
                nas = Path(case.nas_path)
                if not nas.exists():
                    log.warning("Case %d (%s): NAS path not accessible: %s",
                                case.id, case.case_name, case.nas_path)
                    continue
                log.info("Case %d (%s): scanning %s", case.id, case.case_name, case.nas_path)
                job_id = start_case_index(case.id, case.nas_path)
                _wait_for_job(job_id, f"case:{case.id}")
        else:
            log.info("Pass 1: no active cases with NAS paths configured.")

        # ── Pass 2: Global NAS paths (unorganized / historical files) ─────────
        if NAS_PATHS:
            log.info("Pass 2: global index of %d NAS path(s).", len(NAS_PATHS))
            job_id = start_nas_index(NAS_PATHS)
            _wait_for_job(job_id, "global")
        else:
            log.info("Pass 2: no global NAS_PATHS configured — skipping.")

        log.info("Indexer run complete.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
