"""SQLAlchemy models for Sherlock — SQLite backend."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from config import DB_PATH

# ── Engine ───────────────────────────────────────────────────────────────────
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)

# Enable WAL mode for better concurrent read performance on large DBs
@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")  # wait up to 5s on lock contention
    cur.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Base ─────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Users ────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(64), unique=True, nullable=False, index=True)
    display_name  = Column(String(128))
    password_hash = Column(String(256), nullable=False)
    role          = Column(String(16), default="user")   # 'admin' | 'user'
    active        = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_login    = Column(DateTime)

    matters   = relationship("Matter", back_populates="user", cascade="all, delete-orphan")
    uploads   = relationship("Upload", back_populates="user", cascade="all, delete-orphan")
    outputs   = relationship("Output", back_populates="user", cascade="all, delete-orphan")


# ── Cases ─────────────────────────────────────────────────────────────────────
class Case(Base):
    """
    A law firm case/matter record. Each case points to a folder on the NAS
    that holds its documents. The indexer monitors that path independently.
    """
    __tablename__ = "cases"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    created_by      = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Core identifiers
    case_number     = Column(String(128), unique=True, index=True)   # firm's internal ref
    case_name       = Column(String(512), nullable=False)            # e.g. "Smith v. Jones"
    case_type       = Column(String(64))   # Criminal | Civil | Family | Corporate | PI | Other

    # NAS path — where this case's documents live (read-only)
    nas_path        = Column(String(2048))

    # Optional demographics / descriptive fields
    client_name     = Column(String(256))
    opposing_party  = Column(String(256))
    jurisdiction    = Column(String(128))
    assigned_to     = Column(String(256))   # attorney name (free text)
    date_opened     = Column(String(32))    # YYYY-MM-DD string for simplicity
    description     = Column(Text)         # free-form notes

    # Status
    status          = Column(String(16), default="active")  # active | closed | archived

    # Index state
    last_indexed    = Column(DateTime)       # when NAS path was last scanned
    indexed_count   = Column(Integer, default=0)   # files indexed

    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator  = relationship("User", foreign_keys=[created_by])
    matters  = relationship("Matter", back_populates="case", cascade="all, delete-orphan")


def case_collection(case_id: int) -> str:
    """ChromaDB collection name for a specific case's NAS documents."""
    return f"case_{case_id}_docs"


# ── Matters (named conversation threads) ─────────────────────────────────────
class Matter(Base):
    __tablename__ = "matters"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    case_id       = Column(Integer, ForeignKey("cases.id"), nullable=True)   # optional link to a case
    name          = Column(String(256), nullable=False)
    billable_time = Column(Float, default=0.0)   # hours (e.g. 1.5 = 1h30m)
    created_at    = Column(DateTime, default=datetime.utcnow)
    archived      = Column(Boolean, default=False)

    user     = relationship("User", back_populates="matters")
    case     = relationship("Case", back_populates="matters")
    messages = relationship("Message", back_populates="matter", cascade="all, delete-orphan",
                            order_by="Message.created_at")


# ── Messages ─────────────────────────────────────────────────────────────────
class Message(Base):
    __tablename__ = "messages"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    matter_id  = Column(Integer, ForeignKey("matters.id"), nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    role       = Column(String(16), nullable=False)   # 'user' | 'assistant'
    content    = Column(Text, nullable=False)
    sources    = Column(Text)                          # JSON: [{file, excerpt, score}]
    created_at = Column(DateTime, default=datetime.utcnow)

    matter = relationship("Matter", back_populates="messages")

    def sources_list(self) -> list:
        if self.sources:
            try:
                return json.loads(self.sources)
            except Exception:
                pass
        return []


# ── Uploads ───────────────────────────────────────────────────────────────────
class Upload(Base):
    __tablename__ = "uploads"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename    = Column(String(512), nullable=False)
    stored_path = Column(String(1024), nullable=False)
    file_type   = Column(String(32))
    size_bytes  = Column(Integer)
    status      = Column(String(16), default="pending")  # pending|indexing|ready|error
    error_msg   = Column(Text)
    chroma_ids  = Column(Text)   # JSON array of ChromaDB doc IDs
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="uploads")


