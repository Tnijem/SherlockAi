"""
Sherlock file indexer — GB-scale, multi-format, hash-deduped, chunked.

Handles:
  PDF, DOCX, DOC (via LibreOffice), TXT, MD, RTF, HTML, XLSX, XLS, CSV,
  PPTX, images (OCR), audio (Whisper transcription), EML, MSG

Design:
  - Files are split into overlapping ~512-token chunks for better retrieval.
  - Each file's sha256 hash is stored in SQLite; unchanged files are skipped.
  - NAS paths are read-only — this module never writes to source dirs.
  - Runs as a background thread; callers get a job_id to poll status.
  - Gracefully handles NAS disconnects (logs error, continues with other files).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Generator, Optional

import requests
from sqlalchemy.orm import Session

from config import EMBED_MODEL, GLOBAL_COLLECTION, OLLAMA_URL, UPLOADS_DIR, user_collection
from logging_config import get_logger
from models import IndexedFile, SessionLocal, Upload

log = get_logger("sherlock.indexer")

# ── Supported extensions ──────────────────────────────────────────────────────

TEXT_EXTS   = {".txt", ".md", ".rst", ".log", ".csv", ".tsv"}
PDF_EXTS    = {".pdf"}
WORD_EXTS   = {".docx", ".doc"}
EXCEL_EXTS  = {".xlsx", ".xls"}
PPT_EXTS    = {".pptx", ".ppt"}
HTML_EXTS   = {".html", ".htm"}
RTF_EXTS    = {".rtf"}
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif"}
AUDIO_EXTS  = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}
EMAIL_EXTS  = {".eml"}

ALL_SUPPORTED = (
    TEXT_EXTS | PDF_EXTS | WORD_EXTS | EXCEL_EXTS | PPT_EXTS |
    HTML_EXTS | RTF_EXTS | IMAGE_EXTS | AUDIO_EXTS | EMAIL_EXTS
)

# ── Chunking ──────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 1200   # chars (~300 tokens)
CHUNK_OVERLAP = 200    # chars of overlap between chunks


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if c.strip()]


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text(filepath: Path) -> str:
    """Extract plain text from a file. Returns empty string on failure."""
    ext = filepath.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            return _extract_text_file(filepath)
        elif ext in PDF_EXTS:
            return _extract_pdf(filepath)
        elif ext == ".docx":
            return _extract_docx(filepath)
        elif ext == ".doc":
            return _extract_doc_via_libreoffice(filepath)
        elif ext == ".xlsx":
            return _extract_xlsx(filepath)
        elif ext == ".xls":
            return _extract_xls_via_libreoffice(filepath)
        elif ext in PPT_EXTS:
            return _extract_pptx(filepath) if ext == ".pptx" else _extract_via_libreoffice(filepath)
        elif ext in HTML_EXTS:
            return _extract_html(filepath)
        elif ext in RTF_EXTS:
            return _extract_rtf(filepath)
        elif ext in IMAGE_EXTS:
            return _extract_image_ocr(filepath)
        elif ext in AUDIO_EXTS:
            return _extract_audio_whisper(filepath)
        elif ext in EMAIL_EXTS:
            return _extract_eml(filepath)
    except Exception as e:
        log.warning("extract_text failed for %s: %s", filepath, e)
    return ""


def _extract_text_file(fp: Path) -> str:
    # Handle CSV/TSV specially — flatten to readable text
    if fp.suffix.lower() in {".csv", ".tsv"}:
        delim = "\t" if fp.suffix.lower() == ".tsv" else ","
        rows = []
        with fp.open(encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f, delimiter=delim)
            for row in reader:
                rows.append(" | ".join(row))
        return "\n".join(rows)
    return fp.read_text(encoding="utf-8", errors="ignore")


def _extract_pdf(fp: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(fp))
    parts = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if not text.strip():
            # Scanned page — fall back to OCR
            try:
                from PIL import Image
                import pytesseract
                import io as _io
                for img_obj in page.images:
                    img = Image.open(_io.BytesIO(img_obj.data))
                    text += pytesseract.image_to_string(img) + "\n"
            except Exception:
                pass
        parts.append(text)
    return "\n".join(parts)


def _extract_docx(fp: Path) -> str:
    from docx import Document
    doc = Document(str(fp))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    # Also extract table text
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _extract_doc_via_libreoffice(fp: Path) -> str:
    return _extract_via_libreoffice(fp)


def _extract_via_libreoffice(fp: Path) -> str:
    """Convert to txt using LibreOffice (must be installed)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "txt:Text", "--outdir", tmpdir, str(fp)],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice failed: {result.stderr.decode()}")
        out_file = Path(tmpdir) / (fp.stem + ".txt")
        if out_file.exists():
            return out_file.read_text(encoding="utf-8", errors="ignore")
    return ""


