"""Dictation worker: watches uDictate folder, transcribes audio, extracts tasks via LLM,
links cases to NAS folders."""

from __future__ import annotations
import json, os, sqlite3, time, sys, re
from pathlib import Path
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
DICTATE_DIR   = os.environ.get("DICTATE_DIR", os.path.expanduser("~/NAS/uDictate"))
NAS_CLIENT    = os.environ.get("NAS_CLIENT_DIR", os.path.expanduser("~/NAS/Client Data"))
DATA_DIR      = os.environ.get("DATA_DIR", os.path.expanduser("~/Sherlock/data"))
DB_PATH       = os.path.join(DATA_DIR, "dictations.db")
STATUS_PATH   = os.path.join(DATA_DIR, "dictation_status.json")
OLLAMA_URL    = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL  = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3")
POLL_INTERVAL = int(os.environ.get("DICTATION_POLL", "30"))  # seconds

# ── Legal vocabulary prompt for Whisper ─────────────────────────────────────
# Seed Whisper with names/terms it's likely to hear. Dramatically improves accuracy
# for proper nouns, legal terms, and case names.
WHISPER_PROMPT = (
    "Sam Dennis, Terry, Tara, Nicole, Jeremy, Blake, Hala, Luke Claus, Aaron Dobby. "
    "Caleb Abney, Jackson, James Vann, Maggie Roddish, Justin Purvis, Gary Luton, "
    "David Sawat, Miriam, Derek McLeod, Terrell MacJoseph, Joe Moore, Sid Staking, "
    "Tiffany, Mylon, Brandon Allen Purvis, Daniel Marie Pilcher, Cameron Adams, "
    "Judge Timothy Hamill. "
    "Plaintiff, defendant, deposition, mediation, arbitration, discovery, "
    "interrogatories, subpoena, motion to compel, summary judgment, "
    "alternative dispute resolution, service of process, default judgment, "
    "statute of limitations, personal injury, medical malpractice, workers compensation, "
    "premises liability, wrongful death, autopsy report, police report, "
    "Ben Hill County, conflict of interest, attorney-client privilege."
)

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
        case_folder TEXT,
        priority TEXT DEFAULT 'normal',
        due_hint TEXT,
        status TEXT DEFAULT 'pending',
        notes TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (dictation_id) REFERENCES dictations(id)
    )""")
    # Add case_folder column if upgrading from older schema
    try:
        db.execute("ALTER TABLE dictation_tasks ADD COLUMN case_folder TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
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


# ── NAS Case Folder Matching ───────────────────────────────────────────────
_case_folders = None

def _load_case_folders():
    """Build a lookup of client/case names → NAS folder paths."""
    global _case_folders
    if _case_folders is not None:
        return _case_folders

    _case_folders = {}
    if not os.path.isdir(NAS_CLIENT):
        print(f"[dictation] NAS Client Data not found: {NAS_CLIENT}")
        return _case_folders

    for category in os.listdir(NAS_CLIENT):
        cat_path = os.path.join(NAS_CLIENT, category)
        if not os.path.isdir(cat_path):
            continue
        for folder in os.listdir(cat_path):
            folder_path = os.path.join(cat_path, folder)
            if not os.path.isdir(folder_path):
                continue
            # Index by folder name (e.g. "Abney, Caleb") and parts
            name_lower = folder.lower().strip()
            _case_folders[name_lower] = folder_path
            # Also index by individual name parts for fuzzy matching
            parts = re.split(r'[,\s\-]+', name_lower)
            for part in parts:
                part = part.strip()
                if len(part) >= 3:  # skip very short fragments
                    if part not in _case_folders:
                        _case_folders[part] = folder_path

    print(f"[dictation] Loaded {len(_case_folders)} case folder entries")
    return _case_folders


def match_case_folder(client_or_case: str) -> str | None:
    """Try to match a client/case name from LLM output to a NAS folder."""
    if not client_or_case:
        return None

    folders = _load_case_folders()
    if not folders:
        return None

    query = client_or_case.lower().strip()

    # Direct match
    if query in folders:
        return folders[query]

    # Try "Last, First" format
    # Input might be "Caleb Abney" → try "abney, caleb"
    words = re.split(r'[,\s]+', query)
    if len(words) >= 2:
        # Try last-first
        for i in range(len(words)):
            last = words[i]
            firsts = [w for j, w in enumerate(words) if j != i]
            candidate = f"{last}, {' '.join(firsts)}"
            if candidate in folders:
                return folders[candidate]

    # Try matching any single significant word (surname)
    for word in words:
        word = word.strip()
        if len(word) >= 4 and word in folders:
            return folders[word]

    # Substring search as last resort
    for key, path in folders.items():
        if ',' in key and len(key) >= 5:  # only match full "Last, First" entries
            key_parts = set(re.split(r'[,\s]+', key))
            query_parts = set(words)
            # If all significant parts of the folder name appear in the query
            if key_parts and key_parts.issubset(query_parts):
                return path

    return None


# ── Transcription ───────────────────────────────────────────────────────────
_whisper = None
def get_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        print(f"[dictation] Loading Whisper model '{WHISPER_MODEL}'... (this may take a minute)")
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print(f"[dictation] Whisper model '{WHISPER_MODEL}' loaded")
    return _whisper


def _load_learned_vocab():
    """Load user corrections to append to Whisper prompt."""
    vocab_path = os.path.join(DATA_DIR, "learned_vocab.txt")
    if os.path.exists(vocab_path):
        with open(vocab_path) as f:
            terms = [line.strip() for line in f if line.strip()]
        if terms:
            return " " + ", ".join(terms) + "."
    return ""


def _load_vocab_replacements():
    """Load wrong->correct mappings to apply post-transcription."""
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        rows = db.execute("SELECT wrong, correct FROM vocab_corrections").fetchall()
        db.close()
        return rows
    except Exception:
        return []


def transcribe(file_path: str) -> str:
    model = get_whisper()
    prompt = WHISPER_PROMPT + _load_learned_vocab()
    segments, info = model.transcribe(
        file_path,
        beam_size=5,
        language="en",
        initial_prompt=prompt,
    )
    text = " ".join(seg.text.strip() for seg in segments)

    # Apply known corrections post-transcription
    for wrong, correct in _load_vocab_replacements():
        text = re.sub(re.escape(wrong), correct, text, flags=re.IGNORECASE)

    return text


# ── Task Extraction via Ollama ──────────────────────────────────────────────
EXTRACT_PROMPT = """You are an AI assistant for a law firm run by attorney Sam Dennis. Below is a transcription of a voice dictation from Sam to his staff. Sam often gives multiple instructions in a single recording.

