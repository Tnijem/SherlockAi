"""
Sherlock centralized logging configuration.

Four log streams, all JSON-structured, all rotating:
  app.log      — HTTP requests, startup, errors, general events         (50 MB × 5)
  audit.log    — Compliance trail: logins, file access, config changes  (200 MB × 10)
  rag.log      — Every RAG query: latency, scope, sources, scores       (50 MB × 5)
  indexer.log  — Indexing jobs: files, chunks, timings, errors          (100 MB × 5)

Usage:
  from logging_config import get_logger, audit
  log = get_logger("sherlock.rag")
  log.info("query", extra={"user_id": 1, "latency_ms": 842, "sources": 5})
  audit("file_preview", user_id=1, username="jsmith", path="/nas/case/doc.pdf")
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import contextvars
from datetime import datetime, timezone
from pathlib import Path

# ── Log directory ──────────────────────────────────────────────────────────────

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Request-ID context var (set by HTTP middleware, available everywhere) ───────

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# ── JSON formatter ─────────────────────────────────────────────────────────────

# Fields that middleware/callers inject via `extra=`
_EXTRA_FIELDS = (
    "request_id", "user_id", "username", "method", "path", "status",
    "duration_ms", "query", "scope", "sources", "top_score", "collections",
    "latency_embed_ms", "latency_retrieve_ms", "latency_llm_ms", "latency_total_ms",
    "event", "file_path", "file_size", "chunk_count", "job_id", "case_id",
    "indexed", "skipped", "errors", "ip", "detail",
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
            "rid":    request_id_var.get("-"),
        }
        for field in _EXTRA_FIELDS:
            val = getattr(record, field, None)
            if val is not None:
                obj[field] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


class _ConsoleFormatter(logging.Formatter):
    _COLOURS = {
        "DEBUG":    "\033[37m",
        "INFO":     "\033[36m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self._COLOURS.get(record.levelname, "")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        extra = ""
        for field in ("user_id", "event", "path", "status", "latency_total_ms", "latency_ms"):
            val = getattr(record, field, None)
            if val is not None:
                extra += f" {field}={val}"
        return (
            f"{colour}{ts} [{record.levelname[:4]}] "
            f"{record.name.replace('sherlock.', '')}: "
            f"{record.getMessage()}{extra}{self._RESET}"
        )


# ── Handler factory ───────────────────────────────────────────────────────────

def _rotating(filename: str, max_mb: int = 50, backups: int = 5) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(
        _LOG_DIR / filename,
        maxBytes=max_mb * 1_000_000,
        backupCount=backups,
        encoding="utf-8",
    )
    h.setFormatter(_JsonFormatter())
    return h


def _console() -> logging.Handler:
    h = logging.StreamHandler()
    h.setFormatter(_ConsoleFormatter())
    return h


# ── Setup ─────────────────────────────────────────────────────────────────────

_configured = False


def setup_logging(debug: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = logging.DEBUG if debug else logging.INFO

    # ── sherlock (root) → app.log + console
    root = logging.getLogger("sherlock")
    root.setLevel(level)
    root.addHandler(_rotating("app.log", max_mb=50, backups=5))
    root.addHandler(_console())
    root.propagate = False

    # ── sherlock.audit → audit.log (also inherits app.log via root)
    _audit_logger = logging.getLogger("sherlock.audit")
    _audit_logger.setLevel(logging.INFO)
    _audit_logger.addHandler(_rotating("audit.log", max_mb=200, backups=10))
    # Don't propagate to root — audit gets its own file + root's app.log via root handler
    # Actually do propagate so audit events also appear in app.log
    _audit_logger.propagate = True

    # ── sherlock.rag → rag.log
    _rag_logger = logging.getLogger("sherlock.rag")
    _rag_logger.setLevel(logging.INFO)
    _rag_logger.addHandler(_rotating("rag.log", max_mb=50, backups=5))
    _rag_logger.propagate = True   # also goes to app.log

    # ── sherlock.indexer → indexer.log
    _idx_logger = logging.getLogger("sherlock.indexer")
    _idx_logger.setLevel(logging.INFO)
    _idx_logger.addHandler(_rotating("indexer.log", max_mb=100, backups=5))
    _idx_logger.propagate = True

    # ── Silence noisy libs
    for noisy in ("uvicorn.access", "httpx", "chromadb", "httpcore",
                  "multipart", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info("Logging initialized", extra={"event": "startup"})


# ── Convenience ───────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """Return a logger under the sherlock hierarchy."""
    return logging.getLogger(name if name.startswith("sherlock") else f"sherlock.{name}")


_audit_log = logging.getLogger("sherlock.audit")


def audit(event: str, **kwargs) -> None:
    """
    Write a structured audit entry.

    audit("login_success", user_id=1, username="jsmith", ip="10.0.0.5")
    audit("file_preview",  user_id=1, username="jsmith", path="/nas/smith/depo.pdf")
    audit("config_change", user_id=1, username="admin",  detail="NAS_PATHS updated")
    """
    _audit_log.info(event, extra={"event": event, **kwargs})


# ── Log tail utility (used by admin API) ─────────────────────────────────────

def tail_log(
    stream: str,
    lines: int = 200,
    level: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """
    Return the last N parsed JSON log entries from a log stream.
    Filters by level and/or search string if provided.

    stream: "app" | "audit" | "rag" | "indexer"
    """
    filename = _LOG_DIR / f"{stream}.log"
    if not filename.exists():
        return []

    level_upper = level.upper() if level else None
    search_lower = search.lower() if search else None

    # Read from the end — collect up to lines * 4 raw bytes to avoid reading whole file
    with filename.open("rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        # Read last chunk (generous estimate: ~300 bytes per line)
        read_size = min(file_size, lines * 600)
        f.seek(max(0, file_size - read_size))
        raw = f.read().decode("utf-8", errors="replace")

    raw_lines = [l for l in raw.splitlines() if l.strip()]
    # Drop first line (may be partial)
    if file_size > read_size:
        raw_lines = raw_lines[1:]

    entries = []
    for line in reversed(raw_lines):
        try:
            obj = json.loads(line)
        except Exception:
            obj = {"ts": "-", "level": "RAW", "msg": line[:200]}

        if level_upper and obj.get("level") != level_upper:
            continue
        if search_lower:
            if search_lower not in line.lower():
                continue
        entries.append(obj)
        if len(entries) >= lines:
            break

    return list(reversed(entries))