def _extract_xlsx(fp: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(fp), read_only=True, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            row_text = " | ".join(str(v) for v in row if v is not None)
            if row_text.strip():
                parts.append(row_text)
    return "\n".join(parts)


def _extract_xls_via_libreoffice(fp: Path) -> str:
    return _extract_via_libreoffice(fp)


def _extract_pptx(fp: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(fp))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        parts.append(f"[Slide {i}]")
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                parts.append(shape.text)
    return "\n".join(parts)


def _extract_html(fp: Path) -> str:
    text = fp.read_text(encoding="utf-8", errors="ignore")
    # Simple tag stripping — avoid bs4 dependency
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_rtf(fp: Path) -> str:
    # Strip RTF control words
    import re
    text = fp.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"\\[a-z]+\-?\d*\s?", " ", text)
    text = re.sub(r"[{}\\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_image_ocr(fp: Path) -> str:
    from PIL import Image
    import pytesseract
    img = Image.open(str(fp))
    return pytesseract.image_to_string(img)


def _extract_audio_whisper(fp: Path) -> str:
    """Transcribe audio file using faster-whisper."""
    from faster_whisper import WhisperModel
    from config import WHISPER_MODEL, WHISPER_MODEL_DIR
    model = WhisperModel(WHISPER_MODEL, device="cpu", download_root=WHISPER_MODEL_DIR)
    segments, _ = model.transcribe(str(fp), beam_size=5)
    return " ".join(seg.text for seg in segments)


def _extract_eml(fp: Path) -> str:
    import email
    msg = email.message_from_bytes(fp.read_bytes())
    parts = []
    if msg["subject"]:
        parts.append(f"Subject: {msg['subject']}")
    if msg["from"]:
        parts.append(f"From: {msg['from']}")
    if msg["to"]:
        parts.append(f"To: {msg['to']}")
    if msg["date"]:
        parts.append(f"Date: {msg['date']}")
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode("utf-8", errors="ignore"))
    return "\n".join(parts)


# ── Embedding ─────────────────────────────────────────────────────────────────

_idx_embed_buf = {"count": 0, "tokens": 0}
_idx_embed_lock = __import__("threading").Lock()


def _flush_idx_embed_tokens() -> None:
    tok = _idx_embed_buf["tokens"]
    if tok <= 0:
        return
    _idx_embed_buf["count"] = 0
    _idx_embed_buf["tokens"] = 0
    try:
        from models import log_system_tokens
        log_system_tokens(source="system:embed", prompt_tokens=tok, completion_tokens=0)
    except Exception:
        pass


def embed_text(text: str) -> list[float]:
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text[:8192]},
        timeout=60,
    )
    resp.raise_for_status()
    rj = resp.json()
    p_tok = rj.get("prompt_eval_count", 0)
    if p_tok:
        with _idx_embed_lock:
            _idx_embed_buf["count"] += 1
            _idx_embed_buf["tokens"] += p_tok
            if _idx_embed_buf["count"] >= 50:
                _flush_idx_embed_tokens()
    return rj["embedding"]


# ── Hash ──────────────────────────────────────────────────────────────────────

def file_hash(fp: Path, chunk_size: int = 1 << 20) -> str:
    """sha256 of file contents, reading in 1 MB chunks (safe for large files)."""
    h = hashlib.sha256()
    with fp.open("rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ── Job tracking ──────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}   # job_id → {status, progress, total, errors, done}
_jobs_lock = threading.Lock()


def _new_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "indexed": 0, "skipped": 0,
                         "errors": 0, "total": 0, "done": False, "messages": []}
    return job_id


