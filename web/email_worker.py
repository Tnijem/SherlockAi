"""
Sherlock email worker: monitors attorney mailboxes via Microsoft Graph API,
extracts actionable items via LLM, and writes them to the unified task DB.

Two input channels:
  1. Sherlock's own inbox (CC'd items — explicit "process this" signal)
  2. Delegated read of attorney mailboxes (ambient scan)

Filters:
  - Only processes unread mail by default
  - Skips newsletters, marketing, automated notifications
  - Prioritizes: court domains, opposing counsel, clients matching NAS folders
  - Extracts deadlines, action items, and attachments

Runs as: python email_worker.py [once|watch]
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
STATUS_PATH = os.path.join(DATA_DIR, "email_status.json")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
POLL_INTERVAL = int(os.environ.get("EMAIL_POLL", "120"))  # seconds

# Domains that are almost always actionable for a law firm
PRIORITY_DOMAINS = {
    "uscourts.gov", "flcourts.org", "gasupreme.us", "gactapp.us",
    "floridasupremecourt.org", "courtlistener.com",
    # State bar associations
    "gabar.org", "floridabar.org",
}

# Skip patterns — subjects/senders that are never actionable
SKIP_PATTERNS = [
    re.compile(r"(unsubscribe|newsletter|marketing|noreply|no-reply)", re.I),
    re.compile(r"(out of office|automatic reply|auto-reply)", re.I),
    re.compile(r"(password reset|verify your email|confirm your account)", re.I),
]

log = logging.getLogger(__name__)


# ── Database ────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    """Create/upgrade the unified sherlock_tasks database."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.row_factory = sqlite3.Row

    # Unified tasks table — shared with dictation_worker and calendar_worker.
    # The `source` column distinguishes origin: dictation | email | calendar.
    db.execute("""CREATE TABLE IF NOT EXISTS sherlock_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL DEFAULT 'email',
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

    # Email tracking — which messages we've already processed.
    db.execute("""CREATE TABLE IF NOT EXISTS email_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT UNIQUE NOT NULL,
        mailbox TEXT NOT NULL,
        sender TEXT,
        subject TEXT,
        received_at TEXT,
        body_preview TEXT,
        is_actionable INTEGER DEFAULT 0,
        task_json TEXT,
        processed_at TEXT,
        channel TEXT DEFAULT 'monitored'
    )""")

    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_source ON sherlock_tasks(source)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON sherlock_tasks(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON sherlock_tasks(assignee)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_email_msgid ON email_messages(message_id)")
    db.commit()
    return db


# ── NAS Case Folder Matching (shared with dictation_worker) ─────────────────

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


# ── Email filtering ─────────────────────────────────────────────────────────

def _is_priority_sender(sender_email: str) -> bool:
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    return any(domain.endswith(d) for d in PRIORITY_DOMAINS)


def _should_skip(subject: str, sender: str) -> bool:
    text = f"{subject} {sender}"
    return any(p.search(text) for p in SKIP_PATTERNS)


# ── Task extraction via Ollama ──────────────────────────────────────────────

EMAIL_EXTRACT_PROMPT = """You are an AI assistant for a law firm run by attorney Sam Dennis. Below is an email received in his firm's mailbox.

From: {sender}
Subject: {subject}
Date: {received_at}
Body:
{body}

Determine if this email contains actionable items for the firm. If yes, extract each task as a JSON array. For each task:
- "assignee": who should handle it ("Sam Dennis" if it requires attorney action, "Tara" for admin tasks, "unknown" if unclear)
- "action": clear description of what needs to be done
- "client_or_case": the client or case name referenced (use "Last, First" format when possible, or null if general)
- "priority": "urgent" if there's a court deadline, hearing, or time-sensitive language; otherwise "normal"
- "due_hint": any mentioned deadline or due date (or null)

If the email is purely informational with NO action needed (newsletter, FYI, receipt, etc.), respond with an empty array: []

Important:
- Court orders and scheduling notices are ALWAYS actionable
- Discovery requests and responses need tasks created
- Opposing counsel communications usually need a response task
- Bills, receipts, and marketing emails are NOT actionable