# ── NAS index state (tracks what's been indexed from NAS mounts) ──────────────
class IndexedFile(Base):
    __tablename__ = "indexed_files"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    file_path   = Column(String(2048), unique=True, nullable=False, index=True)
    file_hash   = Column(String(64), nullable=False)   # sha256 of content
    size_bytes  = Column(Integer)
    mtime       = Column(String(32))
    chunk_count = Column(Integer, default=0)
    indexed_at  = Column(DateTime, default=datetime.utcnow)
    collection  = Column(String(128), default="sherlock_cases")
    case_id     = Column(Integer, ForeignKey("cases.id"), nullable=True, index=True)


# ── Deadlines ─────────────────────────────────────────────────────────────────
class Deadline(Base):
    __tablename__ = "deadlines"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    matter_id    = Column(Integer, ForeignKey("matters.id"), nullable=True, index=True)
    case_id      = Column(Integer, ForeignKey("cases.id"), nullable=True, index=True)
    date_str     = Column(String(32))          # ISO date or descriptive "30 days from filing"
    description  = Column(Text, nullable=False)
    dl_type      = Column(String(64))          # "statute_of_limitations"|"filing"|"notice"|"contract"|"hearing"|"other"
    source_file  = Column(String(512))
    urgency      = Column(String(16), default="normal")  # "critical"|"high"|"normal"
    extracted_at = Column(DateTime, default=datetime.utcnow)


# ── Matter Briefs ─────────────────────────────────────────────────────────────
class MatterBrief(Base):
    __tablename__ = "matter_briefs"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    matter_id      = Column(Integer, ForeignKey("matters.id"), unique=True, nullable=False, index=True)
    brief_md       = Column(Text)      # Summary section
    risks_md       = Column(Text)      # Risk section
    generated_at   = Column(DateTime, default=datetime.utcnow)
    msg_count      = Column(Integer, default=0)  # message count at generation time — stale if different


# ── Query Logs (usage dashboard) ──────────────────────────────────────────────
class QueryLog(Base):
    __tablename__ = "query_logs"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    matter_id     = Column(Integer, ForeignKey("matters.id"), nullable=True, index=True)
    query_type    = Column(String(32), default="auto")
    verbosity     = Column(String(32), default="attorney")
    research_mode = Column(Boolean, default=False)
    latency_ms    = Column(Integer)       # total response time
    chunks_used   = Column(Integer)
    rate_limited  = Column(Boolean, default=False)
    prompt_tokens     = Column(Integer)    # tokens in the prompt sent to LLM
    completion_tokens = Column(Integer)    # tokens generated by LLM
    total_tokens      = Column(Integer)    # prompt + completion
    tokens_per_sec    = Column(Float)      # generation speed (tok/s)
    source            = Column(String(32), default="user")  # user | system:brief | system:deadline | system:embed | system:sync
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)


# ── Outputs ───────────────────────────────────────────────────────────────────
class Output(Base):
    __tablename__ = "outputs"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    matter_id  = Column(Integer, ForeignKey("matters.id"), nullable=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    file_path  = Column(String(2048), nullable=False)
    filename   = Column(String(512), nullable=False)
    saved_at   = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="outputs")


# ── Query Templates (saved prompts) ──────────────────────────────────────────
class QueryTemplate(Base):
    __tablename__ = "query_templates"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)  # NULL = system-wide
    name        = Column(String(256), nullable=False)
    template    = Column(Text, nullable=False)
    category    = Column(String(64), default="general")  # general|discovery|drafting|review|deposition
    query_type  = Column(String(32), default="auto")
    created_at  = Column(DateTime, default=datetime.utcnow)


# System user ID constant — used to attribute background/automated token usage
# Uses 999999 to avoid autoincrement conflicts (SQLite starts at 1)
SYSTEM_USER_ID = 999999