def get_job_status(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ── Core indexing logic ───────────────────────────────────────────────────────

def _index_file(
    fp: Path,
    collection_name: str,
    db: Session,
    job_id: str,
    source_label: Optional[str] = None,
    case_id: Optional[int] = None,
) -> tuple[int, bool]:
    """
    Index a single file into ChromaDB.
    Returns (chunks_added, was_skipped).

    Optimization order:
      1. stat() for mtime — cheap syscall, skips most files on large NAS
      2. Hash only if mtime changed — avoids reading file contents unnecessarily
      3. Extract + embed only if hash changed
    """
    ext = fp.suffix.lower()
    if ext not in ALL_SUPPORTED:
        return 0, True

    # Step 1: stat — cheap, avoids even opening the file
    try:
        stat = fp.stat()
        fsize = stat.st_size
        fmtime = str(stat.st_mtime)
    except OSError as e:
        log.warning("Cannot stat %s (NAS disconnect?): %s", fp, e)
        return 0, True

    existing = db.query(IndexedFile).filter(IndexedFile.file_path == str(fp)).first()

    # Step 2: if mtime unchanged, skip entirely (no hash needed)
    if existing and existing.mtime == fmtime:
        return 0, True

    # Step 3: mtime changed (or new file) — now hash to confirm content change
    try:
        fhash = file_hash(fp)
    except OSError as e:
        log.warning("Cannot hash %s: %s", fp, e)
        return 0, True

    if existing and existing.file_hash == fhash:
        # Content identical despite mtime change (e.g. NAS touch) — update mtime only
        existing.mtime = fmtime
        db.commit()
        return 0, True

    # Extract text
    text = extract_text(fp)
    if not text.strip():
        log.debug("No text extracted from %s", fp)
        return 0, False

    # Chunk
    chunks = chunk_text(text)
    if not chunks:
        return 0, False

    # Get or create ChromaDB collection
    from rag import get_or_create_collection
    coll = get_or_create_collection(collection_name)

    # Remove old chunks for this file if re-indexing
    if existing:
        try:
            old_ids = [f"{fp}__chunk_{i}" for i in range(existing.chunk_count)]
            if old_ids:
                coll.delete(ids=old_ids)
                # Also clean FTS5
                try:
                    import sqlite3
                    from config import DB_PATH
                    fts_conn = sqlite3.connect(str(DB_PATH))
                    fts_cur = fts_conn.cursor()
                    for old_id in old_ids:
                        fts_cur.execute("DELETE FROM chunk_fts WHERE chunk_id = ?", (old_id,))
                    fts_conn.commit()
                    fts_conn.close()
                except Exception:
                    pass
        except Exception:
            pass

    # Embed and upsert chunks
    label = source_label or fp.name
    doc_ids, embeddings, docs, metas = [], [], [], []
    for i, chunk in enumerate(chunks):
        try:
            emb = embed_text(chunk)
        except Exception as e:
            log.warning("Embed failed chunk %d of %s: %s", i, fp, e)
            continue
        doc_ids.append(f"{fp}__chunk_{i}")
        embeddings.append(emb)
        docs.append(chunk)
        metas.append({
            "source": label,
            "path": str(fp),
            "chunk": i,
            "total_chunks": len(chunks),
            "ext": ext,
        })

    if doc_ids:
        coll.upsert(ids=doc_ids, embeddings=embeddings, documents=docs, metadatas=metas)

    # Insert into FTS5 for hybrid search
    try:
        import sqlite3
        from config import DB_PATH
        fts_conn = sqlite3.connect(str(DB_PATH))
        fts_cur = fts_conn.cursor()
        for doc_id, chunk_doc in zip(doc_ids, docs):
            fts_cur.execute(
                "INSERT OR REPLACE INTO chunk_fts(chunk_id, collection, source, content) VALUES (?, ?, ?, ?)",
                (doc_id, collection_name, label, chunk_doc)
            )
        fts_conn.commit()
        fts_conn.close()
    except Exception as e:
        log.warning("FTS5 index failed: %s", e)

    # Update SQLite state
    from datetime import datetime as _dt
    if existing:
        existing.file_hash = fhash
        existing.size_bytes = fsize
        existing.mtime = fmtime
        existing.chunk_count = len(doc_ids)
        existing.collection = collection_name
        existing.indexed_at = _dt.utcnow()
        if case_id is not None:
            existing.case_id = case_id
    else:
        db.add(IndexedFile(
            file_path=str(fp),
            file_hash=fhash,
            size_bytes=fsize,
            mtime=fmtime,
            chunk_count=len(doc_ids),
            collection=collection_name,
            case_id=case_id,
        ))
    db.commit()

    return len(doc_ids), False


# ── Case NAS index (background job) ──────────────────────────────────────────

def start_case_index(case_id: int, nas_path: str) -> str:
    """
    Index a single case's NAS path into its own ChromaDB collection.
    Only processes files new or changed since last run (mtime-first).
    Returns job_id.
    """
    from models import Case, case_collection
    job_id = _new_job()

    def _run():
        db = SessionLocal()
        try:
            _update_job(job_id, status="running")
            indexed = skipped = errors = 0

            root = Path(nas_path)
            if not root.exists():
                _update_job(job_id, status="error", done=True,
                            messages=[f"NAS path not accessible: {nas_path}"])
                return

            coll_name = case_collection(case_id)
            all_files = [
                fp for fp in root.rglob("*")
                if fp.is_file() and fp.suffix.lower() in ALL_SUPPORTED
            ]
            _update_job(job_id, total=len(all_files))
            log.info("Case %d: scanning %d files in %s", case_id, len(all_files), nas_path)

            for fp in all_files:
                try:
                    added, was_skipped = _index_file(
                        fp, coll_name, db, job_id,
                        source_label=fp.name,
                        case_id=case_id,
                    )
                    if was_skipped:
                        skipped += 1
                    else:
                        indexed += 1
                except Exception as e:
                    errors += 1
                    log.error("Error indexing %s (case %d): %s", fp, case_id, e)
                _update_job(job_id, indexed=indexed, skipped=skipped, errors=errors)

            # Update case record with index timestamp and count
            from datetime import datetime as _dt
            case = db.query(Case).filter(Case.id == case_id).first()
            if case:
                case.last_indexed = _dt.utcnow()
                case.indexed_count = (
                    db.query(IndexedFile)
                    .filter(IndexedFile.case_id == case_id)
                    .count()
                )
                db.commit()

            log.info(
                "case_index_done",
                extra={"job_id": job_id, "case_id": case_id,
                       "indexed": indexed, "skipped": skipped, "errors": errors},
            )
            _update_job(job_id, status="done", done=True)
        except Exception as e:
            log.error("case_index_error", extra={"job_id": job_id, "case_id": case_id, "detail": str(e)})
            _update_job(job_id, status="error", done=True, messages=[str(e)])
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True).start()
    return job_id


