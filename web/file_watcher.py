"""
Automatic file watcher for Sherlock.

Watches NAS_PATHS using macOS FSEvents (via watchdog).
When supported files appear or change, triggers a debounced NAS re-index.

Debounce: 10 seconds of quiet after the last event before firing.
This batches rapid multi-file changes (e.g. bulk copy to NAS) into one job.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from config import NAS_PATHS
from logging_config import get_logger

log = get_logger("sherlock.watcher")

DEBOUNCE_SECONDS = 10  # seconds of quiet before triggering re-index

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    _WATCHDOG = True
except ImportError:
    _WATCHDOG = False
    log.warning("watchdog not installed — auto file-watching disabled. "
                "Install with: pip install watchdog")


def _is_supported(path: str) -> bool:
    """Returns True if the file extension is one the indexer can process."""
    from indexer import ALL_SUPPORTED
    return Path(path).suffix.lower() in ALL_SUPPORTED


class _DebounceHandler(FileSystemEventHandler if _WATCHDOG else object):
    """
    Coalesces rapid FS events (bulk file copies, incremental saves)
    into a single callback invocation after DEBOUNCE_SECONDS of quiet.
    """

    def __init__(self, callback, label: str):
        if _WATCHDOG:
            super().__init__()
        self._callback = callback
        self._label = label
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _reset_timer(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        log.info("watcher_trigger", extra={"watch": self._label})
        try:
            self._callback()
        except Exception as exc:
            log.error("watcher_callback_error: %s", exc)

    def on_created(self, event: "FileSystemEvent"):
        if not event.is_directory and _is_supported(event.src_path):
            log.debug("watcher_created: %s", event.src_path)
            self._reset_timer()

    def on_modified(self, event: "FileSystemEvent"):
        if not event.is_directory and _is_supported(event.src_path):
            log.debug("watcher_modified: %s", event.src_path)
            self._reset_timer()

    def on_moved(self, event: "FileSystemEvent"):
        dest = getattr(event, "dest_path", "")
        if dest and not event.is_directory and _is_supported(dest):
            log.debug("watcher_moved: %s → %s", event.src_path, dest)
            self._reset_timer()


class FileWatcher:
    """Manages watchdog observers for all configured NAS paths."""

    def __init__(self):
        self._observer: Optional["Observer"] = None
        self._running = False
        self._watched: list[str] = []

    def start(self):
        if not _WATCHDOG:
            log.warning("watcher_unavailable: install watchdog to enable auto-indexing")
            return

        if not NAS_PATHS:
            log.info("watcher_idle: no NAS_PATHS configured")
            return

        from indexer import start_nas_index

        self._observer = Observer()

        for path_str in NAS_PATHS:
            nas = Path(path_str)
            if not nas.exists():
                log.warning("watcher_skip_missing: %s (not mounted?)", path_str)
                continue

            handler = _DebounceHandler(
                callback=lambda: start_nas_index(NAS_PATHS),
                label=nas.name,
            )
            self._observer.schedule(handler, str(nas), recursive=True)
            self._watched.append(str(nas))
            log.info("watcher_watching: %s", nas)

        if not self._watched:
            log.info("watcher_idle: no accessible NAS paths to watch")
            return

        self._observer.start()
        self._running = True
        log.info("watcher_started: watching %d path(s), debounce=%ds",
                 len(self._watched), DEBOUNCE_SECONDS)

    def stop(self):
        if self._observer and self._running:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._running = False
            log.info("watcher_stopped")

    def is_running(self) -> bool:
        return self._running and self._observer is not None and self._observer.is_alive()

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "watched_paths": self._watched,
            "debounce_seconds": DEBOUNCE_SECONDS,
            "watchdog_available": _WATCHDOG,
        }


# ── Module singleton ──────────────────────────────────────────────────────────

_watcher = FileWatcher()


def start_watcher():
    """Start the file watcher. Called from main.py lifespan."""
    _watcher.start()


def stop_watcher():
    """Stop the file watcher. Called from main.py lifespan."""
    _watcher.stop()


def watcher_status() -> dict:
    return _watcher.status()