# ── Create all tables ─────────────────────────────────────────────────────────
def init_db():
    Base.metadata.create_all(bind=engine)
    # Migrate: add token columns to query_logs if missing
    import sqlite3
    db_path = str(DB_PATH)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(query_logs)")
    cols = {row[1] for row in cur.fetchall()}
    for col, typ in [
        ("prompt_tokens", "INTEGER"),
        ("completion_tokens", "INTEGER"),
        ("total_tokens", "INTEGER"),
        ("tokens_per_sec", "REAL"),
        ("source", "TEXT DEFAULT 'user'"),
    ]:
        if col not in cols:
            cur.execute(f"ALTER TABLE query_logs ADD COLUMN {col} {typ}")
    conn.commit()

    # Migrate: add missing columns to matters table
    cur.execute("PRAGMA table_info(matters)")
    matter_cols = {row[1] for row in cur.fetchall()}
    if "case_id" not in matter_cols:
        cur.execute("ALTER TABLE matters ADD COLUMN case_id INTEGER REFERENCES cases(id)")
    if "billable_time" not in matter_cols:
        cur.execute("ALTER TABLE matters ADD COLUMN billable_time REAL DEFAULT 0.0")
    conn.commit()

    # Migrate: add case_id column to indexed_files if missing
    cur.execute("PRAGMA table_info(indexed_files)")
    if_cols = {row[1] for row in cur.fetchall()}
    if "case_id" not in if_cols:
        cur.execute("ALTER TABLE indexed_files ADD COLUMN case_id INTEGER REFERENCES cases(id)")
        conn.commit()

    # Create FTS5 virtual table for hybrid BM25 keyword search
    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            chunk_id,
            collection,
            source,
            content,
            tokenize='porter unicode61'
        )
    """)
    conn.commit()
    conn.close()

    # Ensure system user exists (id=SYSTEM_USER_ID) for background task attribution
    db = SessionLocal()
    try:
        sys_user = db.query(User).filter(User.id == SYSTEM_USER_ID).first()
        if not sys_user:
            from auth import hash_password
            sys_user = User(
                id=SYSTEM_USER_ID,
                username="_sherlock_system",
                display_name="Sherlock (System)",
                password_hash=hash_password("__system_nologin__"),
                role="system",
                active=False,  # can't log in
            )
            db.add(sys_user)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # Seed default query templates
    db2 = SessionLocal()
    try:
        if db2.query(QueryTemplate).filter(QueryTemplate.user_id == None).count() == 0:
            defaults = [
                ("Summarize All Documents", "Provide a comprehensive summary of all documents in this case. Identify key parties, core facts, and important dates.", "review", "summary"),
                ("Extract All Deadlines", "Identify every deadline, statute of limitations, filing window, notice requirement, and time-sensitive obligation in these documents.", "review", "risk"),
                ("Timeline of Events", "Build a chronological timeline of all events mentioned in the documents. Flag any gaps or ambiguous sequences.", "review", "timeline"),
                ("Contract Risk Review", "Review this contract for: unusual terms, missing standard protections, ambiguous language, one-sided provisions, and potential liability exposure.", "review", "risk"),
                ("Deposition Prep", "Based on these documents, generate a list of deposition questions organized by topic. Focus on gaps, inconsistencies, and areas requiring clarification.", "deposition", "auto"),
                ("Draft Motion to Compel", "Based on the discovery requests and responses in these documents, draft a motion to compel. Identify specific deficiencies in the responses.", "drafting", "drafting"),
                ("Privilege Review", "Review these documents and identify any that may be subject to attorney-client privilege, work product protection, or other applicable privileges.", "discovery", "auto"),
                ("Compare Documents", "Compare the key documents in this matter. Identify differences, conflicts, and areas of agreement between them.", "review", "compare"),
            ]
            for name, template, category, qt in defaults:
                db2.add(QueryTemplate(name=name, template=template, category=category, query_type=qt))
            db2.commit()
    except Exception:
        db2.rollback()
    finally:
        db2.close()


def log_system_tokens(
    source: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    tokens_per_sec: float = 0.0,
    latency_ms: int | None = None,
    user_id: int | None = None,
    matter_id: int | None = None,
) -> None:
    """Log token usage from background/system operations (briefs, deadlines, embedding, etc.)."""
    db = SessionLocal()
    try:
        ql = QueryLog(
            user_id=user_id or SYSTEM_USER_ID,
            matter_id=matter_id,
            query_type=source.split(":")[-1] if ":" in source else source,
            source=source,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            tokens_per_sec=tokens_per_sec if tokens_per_sec > 0 else None,
            latency_ms=latency_ms,
        )
        db.add(ql)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
