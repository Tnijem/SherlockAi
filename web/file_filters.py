"""
Index file filters for Sherlock.

Rules stored in ~/Sherlock/index_filters.json. Each rule specifies conditions
and an action (exclude/include). Conditions within a rule are AND'd; multiple
rules are OR'd. An explicit "include" rule overrides any matching "exclude".

Supported conditions
--------------------
  filename_pattern   glob against filename only          "*.docx", "temp_*"
  path_pattern       glob against full absolute path     "/archive/**"
  created_before     created more than N ago             "4y", "6m", "30d"
  created_after      created less than N ago             "1y"
  modified_before    not modified in the last N          "2y"
  modified_after     modified within the last N          "90d"
  size_gt            file size > N bytes                 10485760  (10 MB)
  size_lt            file size < N bytes

Time units: y (years), mo/m (months), w (weeks), d (days), h (hours)
"""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from dataclasses import dataclass, asdict, fields as dc_fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from logging_config import get_logger

log = get_logger("sherlock.filters")

_FILTERS_PATH = Path(__file__).parent.parent / "index_filters.json"

# ── Time delta parsing ────────────────────────────────────────────────────────

_DELTA_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(y|mo|m|w|d|h)$", re.IGNORECASE)
_UNIT_DAYS = {"y": 365.25, "mo": 30.44, "m": 30.44, "w": 7.0, "d": 1.0, "h": 1/24}


def parse_delta(s: str) -> timedelta:
    """Parse "4y", "6m", "30d", "2w" → timedelta. Raises ValueError on bad input."""
    m = _DELTA_RE.match(s.strip())
    if not m:
        raise ValueError(
            f"Invalid duration {s!r}. Use e.g. '4y', '6m', '30d', '2w', '12h'"
        )
    n, unit = float(m.group(1)), m.group(2).lower()
    return timedelta(days=n * _UNIT_DAYS[unit])


def _created(stat) -> datetime:
    """File creation time — uses st_birthtime on macOS, falls back to min(mtime,ctime)."""
    bt = getattr(stat, "st_birthtime", None)
    return datetime.fromtimestamp(bt if bt is not None else min(stat.st_mtime, stat.st_ctime))


def _modified(stat) -> datetime:
    return datetime.fromtimestamp(stat.st_mtime)


# ── Filter rule ───────────────────────────────────────────────────────────────

@dataclass
class FilterRule:
    id:               str
    name:             str
    enabled:          bool            = True
    action:           str             = "exclude"   # "exclude" | "include"
    filename_pattern: Optional[str]   = None        # glob, e.g. "*.docx"
    path_pattern:     Optional[str]   = None        # glob on full path
    created_before:   Optional[str]   = None        # e.g. "4y"
    created_after:    Optional[str]   = None
    modified_before:  Optional[str]   = None        # e.g. "2y"
    modified_after:   Optional[str]   = None
    size_gt:          Optional[int]   = None        # bytes
    size_lt:          Optional[int]   = None

    def matches(self, fp: Path) -> bool:
        """Return True if ALL non-None conditions match this file."""
        try:
            stat = fp.stat()
        except OSError:
            return False
        now = datetime.now()

        if self.filename_pattern:
            if not fnmatch.fnmatch(fp.name.lower(), self.filename_pattern.lower()):
                return False

        if self.path_pattern:
            if not fnmatch.fnmatch(str(fp), self.path_pattern):
                return False

        if self.created_before:
            if _created(stat) >= now - parse_delta(self.created_before):
                return False   # too recent — doesn't match

        if self.created_after:
            if _created(stat) <= now - parse_delta(self.created_after):
                return False   # too old — doesn't match

        if self.modified_before:
            if _modified(stat) >= now - parse_delta(self.modified_before):
                return False

        if self.modified_after:
            if _modified(stat) <= now - parse_delta(self.modified_after):
                return False

        if self.size_gt is not None and stat.st_size <= self.size_gt:
            return False

        if self.size_lt is not None and stat.st_size >= self.size_lt:
            return False

        return True

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()
                if v is not None or k in ("id", "name", "enabled", "action")}

    @classmethod
    def from_dict(cls, d: dict) -> "FilterRule":
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def validate(self):
        """Raise ValueError if any time-delta field is malformed."""
        for field in ("created_before", "created_after", "modified_before", "modified_after"):
            val = getattr(self, field)
            if val:
                parse_delta(val)   # raises on bad format
        if self.action not in ("exclude", "include"):
            raise ValueError(f"action must be 'exclude' or 'include', got {self.action!r}")


