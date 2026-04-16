"""
Sherlock calendar worker: monitors attorney calendars via Microsoft Graph API,
surfaces upcoming deadlines, and creates prep tasks.

Capabilities:
  - Pull events for the next 60 days from each monitored mailbox's calendar
  - Detect court hearings, depositions, mediations, filing deadlines
  - Cross-reference against existing sherlock_tasks to flag unprepped events
  - Create "prep" tasks: draft motions, prepare exhibits, file responses
  - Detect travel/conflict overlaps between back-to-back hearings

Runs as: python calendar_worker.py [once|watch]
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = os.environ.get("DATA_DIR", os.path.expanduser("~/Sherlock/data"))
DB_PATH = os.path.join(DATA_DIR, "sherlock_tasks.db")
STATUS_PATH = os.path.join(DATA_DIR, "calendar_status.json")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
POLL_INTERVAL = int(os.environ.get("CALENDAR_POLL", "600"))  # 10 min default
LOOKAHEAD_DAYS = int(os.environ.get("CALENDAR_LOOKAHEAD", "60"))

log = logging.getLogger(__name__)


# ── Legal event detection ───────────────────────────────────────────────────

# Patterns that indicate a court event (case-insensitive)
COURT_EVENT_RE = re.compile(
    r"(hearing|deposition|mediation|arbitration|trial|conference|"
    r"status\s+conference|calendar\s+call|motion|MSJ|"
    r"summary\s+judgment|oral\s+argument|arraignment|"
    r"pretrial|pre-trial|settlement|ADR|"
    r"discovery\s+deadline|filing\s+deadline|response\s+due)",
    re.IGNORECASE,
)

DEADLINE_RE = re.compile(
    r"(deadline|due\s+date|response\s+due|must\s+be\s+filed|"
    r"last\s+day|expir|statute\s+of\s+limitations)",
    re.IGNORECASE,
)


def _is_legal_event(subject: str, body: str = "") -> bool:
    text = f"{subject} {body}"
    return bool(COURT_EVENT_RE.search(text))


def _is_deadline(subject: str, body: str = "") -> bool:
    text = f"{subject} {body}"
    return bool(DEADLINE_RE.search(text))


# ── Database ────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = sqlite3.Row

    # Shared sherlock_tasks table (email_worker creates it if it runs first)
    db.execute("""CREATE TABLE IF NOT EXISTS sherlock_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL DEFAULT 'calendar',
        source_id TEXT,
        source_ref TEXT,
        assignee TEXT,
        action TEXT NOT NULL,
        client_or_case TEXT,
        case_folder TEXT,
        priority TEXT DEFAULT 'normal',
        due_hint TEXT,
        status TEXT DEFAULT 'pending',
        notes TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )""")

    # Calendar event tracking
    db.execute("""CREATE TABLE IF NOT EXISTS calendar_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE NOT NULL,
        mailbox TEXT NOT NULL,
        subject TEXT,
        start_time TEXT,
        end_time TEXT,
        location TEXT,
        body_preview TEXT,
        is_legal_event INTEGER DEFAULT 0,
        is_deadline INTEGER DEFAULT 0,
        has_prep_task INTEGER DEFAULT 0,
        task_json TEXT,
        processed_at TEXT
    )""")

    db.execute("CREATE INDEX IF NOT EXISTS idx_cal_eventid ON calendar_events(event_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source ON sherlock_tasks(source)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON sherlock_tasks(status)")
    db.commit()
    return db


# ── NAS case folder matching (shared with email/dictation workers) ──────────

NAS_CLIENT = os.environ.get("NAS_CLIENT_DIR", os.path.expanduser("~/NAS/Client Data"))
_case_folders: dict[str, str] | None = None


def _load_case_folders() -> dict[str, str]:
    global _case_folders
    if _case_folders is not None:
        return _case_folders
    _case_folders = {}
    if not os.path.isdir(NAS_CLIENT):
        return _case_folders
    for category in os.listdir(NAS_CLIENT):
        cat_path = os.path.join(NAS_CLIENT, category)
        if not os.path.isdir(cat_path):
            continue
        for folder in os.listdir(cat_path):
            folder_path = os.path.join(cat_path, folder)
            if not os.path.isdir(folder_path):
                continue
            name_lower = folder.lower().strip()
            _case_folders[name_lower] = folder_path
            for part in re.split(r'[,\s\-]+', name_lower):
                part = part.strip()
                if len(part) >= 3:
                    _case_folders.setdefault(part, folder_path)
    return _case_folders


def match_case_folder(client_or_case: str | None) -> str | None:
    if not client_or_case:
        return None
    folders = _load_case_folders()
    query = client_or_case.lower().strip()
    if query in folders:
        return folders[query]
    words = re.split(r'[,\s]+', query)
    if len(words) >= 2:
        for i in range(len(words)):
            candidate = f"{words[i]}, {' '.join(w for j, w in enumerate(words) if j != i)}"
            if candidate in folders:
                return folders[candidate]
    for word in words:
        if len(word) >= 4 and word in folders:
            return folders[word]
    return None


# ── Task extraction via Ollama ──────────────────────────────────────────────

CALENDAR_EXTRACT_PROMPT = """You are an AI assistant for a law firm run by attorney Sam Dennis. Below is a calendar event from his schedule.

