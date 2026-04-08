"""Dictation worker: watches uDictate folder, transcribes audio, extracts tasks via LLM."""

from __future__ import annotations
import json, os, sqlite3, time, sys, re
from pathlib import Path
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
DICTATE_DIR   = os.environ.get("DICTATE_DIR", os.path.expanduser("~/NAS/uDictate"))
DATA_DIR      = os.environ.get("DATA_DIR", os.path.expanduser("~/Sherlock/data"))
DB_PATH       = os.path.join(DATA_DIR, "dictations.db")
STATUS_PATH   = os.path.join(DATA_DIR, "dictation_status.json")
OLLAMA_URL    = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL  = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
POLL_INTERVAL = int(os.environ.get("DICTATION_POLL", "30"))  # seconds

# ── Database ────────────────────────────────────────────────────────────────
def init_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS dictations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT UNIQUE NOT NULL,
        file_path TEXT NOT NULL,
        recorded_at TEXT,
        duration_secs INTEGER,
        transcript TEXT,
        transcribed_at TEXT,
        task_json TEXT,
        analyzed_at TEXT,
        status TEXT DEFAULT 'pending'
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS dictation_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dictation_id INTEGER NOT NULL,
        task_order INTEGER NOT NULL DEFAULT 0,
        assignee TEXT,
        action TEXT NOT NULL,
        client_or_case TEXT,
        priority TEXT DEFAULT 'normal',
        due_hint TEXT,
        status TEXT DEFAULT 'pending',
        notes TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (dictation_id) REFERENCES dictations(id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON dictation_tasks(status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON dictation_tasks(assignee)")
    db.commit()
    return db


def parse_filename(name: str):
    """Extract metadata from uDictate filename.
    Format: DEFAULT_8062_07Apr26_111730AM_00_01_30.m4a
    """
    m = re.match(
        r'DEFAULT_(\d+)_(\d{2})(\w{3})(\d{2})_(\d{6})(AM|PM)_(\d{2})_(\d{2})_(\d{2})\.m4a',
        name
    )
    if not m:
        return None, None
    seq, day, mon, yr, hhmmss, ampm, dh, dm, ds = m.groups()
    months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
              'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
    hour = int(hhmmss[:2])
    minute = int(hhmmss[2:4])
    sec = int(hhmmss[4:6])
    if ampm == 'PM' and hour != 12:
        hour += 12
    if ampm == 'AM' and hour == 12:
        hour = 0
    try:
        dt = datetime(2000 + int(yr), months.get(mon, 1), int(day), hour, minute, sec)
        recorded_at = dt.isoformat()
    except Exception:
        recorded_at = None
    duration = int(dh) * 3600 + int(dm) * 60 + int(ds)
    return recorded_at, duration


# ── Transcription ───────────────────────────────────────────────────────────
_whisper = None
def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print(f"[dictation] Whisper model '{WHISPER_MODEL}' loaded")
    return _whisper


def transcribe(file_path: str) -> str:
    model = get_whisper()
    segments, info = model.transcribe(file_path, beam_size=5)
    return " ".join(seg.text.strip() for seg in segments)


# ── Task Extraction via Ollama ──────────────────────────────────────────────
EXTRACT_PROMPT = """You are an AI assistant for a law firm. Below is a transcription of a voice dictation from attorney Sam Dennis to his assistant(s). Sam often gives multiple instructions in a single recording.

Extract each distinct task/instruction as a JSON array. For each task:
- "assignee": who should do it (e.g. "Tara", "Terry", or "unknown" if unclear)
- "action": clear description of what needs to be done
- "client_or_case": the client name, case name, or matter referenced (or null if general)
- "priority": "urgent" if time-sensitive language is used, otherwise "normal"
- "due_hint": any mentioned deadline or timing (or null)

Important:
- Split compound instructions into separate tasks
- Clean up transcription artifacts (misheard words are common)
- Preserve names of people, cases, and legal terminology as best you can
- If Sam mentions something is just a personal note/reminder to himself, still capture it but note that

Respond ONLY with a valid JSON array, no other text.

TRANSCRIPTION:
{transcript}"""


def extract_tasks(transcript: str) -> list[dict]:
    import urllib.request
    prompt = EXTRACT_PROMPT.format(transcript=transcript)
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 2048}
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    response_text = data.get("response", "")
    # Extract JSON array from response
    start = response_text.find("[")
    end = response_text.rfind("]") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(response_text[start:end])
        except json.JSONDecodeError:
            pass
    print(f"[dictation] WARNING: Could not parse LLM response as JSON")
    return [{"assignee": "unknown", "action": response_text.strip(), "client_or_case": None,
             "priority": "normal", "due_hint": None}]


# ── Status ──────────────────────────────────────────────────────────────────
def write_status(info: dict):
    info["updated_at"] = datetime.utcnow().isoformat()
    info["pid"] = os.getpid()
    with open(STATUS_PATH, "w") as f:
        json.dump(info, f)


# ── Main Loop ───────────────────────────────────────────────────────────────
def process_new_files(db: sqlite3.Connection):
    """Scan for new dictation files, transcribe and extract tasks."""
    if not os.path.isdir(DICTATE_DIR):
        print(f"[dictation] Directory not found: {DICTATE_DIR}")
        return 0

    files = sorted(f for f in os.listdir(DICTATE_DIR) if f.lower().endswith('.m4a'))
    existing = set(r[0] for r in db.execute("SELECT file_name FROM dictations").fetchall())
    new_files = [f for f in files if f not in existing]

    if not new_files:
        return 0

    print(f"[dictation] Found {len(new_files)} new dictation(s)")
    processed = 0

    for fname in new_files:
        fpath = os.path.join(DICTATE_DIR, fname)
        recorded_at, duration = parse_filename(fname)

        write_status({
            "stage": "transcribing",
            "current_file": fname,
            "processed": processed,
            "total_new": len(new_files),
        })

        try:
            # Step 1: Transcribe
            print(f"[dictation] Transcribing {fname}...", end=" ", flush=True)
            t0 = time.time()
            transcript = transcribe(fpath)
            t_transcribe = time.time() - t0
            print(f"✓ ({t_transcribe:.1f}s, {len(transcript)} chars)")

            db.execute("""INSERT OR IGNORE INTO dictations
                (file_name, file_path, recorded_at, duration_secs, transcript, transcribed_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'transcribed')""",
                (fname, fpath, recorded_at, duration, transcript, datetime.utcnow().isoformat()))
            db.commit()

            dictation_id = db.execute(
                "SELECT id FROM dictations WHERE file_name = ?", (fname,)
            ).fetchone()[0]

            # Step 2: Extract tasks via LLM
            write_status({
                "stage": "analyzing",
                "current_file": fname,
                "processed": processed,
                "total_new": len(new_files),
            })

            print(f"[dictation] Extracting tasks from {fname}...", end=" ", flush=True)
            t0 = time.time()
            tasks = extract_tasks(transcript)
            t_extract = time.time() - t0
            print(f"✓ ({t_extract:.1f}s, {len(tasks)} tasks)")

            db.execute("""UPDATE dictations SET task_json = ?, analyzed_at = ?, status = 'analyzed'
                WHERE id = ?""", (json.dumps(tasks), datetime.utcnow().isoformat(), dictation_id))

            for i, task in enumerate(tasks):
                db.execute("""INSERT INTO dictation_tasks
                    (dictation_id, task_order, assignee, action, client_or_case,
                     priority, due_hint, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (dictation_id, i + 1,
                     task.get("assignee", "unknown"),
                     task.get("action", ""),
                     task.get("client_or_case"),
                     task.get("priority", "normal"),
                     task.get("due_hint"),
                     datetime.utcnow().isoformat()))
            db.commit()

            processed += 1
            print(f"[dictation] ✓ {fname}: {len(tasks)} tasks extracted")

        except Exception as e:
            print(f"[dictation] ✗ {fname}: {e}")
            db.execute("""INSERT OR REPLACE INTO dictations
                (file_name, file_path, recorded_at, duration_secs, status)
                VALUES (?, ?, ?, ?, 'error')""",
                (fname, fpath, recorded_at, duration))
            db.commit()

    return processed


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = init_db()
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"

    if mode == "watch":
        print(f"[dictation] Watching {DICTATE_DIR} every {POLL_INTERVAL}s")
        write_status({"stage": "watching", "processed": 0, "total_new": 0})
        while True:
            try:
                n = process_new_files(db)
                if n:
                    total = db.execute("SELECT COUNT(*) FROM dictation_tasks").fetchone()[0]
                    pending = db.execute("SELECT COUNT(*) FROM dictation_tasks WHERE status='pending'").fetchone()[0]
                    write_status({"stage": "idle", "total_tasks": total, "pending_tasks": pending, "last_batch": n})
                    print(f"[dictation] Batch done: {n} files → {total} total tasks ({pending} pending)")
            except Exception as e:
                print(f"[dictation] Error in watch loop: {e}")
            time.sleep(POLL_INTERVAL)
    else:
        n = process_new_files(db)
        total = db.execute("SELECT COUNT(*) FROM dictation_tasks").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM dictation_tasks WHERE status='pending'").fetchone()[0]
        write_status({"stage": "done", "total_tasks": total, "pending_tasks": pending, "last_batch": n})
        print(f"\n[dictation] Complete: {n} files processed, {total} total tasks ({pending} pending)")

    db.close()


if __name__ == "__main__":
    main()