Respond ONLY with a valid JSON array, no other text."""


def extract_email_tasks(sender: str, subject: str, received_at: str, body: str) -> list[dict]:
    import urllib.request

    prompt = EMAIL_EXTRACT_PROMPT.format(
        sender=sender, subject=subject, received_at=received_at,
        body=body[:4000],  # cap body length for LLM context
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
    # If we can't parse, return empty (assume non-actionable)
    log.warning("Could not parse LLM response for email: %s", subject)
    return []


# ── Graph API email fetching ────────────────────────────────────────────────

def fetch_unread_emails(graph, mailbox: str, since_hours: int = 24,
                        channel: str = "monitored") -> list[dict]:
    """Fetch unread emails from a mailbox via Graph API.

    Returns list of dicts with: message_id, sender, subject, received_at,
    body_preview, body_text, channel.
    """
    since = (datetime.utcnow() - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    endpoint = f"/users/{mailbox}/messages"
    params = {
        "$filter": f"isRead eq false and receivedDateTime ge {since}",
        "$select": "id,from,subject,receivedDateTime,bodyPreview,body,hasAttachments",
        "$orderby": "receivedDateTime desc",
        "$top": "50",
    }

    try:
        messages = graph.get_all_pages(endpoint, params, max_pages=5)
    except Exception as e:
        log.error("Graph API fetch failed for %s: %s", mailbox, e)
        return []

    results = []
    for msg in messages:
        sender_info = msg.get("from", {}).get("emailAddress", {})
        sender_email = sender_info.get("address", "")
        sender_name = sender_info.get("name", sender_email)

        results.append({
            "message_id": msg["id"],
            "sender": f"{sender_name} <{sender_email}>",
            "sender_email": sender_email,
            "subject": msg.get("subject", "(no subject)"),
            "received_at": msg.get("receivedDateTime", ""),
            "body_preview": msg.get("bodyPreview", ""),
            "body_text": _clean_html(msg.get("body", {}).get("content", "")),
            "has_attachments": msg.get("hasAttachments", False),
            "channel": channel,
        })
    return results


_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _clean_html(s: str) -> str:
    s = _HTML_TAG.sub(" ", s)
    s = _WS.sub("\n\n", s)
    return s.strip()


# ── Main processing ─────────────────────────────────────────────────────────

def process_emails(db: sqlite3.Connection, graph) -> int:
    """Fetch and process new emails from all monitored mailboxes."""
    config = graph.config
    mailboxes = list(config.get("monitored_mailboxes", []))
    service_acct = config.get("service_account", "")

    # Also check Sherlock's own inbox for CC'd items
    if service_acct and service_acct not in mailboxes:
        mailboxes.append(service_acct)

    _load_case_folders()
    processed = 0

    for mailbox in mailboxes:
        channel = "cc_inbox" if mailbox == service_acct else "monitored"
        log.info("Checking mailbox: %s (channel=%s)", mailbox, channel)

        emails = fetch_unread_emails(graph, mailbox, since_hours=24, channel=channel)
        log.info("  %d unread emails", len(emails))

        for email in emails:
            # Skip if already processed
            existing = db.execute(
                "SELECT id FROM email_messages WHERE message_id = ?",
                (email["message_id"],)
            ).fetchone()
            if existing:
                continue

            sender = email["sender"]
            subject = email["subject"]

            # Skip obvious non-actionable
            if _should_skip(subject, sender):
                db.execute("""INSERT OR IGNORE INTO email_messages
                    (message_id, mailbox, sender, subject, received_at,
                     body_preview, is_actionable, processed_at, channel)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (email["message_id"], mailbox, sender, subject,
                     email["received_at"], email["body_preview"][:500],
                     datetime.utcnow().isoformat(), channel))
                db.commit()
                continue

            # CC'd items to Sherlock's inbox are always processed
            # Priority-domain senders are always processed
            # For other emails, use the LLM to decide
            is_priority = _is_priority_sender(email.get("sender_email", ""))
            is_cc = channel == "cc_inbox"

            log.info("  Processing: %s (priority=%s, cc=%s)", subject, is_priority, is_cc)

            body = email["body_text"] or email["body_preview"]
            tasks = extract_email_tasks(sender, subject, email["received_at"], body)

            is_actionable = len(tasks) > 0
            db.execute("""INSERT OR IGNORE INTO email_messages
                (message_id, mailbox, sender, subject, received_at,
                 body_preview, is_actionable, task_json, processed_at, channel)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (email["message_id"], mailbox, sender, subject,
                 email["received_at"], email["body_preview"][:500],
                 int(is_actionable), json.dumps(tasks),
                 datetime.utcnow().isoformat(), channel))

            for i, task in enumerate(tasks):
                case_name = task.get("client_or_case")
                case_folder = match_case_folder(case_name)
                priority = task.get("priority", "normal")
                if is_priority:
                    priority = "urgent"

                db.execute("""INSERT INTO sherlock_tasks
                    (source, source_id, source_ref, assignee, action,
                     client_or_case, case_folder, priority, due_hint,
                     status, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    ("email", email["message_id"],
                     f"From: {sender}\nSubject: {subject}",
                     task.get("assignee", "unknown"),
                     task.get("action", ""),
                     case_name, case_folder, priority,
                     task.get("due_hint"),
                     f"Source email: {subject}",
                     datetime.utcnow().isoformat()))

                if case_folder:
                    log.info("    Task: %s → %s (matched: %s)",
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

    # Import here so the module can be imported without Graph deps
    from graph_auth import GraphClient
    try:
        graph = GraphClient()
    except Exception as e:
        log.error("Graph API initialization failed: %s", e)
        log.error("Ensure firm.yaml has email config and SHERLOCK_O365_SECRET is set")
        sys.exit(1)

    mode = sys.argv[1] if len(sys.argv) > 1 else "once"

    if mode == "watch":
        log.info("Email worker watching every %ds", POLL_INTERVAL)
        write_status({"stage": "watching", "processed": 0})
        while True:
            try:
                n = process_emails(db, graph)
                total = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='email'").fetchone()[0]
                pending = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='email' AND status='pending'").fetchone()[0]
                write_status({
                    "stage": "idle",
                    "total_tasks": total,
                    "pending_tasks": pending,
                    "last_batch": n,
                })
                if n:
                    log.info("Processed %d emails, %d total email tasks (%d pending)", n, total, pending)
            except Exception as e:
                log.error("Email worker error: %s", e)
            time.sleep(POLL_INTERVAL)
    else:
        n = process_emails(db, graph)
        total = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='email'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM sherlock_tasks WHERE source='email' AND status='pending'").fetchone()[0]
        write_status({"stage": "done", "total_tasks": total, "pending_tasks": pending, "last_batch": n})
        log.info("Done: %d emails processed, %d total tasks (%d pending)", n, total, pending)

    db.close()


if __name__ == "__main__":
    main()