Event: {subject}
When: {start_time} to {end_time}
Location: {location}
Details: {body}

Determine what preparation tasks are needed for this event. Consider:
- Court hearings need: review case file, prepare arguments, file any pending motions
- Depositions need: prepare questions, review witness statements, coordinate with client
- Mediations need: prepare settlement demands, organize exhibits, brief client
- Filing deadlines need: draft document, review, file with court
- Client meetings need: review case status, prepare updates

Extract preparation tasks as a JSON array. For each task:
- "assignee": who handles it ("Sam Dennis" for attorney work, "Tara" for admin/filing)
- "action": clear description of the prep task
- "client_or_case": the client or case name (use "Last, First" format, or null)
- "priority": "urgent" if event is within 7 days, otherwise "normal"
- "due_hint": when the prep should be done (typically 1-3 days before the event)

If the event needs no preparation (lunch, personal, etc.), respond with: []

Respond ONLY with a valid JSON array, no other text."""


def extract_calendar_tasks(subject: str, start_time: str, end_time: str,
                           location: str, body: str) -> list[dict]:
    import urllib.request

    prompt = CALENDAR_EXTRACT_PROMPT.format(
        subject=subject, start_time=start_time, end_time=end_time,
        location=location or "(none)",
        body=body[:3000] if body else "(none)",
    )
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 2048},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    response_text = data.get("response", "")
    start = response_text.find("[")
    end = response_text.rfind("]") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(response_text[start:end])
        except json.JSONDecodeError:
            pass
    return []


# ── Graph API calendar fetching ─────────────────────────────────────────────

_HTML_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return _HTML_TAG.sub(" ", s).strip()


def fetch_upcoming_events(graph, mailbox: str,
                          lookahead_days: int = LOOKAHEAD_DAYS) -> list[dict]:
    """Fetch upcoming calendar events from a mailbox."""
    now = datetime.utcnow()
    end = now + timedelta(days=lookahead_days)

    endpoint = f"/users/{mailbox}/calendarview"
    params = {
        "startdatetime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enddatetime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "$select": "id,subject,start,end,location,body,organizer,isAllDay",
        "$orderby": "start/dateTime",
        "$top": "100",
    }

    try:
        events = graph.get_all_pages(endpoint, params, max_pages=5)
    except Exception as e:
        log.error("Graph calendar fetch failed for %s: %s", mailbox, e)
        return []

    results = []
    for ev in events:
        start_dt = ev.get("start", {}).get("dateTime", "")
        end_dt = ev.get("end", {}).get("dateTime", "")
        loc = ev.get("location", {})
        location = loc.get("displayName", "") if isinstance(loc, dict) else str(loc)

        results.append({
            "event_id": ev["id"],
            "subject": ev.get("subject", "(no subject)"),
            "start_time": start_dt,
            "end_time": end_dt,
            "location": location,
            "body": _clean(ev.get("body", {}).get("content", "")),
            "is_all_day": ev.get("isAllDay", False),
        })
    return results


# ── Conflict detection ──────────────────────────────────────────────────────

def detect_conflicts(events: list[dict]) -> list[str]:
    """Find back-to-back or overlapping events that might conflict."""
    warnings: list[str] = []
    legal = [e for e in events if _is_legal_event(e["subject"], e.get("body", ""))]

    for i in range(len(legal) - 1):
        a = legal[i]
        b = legal[i + 1]
        a_end = a["end_time"]
        b_start = b["start_time"]
        if a_end and b_start and a_end[:10] == b_start[:10]:
            # Same day — check for tight turnaround
            try:
                t_a = datetime.fromisoformat(a_end.replace("Z", "+00:00"))
                t_b = datetime.fromisoformat(b_start.replace("Z", "+00:00"))
                gap = (t_b - t_a).total_seconds() / 60
                if gap < 60:
                    warnings.append(
                        f"Tight turnaround ({int(gap)} min) between "
                        f"'{a['subject']}' and '{b['subject']}' on {a_end[:10]}"
                    )
            except Exception:
                pass
    return warnings


# ── Main processing ─────────────────────────────────────────────────────────

def process_calendars(db: sqlite3.Connection, graph) -> int:
    config = graph.config
    mailboxes = list(config.get("monitored_mailboxes", []))
    _load_case_folders()
    processed = 0

    for mailbox in mailboxes:
        log.info("Checking calendar: %s", mailbox)
        events = fetch_upcoming_events(graph, mailbox)
        log.info("  %d upcoming events (next %d days)", len(events), LOOKAHEAD_DAYS)

        # Conflict detection
        conflicts = detect_conflicts(events)
        for c in conflicts:
            log.warning("  CONFLICT: %s", c)

        for event in events:
            # Skip if already processed
            existing = db.execute(
                "SELECT id, has_prep_task FROM calendar_events WHERE event_id = ?",
                (event["event_id"],)
            ).fetchone()
            if existing:
                continue

            subject = event["subject"]
            is_legal = _is_legal_event(subject, event.get("body", ""))
            is_deadline_ev = _is_deadline(subject, event.get("body", ""))

            # Only run LLM extraction on legal events and deadlines
            tasks = []
            if is_legal or is_deadline_ev:
                log.info("  Legal event: %s (%s)", subject, event["start_time"][:10])
                tasks = extract_calendar_tasks(
                    subject, event["start_time"], event["end_time"],
                    event["location"], event.get("body", ""),
                )

            db.execute("""INSERT OR IGNORE INTO calendar_events
                (event_id, mailbox, subject, start_time, end_time, location,
                 body_preview, is_legal_event, is_deadline, has_prep_task,
                 task_json, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event["event_id"], mailbox, subject,
                 event["start_time"], event["end_time"], event["location"],
                 event.get("body", "")[:500],
                 int(is_legal), int(is_deadline_ev), int(len(tasks) > 0),
                 json.dumps(tasks), datetime.utcnow().isoformat()))

            for i, task in enumerate(tasks):
                case_name = task.get("client_or_case")
                case_folder = match_case_folder(case_name)

                # Auto-upgrade priority for events within 7 days
                priority = task.get("priority", "normal")
                try:
                    event_date = datetime.fromisoformat(
                        event["start_time"].replace("Z", "+00:00")
                    )
                    if (event_date - datetime.now(event_date.tzinfo)).days <= 7:
                        priority = "urgent"
                except Exception:
                    pass

                db.execute("""INSERT INTO sherlock_tasks
                    (source, source_id, source_ref, assignee, action,
                     client_or_case, case_folder, priority, due_hint,
                     status, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    ("calendar", event["event_id"],
                     f"Event: {subject}\nDate: {event['start_time'][:10]}",
                     task.get("assignee", "unknown"),
                     task.get("action", ""),
                     case_name, case_folder, priority,
                     task.get("due_hint"),
                     f"Prep for: {subject} on {event['start_time'][:10]}",
                     datetime.utcnow().isoformat()))

                if case_folder:
                    log.info("    Task: %s → %s (case: %s)",
                             task.get("assignee"), task.get("action"),
                             os.path.basename(case_folder))

            db.commit()
            processed += 1

    return processed


# ── Status ──────────────────────────────────────────────────────────────────

def write_status(info: dict):
    info["updated_at"] = datetime.utcnow().isoformat()
    info["pid"] = os.getpid()
    with open(STATUS_PATH, "w") as f:
        json.dump(info, f)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    os.makedirs(DATA_DIR, exist_ok=True)
    db = init_db()

    from graph_auth import GraphClient
    try:
        graph = GraphClient()
    except Exception as e:
        log.error("Graph API initialization failed: %s", e)
        sys.exit(1)

    mode = sys.argv[1] if len(sys.argv) > 1 else "once"

    if mode == "watch":
        log.info("Calendar worker watching every %ds", POLL_INTERVAL)
        write_status({"stage": "watching", "processed": 0})
        while True:
            try:
                n = process_calendars(db, graph)
                total = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='calendar'").fetchone()[0]
                pending = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='calendar' AND status='pending'").fetchone()[0]
                write_status({
                    "stage": "idle", "total_tasks": total,
                    "pending_tasks": pending, "last_batch": n,
                })
                if n:
                    log.info("Processed %d events, %d total tasks (%d pending)", n, total, pending)
            except Exception as e:
                log.error("Calendar worker error: %s", e)
            time.sleep(POLL_INTERVAL)
    else:
        n = process_calendars(db, graph)
        total = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='calendar'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='calendar' AND status='pending'").fetchone()[0]
        write_status({"stage": "done", "total_tasks": total, "pending_tasks": pending, "last_batch": n})
        log.info("Done: %d events processed, %d total tasks (%d pending)", n, total, pending)

    db.close()


if __name__ == "__main__":
    main()