{staff_list}

Extract each distinct task/instruction as a JSON array. For each task:
- "assignee": who should do it (e.g. "Tara", "Terry", or "unknown" if unclear)
- "action": clear description of what needs to be done
- "client_or_case": the client name (Last, First format preferred) or case name referenced (or null if general)
- "priority": "urgent" if time-sensitive language is used, otherwise "normal"
- "due_hint": any mentioned deadline or timing (or null)

Important:
- Split compound instructions into separate tasks
- For client names, use "Last, First" format when possible (e.g. "Abney, Caleb" not "Caleb Abney")
- Preserve names of people, cases, and legal terminology accurately
- If Sam mentions something personal or a reminder to himself, still capture it but mark assignee as "Sam Dennis"

Respond ONLY with a valid JSON array, no other text.

TRANSCRIPTION:
{transcript}"""


def _load_assignees() -> str:
    """Load assignees from DB for the extraction prompt."""
    db_path = os.path.join(DATA_DIR, "dictations.db")
    fallback = "Terry (paralegal), Tara (remote assistant), Nicole, Jeremy, Blake, Luke Claus, Hala, Aaron Dobby"
    if not os.path.exists(db_path):
        return fallback
    try:
        import sqlite3
        db = sqlite3.connect(db_path, timeout=5)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT name, role FROM assignees WHERE active = 1 ORDER BY name").fetchall()
        db.close()
        if not rows:
            return fallback
        parts = []
        for r in rows:
            if r["role"]:
                parts.append(f"{r['name']} ({r['role']})")
            else:
                parts.append(r["name"])
        return ", ".join(parts)
    except Exception:
        return fallback


def extract_tasks(transcript: str) -> list[dict]:
    import urllib.request
    staff = _load_assignees()
    prompt = EXTRACT_PROMPT.format(transcript=transcript, staff_list="His staff includes: " + staff + ".")
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
def process_new_files(db: sqlite3.Connection, reprocess: bool = False):
    """Scan for new dictation files, transcribe and extract tasks."""
    if not os.path.isdir(DICTATE_DIR):
        print(f"[dictation] Directory not found: {DICTATE_DIR}")
        return 0

    files = sorted(f for f in os.listdir(DICTATE_DIR) if f.lower().endswith('.m4a'))

    if reprocess:
        # Wipe existing data for full reprocess
        db.execute("DELETE FROM dictation_tasks")
        db.execute("DELETE FROM dictations")
        db.commit()
        new_files = files
        print(f"[dictation] REPROCESSING all {len(new_files)} files")
    else:
        existing = set(r[0] for r in db.execute("SELECT file_name FROM dictations").fetchall())
        new_files = [f for f in files if f not in existing]

    if not new_files:
        return 0

    print(f"[dictation] Found {len(new_files)} new dictation(s)")

    # Pre-load case folders for matching
    _load_case_folders()

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
            print(f"done ({t_transcribe:.1f}s, {len(transcript)} chars)")

            db.execute("""INSERT OR REPLACE INTO dictations
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
            print(f"done ({t_extract:.1f}s, {len(tasks)} tasks)")

            db.execute("""UPDATE dictations SET task_json = ?, analyzed_at = ?, status = 'analyzed'
                WHERE id = ?""", (json.dumps(tasks), datetime.utcnow().isoformat(), dictation_id))

            # Step 3: Match case folders
            for i, task in enumerate(tasks):
                case_name = task.get("client_or_case")
                case_folder = match_case_folder(case_name) if case_name else None
                if case_folder:
                    print(f"  -> Matched '{case_name}' to {os.path.basename(case_folder)}")

                db.execute("""INSERT INTO dictation_tasks
                    (dictation_id, task_order, assignee, action, client_or_case,
                     case_folder, priority, due_hint, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (dictation_id, i + 1,
                     task.get("assignee", "unknown"),
                     task.get("action", ""),
                     case_name,
                     case_folder,
                     task.get("priority", "normal"),
                     task.get("due_hint"),
                     datetime.utcnow().isoformat()))
            db.commit()

            processed += 1
            print(f"[dictation] {fname}: {len(tasks)} tasks extracted")

        except Exception as e:
            print(f"[dictation] ERROR {fname}: {e}")
            import traceback
            traceback.print_exc()
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

    if mode == "reprocess":
        n = process_new_files(db, reprocess=True)
        total = db.execute("SELECT COUNT(*) FROM dictation_tasks").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM dictation_tasks WHERE status='pending'").fetchone()[0]
        matched = db.execute("SELECT COUNT(*) FROM dictation_tasks WHERE case_folder IS NOT NULL").fetchone()[0]
        write_status({"stage": "done", "total_tasks": total, "pending_tasks": pending,
                       "matched_cases": matched, "last_batch": n})
        print(f"\n[dictation] Complete: {n} files, {total} tasks ({matched} linked to case folders)")
    elif mode == "watch":
        print(f"[dictation] Watching {DICTATE_DIR} every {POLL_INTERVAL}s")
        write_status({"stage": "watching", "processed": 0, "total_new": 0})
        while True:
            try:
                n = process_new_files(db)
                if n:
                    total = db.execute("SELECT COUNT(*) FROM dictation_tasks").fetchone()[0]
                    pending = db.execute("SELECT COUNT(*) FROM dictation_tasks WHERE status='pending'").fetchone()[0]
                    write_status({"stage": "idle", "total_tasks": total, "pending_tasks": pending, "last_batch": n})
                    print(f"[dictation] Batch done: {n} files, {total} total tasks ({pending} pending)")
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