# ── Filter set ────────────────────────────────────────────────────────────────

class FilterSet:
    def __init__(self, rules: list[FilterRule]):
        self._rules = [r for r in rules if r.enabled]

    def should_index(self, fp: Path) -> tuple[bool, Optional[str]]:
        """
        Returns (should_index, reason_if_excluded).
        An include rule matching overrides any exclude.
        """
        exclude_reason: Optional[str] = None
        for rule in self._rules:
            if rule.matches(fp):
                if rule.action == "exclude":
                    exclude_reason = rule.name
                elif rule.action == "include":
                    return True, None   # explicit include wins
        if exclude_reason:
            return False, f"filtered: {exclude_reason!r}"
        return True, None

    def apply(self, files: list[Path]) -> tuple[list[Path], int]:
        """
        Returns (kept_files, filtered_count).
        Logs each filtered file at DEBUG level.
        """
        if not self._rules:
            return files, 0
        kept, n_filtered = [], 0
        for fp in files:
            ok, reason = self.should_index(fp)
            if ok:
                kept.append(fp)
            else:
                n_filtered += 1
                log.debug("filtered_file: %s — %s", fp.name, reason)
        if n_filtered:
            log.info("file_filter: kept %d, filtered %d", len(kept), n_filtered)
        return kept, n_filtered


# ── Persistence ───────────────────────────────────────────────────────────────

def load_rules() -> list[FilterRule]:
    if not _FILTERS_PATH.exists():
        return []
    try:
        raw = json.loads(_FILTERS_PATH.read_text(encoding="utf-8"))
        return [FilterRule.from_dict(r) for r in raw]
    except Exception as exc:
        log.error("Failed to load index_filters.json: %s", exc)
        return []


def save_rules(rules: list[FilterRule]):
    _FILTERS_PATH.write_text(
        json.dumps([r.to_dict() for r in rules], indent=2),
        encoding="utf-8",
    )


def get_filter_set() -> FilterSet:
    return FilterSet(load_rules())


# ── CRUD helpers (called from main.py routes) ─────────────────────────────────

def api_list() -> list[dict]:
    return [r.to_dict() for r in load_rules()]


def api_add(data: dict) -> dict:
    rules = load_rules()
    data.setdefault("id", str(uuid.uuid4())[:8])
    rule = FilterRule.from_dict(data)
    rule.validate()
    rules.append(rule)
    save_rules(rules)
    log.info("filter_added: %s (%s)", rule.name, rule.id)
    return rule.to_dict()


def api_update(rule_id: str, updates: dict) -> Optional[dict]:
    rules = load_rules()
    for i, r in enumerate(rules):
        if r.id == rule_id:
            merged = {**r.to_dict(), **updates, "id": rule_id}
            updated = FilterRule.from_dict(merged)
            updated.validate()
            rules[i] = updated
            save_rules(rules)
            log.info("filter_updated: %s", rule_id)
            return updated.to_dict()
    return None


def api_delete(rule_id: str) -> bool:
    rules = load_rules()
    new_rules = [r for r in rules if r.id != rule_id]
    if len(new_rules) == len(rules):
        return False
    save_rules(new_rules)
    log.info("filter_deleted: %s", rule_id)
    return True


def api_preview(data: dict, scan_paths: list[str]) -> dict:
    """
    Dry-run a rule (not yet saved) against all files in scan_paths.
    Returns counts so the user can see impact before saving.
    """
    rule = FilterRule.from_dict({**data, "id": "preview", "enabled": True})
    rule.validate()
    fs = FilterSet([rule])

    from indexer import ALL_SUPPORTED
    total = kept = excluded = 0
    examples: list[str] = []

    for path_str in scan_paths:
        root = Path(path_str)
        if not root.exists():
            continue
        for fp in root.rglob("*"):
            if not fp.is_file() or fp.suffix.lower() not in ALL_SUPPORTED:
                continue
            total += 1
            ok, _ = fs.should_index(fp)
            if ok:
                kept += 1
            else:
                excluded += 1
                if len(examples) < 10:
                    examples.append(fp.name)

    return {
        "total_files":    total,
        "would_exclude":  excluded,
        "would_keep":     kept,
        "example_files":  examples,
    }