# ── Legacy NAS index (global, used by admin re-index button) ──────────────────

def start_nas_index(nas_paths: list[str]) -> str:
    """Index a flat list of NAS paths into the global collection. Returns job_id."""
    job_id = _new_job()

    def _run():
        db = SessionLocal()
        try:
            _update_job(job_id, status="running")
            indexed = skipped = errors = 0

            for nas_path in nas_paths:
                root = Path(nas_path)
                if not root.exists():
                    _update_job(job_id, messages=[f"⚠ NAS path not accessible: {nas_path}"])
                    continue

                all_files = [
                    fp for fp in root.rglob("*")
                    if fp.is_file() and fp.suffix.lower() in ALL_SUPPORTED
                ]
                _update_job(job_id, total=len(all_files))

                for fp in all_files:
                    try:
                        added, was_skipped = _index_file(fp, GLOBAL_COLLECTION, db, job_id)
                        if was_skipped:
                            skipped += 1
                        else:
                            indexed += 1
                    except Exception as e:
                        errors += 1
                        log.error("Error indexing %s: %s", fp, e)
                    _update_job(job_id, indexed=indexed, skipped=skipped, errors=errors)

            _update_job(job_id, status="done", done=True)
        except Exception as e:
            _update_job(job_id, status="error", done=True, messages=[str(e)])
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True).start()
    return job_id


# ── User upload indexing (background job) ─────────────────────────────────────

def start_upload_index(upload_id: int, user_id: int, filepath: Path) -> str:
    """Index a user-uploaded file into their private collection. Returns job_id."""
    job_id = _new_job()

    def _run():
        db = SessionLocal()
        try:
            upload = db.query(Upload).filter(Upload.id == upload_id).first()
            if not upload:
                _update_job(job_id, status="error", done=True)
                return

            upload.status = "indexing"
            db.commit()

            _update_job(job_id, status="running", total=1)
            added, _ = _index_file(
                filepath,
                user_collection(user_id),
                db,
                job_id,
                source_label=filepath.name,
            )

            # Store ChromaDB IDs on upload record
            chunk_ids = [f"{filepath}__chunk_{i}" for i in range(added)]
            upload.chroma_ids = json.dumps(chunk_ids)
            upload.status = "ready"
            db.commit()

            _update_job(job_id, status="done", done=True, indexed=added)
        except Exception as e:
            log.error("Upload index error: %s", e)
            if upload := db.query(Upload).filter(Upload.id == upload_id).first():
                upload.status = "error"
                upload.error_msg = str(e)
                db.commit()
            _update_job(job_id, status="error", done=True, messages=[str(e)])
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True).start()
    return job_id
