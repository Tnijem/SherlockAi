"""
CourtListener API integration for Sherlock.

Downloads real legal case opinions from https://www.courtlistener.com
and saves them to SampleData/ for indexing.

API docs: https://www.courtlistener.com/help/api/rest/
No API key required for basic access (60 req/hr anonymous).
Register for 5,000 req/day free tier.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests

from config import NAS_PATHS, UPLOADS_DIR
from logging_config import get_logger

log = get_logger("sherlock.courtlistener")

# ── Config ────────────────────────────────────────────────────────────────────

API_BASE = "https://www.courtlistener.com/api/rest/v4"
SAMPLE_DATA_DIR = Path(__file__).parent.parent / "SampleData"
REQUEST_DELAY = 1.5   # seconds between API calls (rate-limit courtesy)
MAX_TEXT_CHARS = 100_000  # cap opinion text per file

# Common court identifiers (CourtListener slugs)
COURTS = {
    "All Federal":    None,
    "Supreme Court":  "scotus",
    "1st Circuit":    "ca1",
    "2nd Circuit":    "ca2",
    "3rd Circuit":    "ca3",
    "4th Circuit":    "ca4",
    "5th Circuit":    "ca5",
    "6th Circuit":    "ca6",
    "7th Circuit":    "ca7",
    "8th Circuit":    "ca8",
    "9th Circuit":    "ca9",
    "10th Circuit":   "ca10",
    "11th Circuit":   "ca11",
    "D.C. Circuit":   "cadc",
    "Fed. Circuit":   "cafc",
    "SDNY":           "nysd",
    "NDCA":           "cand",
    "CDCA":           "cacd",
    "N.D. Ill.":      "ilnd",
    "S.D. Tex.":      "txsd",
}

# ── Status tracking ───────────────────────────────────────────────────────────

_status: dict = {
    "running": False,
    "total": 0,
    "downloaded": 0,
    "skipped": 0,
    "failed": 0,
    "messages": [],
    "done": False,
}
_status_lock = threading.Lock()


def get_download_status() -> dict:
    with _status_lock:
        return dict(_status)


def _reset_status(total: int):
    with _status_lock:
        _status.update({
            "running": True,
            "total": total,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "messages": [],
            "done": False,
        })


def _update_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def _append_msg(msg: str):
    with _status_lock:
        _status["messages"].append(msg)
        # Keep last 200 messages
        if len(_status["messages"]) > 200:
            _status["messages"] = _status["messages"][-200:]


# ── Text helpers ──────────────────────────────────────────────────────────────

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    text = _HTML_TAG.sub(" ", text)
    text = _WHITESPACE.sub("\n\n", text)
    return text.strip()


def _safe_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", s)[:80]


# ── API calls ─────────────────────────────────────────────────────────────────

def _search_opinions(
    query: str = "",
    court: Optional[str] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
    page_size: int = 20,
    page: int = 1,
) -> dict:
    """
    Search CourtListener opinions endpoint.
    Returns raw JSON response dict.
    """
    params: dict = {
        "format": "json",
        "page_size": min(page_size, 20),  # API max is 20 per page
        "page": page,
        "order_by": "score desc",
        "type": "o",   # opinions only
    }
    if query:
        params["q"] = query
    if court:
        params["court"] = court
    if after_date:
        params["filed_after"] = after_date
    if before_date:
        params["filed_before"] = before_date

    resp = requests.get(
        f"{API_BASE}/search/",
        params=params,
        timeout=30,
        headers={"User-Agent": "SherlockLegal/1.0 (local law firm RAG)"},
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_opinion_text(opinion_url: str) -> tuple[str, str]:
    """
    Fetch full opinion text from a CourtListener opinion URL.
    Returns (case_name, plain_text).
    """
    resp = requests.get(
        opinion_url,
        params={"format": "json"},
        timeout=30,
        headers={"User-Agent": "SherlockLegal/1.0 (local law firm RAG)"},
    )
    resp.raise_for_status()
    data = resp.json()

    # Try text fields in order of preference
    text = (
        data.get("plain_text")
        or _clean(data.get("html_with_citations") or "")
        or _clean(data.get("html") or "")
        or _clean(data.get("xml_harvard") or "")
        or ""
    )
    case_name = data.get("case_name", "") or data.get("case_name_short", "") or "unknown"
    return case_name, text


# ── Download job ──────────────────────────────────────────────────────────────

def start_download(
    count: int = 20,
    query: str = "",
    court: Optional[str] = None,
    after_date: Optional[str] = None,
    before_date: Optional[str] = None,
    dest_dir: Optional[str] = None,
    trigger_index: bool = True,
) -> None:
    """
    Launch a background thread to download `count` opinions from CourtListener.

    Args:
        count:         Number of cases to download (max 200).
        query:         Free-text search query (e.g. "breach of contract").
        court:         CourtListener court slug (e.g. "scotus", "ca9") or None for all.
        after_date:    ISO date string e.g. "2020-01-01".
        before_date:   ISO date string e.g. "2023-12-31".
        dest_dir:      Directory to save .txt files (defaults to SampleData/).
        trigger_index: If True, call start_nas_index() after download completes.
    """
    if _status.get("running"):
        log.warning("courtlistener_download_already_running")
        return

    count = min(count, 200)
    _reset_status(total=count)

    def _run():
        save_dir = Path(dest_dir) if dest_dir else SAMPLE_DATA_DIR
        save_dir.mkdir(parents=True, exist_ok=True)
        log.info("courtlistener_download_start", extra={
            "count": count, "court": court, "query": query
        })

        collected = 0
        page = 1

        while collected < count:
            batch = min(20, count - collected)  # API max 20/page
            try:
                result = _search_opinions(
                    query=query,
                    court=court,
                    after_date=after_date,
                    before_date=before_date,
                    page_size=batch,
                    page=page,
                )
            except Exception as exc:
                _append_msg(f"✗ Search error (page {page}): {exc}")
                log.error("courtlistener_search_error: %s", exc)
                break

            hits = result.get("results", [])
            if not hits:
                _append_msg("ℹ No more results from CourtListener.")
                break

            for hit in hits:
                if collected >= count:
                    break

                case_name = hit.get("caseName") or hit.get("case_name") or "Unknown"
                court_id  = hit.get("court_id", "unknown")
                filed     = hit.get("dateFiled", "")[:10] if hit.get("dateFiled") else "unknown"
                opinion_url = None

                # opinions is a list of partial URLs like "/api/rest/v4/opinions/12345/"
                for op in hit.get("opinions", []):
                    if isinstance(op, dict):
                        opinion_url = "https://www.courtlistener.com" + op.get("absolute_url", "")
                        break
                    elif isinstance(op, str):
                        opinion_url = "https://www.courtlistener.com" + op
                        break

                if not opinion_url:
                    _append_msg(f"⚠ Skip (no opinion URL): {case_name}")
                    with _status_lock:
                        _status["skipped"] += 1
                    continue

                # Build filename
                fname = f"{filed}_{_safe_filename(case_name)}_{court_id}.txt"
                fpath = save_dir / fname

                if fpath.exists():
                    _append_msg(f"→ Already exists: {fname}")
                    with _status_lock:
                        _status["skipped"] += 1
                    collected += 1
                    continue

                # Fetch opinion text
                try:
                    time.sleep(REQUEST_DELAY)
                    _, text = _fetch_opinion_text(opinion_url)
                    if not text or len(text.strip()) < 100:
                        _append_msg(f"⚠ Skip (no text): {case_name}")
                        with _status_lock:
                            _status["skipped"] += 1
                        continue

                    text = text[:MAX_TEXT_CHARS]
                    header = (
                        f"CASE: {case_name}\n"
                        f"COURT: {court_id.upper()}\n"
                        f"DATE FILED: {filed}\n"
                        f"SOURCE: CourtListener\n"
                        f"URL: {opinion_url}\n"
                        f"{'─' * 60}\n\n"
                    )
                    fpath.write_text(header + text, encoding="utf-8")
                    _append_msg(f"✓ {fname}")
                    with _status_lock:
                        _status["downloaded"] += 1
                    collected += 1
                    log.info("courtlistener_saved: %s", fname)

                except Exception as exc:
                    _append_msg(f"✗ Error fetching {case_name}: {exc}")
                    log.error("courtlistener_fetch_error: %s — %s", case_name, exc)
                    with _status_lock:
                        _status["failed"] += 1

            if not result.get("next"):
                _append_msg("ℹ Reached end of CourtListener results.")
                break
            page += 1

        _update_status(running=False, done=True)
        log.info("courtlistener_done", extra={
            "downloaded": _status["downloaded"],
            "skipped": _status["skipped"],
            "failed": _status["failed"],
        })

        # Trigger NAS/case re-index if SampleData is in NAS_PATHS
        if trigger_index:
            _trigger_indexer(save_dir)

    threading.Thread(target=_run, daemon=True, name="courtlistener-dl").start()


def _trigger_indexer(save_dir: Path):
    """
    After download, trigger a NAS re-index.
    Always uses start_nas_index(NAS_PATHS). If save_dir isn't in NAS_PATHS,
    logs a hint so the admin can add it rather than silently doing nothing.
    """
    from indexer import start_nas_index
    try:
        save_str  = str(save_dir.resolve())
        nas_strs  = [str(Path(p).resolve()) for p in NAS_PATHS]
        if save_str not in nas_strs:
            log.warning(
                "courtlistener_index_hint: %s is not in NAS_PATHS — "
                "add it in Configuration to auto-index downloaded cases. "
                "Triggering index of configured NAS paths instead.",
                save_dir,
            )
        start_nas_index(NAS_PATHS)
        log.info("courtlistener_triggered_nas_index")
    except Exception as exc:
        log.error("courtlistener_trigger_index_error: %s", exc)


# ── Public helpers ────────────────────────────────────────────────────────────

def list_courts() -> dict[str, Optional[str]]:
    """Returns the friendly-name → court-slug mapping."""
    return dict(COURTS)
