"""
Audio transcription via faster-whisper (fully local, no internet).

Supports: MP3, WAV, M4A, OGG, FLAC, AAC
Model: configurable (default: medium) — stored in WHISPER_MODEL_DIR
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

from config import WHISPER_MODEL, WHISPER_MODEL_DIR, WHISPER_LANGUAGE

log = logging.getLogger("sherlock.audio")

# ── Job tracking (shared with indexer pattern) ────────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_model_cache: dict[str, object] = {}
_model_lock = threading.Lock()


def _new_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "done": False, "transcript": "", "error": ""}
    return job_id


def get_job_status(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ── Model loading (cached — expensive first load) ─────────────────────────────

def _get_model():
    with _model_lock:
        if WHISPER_MODEL not in _model_cache:
            from faster_whisper import WhisperModel
            log.info("Loading Whisper model '%s' from %s ...", WHISPER_MODEL, WHISPER_MODEL_DIR)
            _model_cache[WHISPER_MODEL] = WhisperModel(
                WHISPER_MODEL,
                device="cpu",          # Mac Mini — no CUDA; Apple Silicon via cpu is fine
                compute_type="int8",   # int8 reduces memory, fast on M-series
                download_root=WHISPER_MODEL_DIR,
            )
            log.info("Whisper model loaded.")
    return _model_cache[WHISPER_MODEL]


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe_sync(audio_path: Path) -> str:
    """Blocking transcription. Returns full transcript text."""
    model = _get_model()
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        language=WHISPER_LANGUAGE or None,  # None = auto-detect
        vad_filter=True,        # Skip silence
        vad_parameters={"min_silence_duration_ms": 500},
    )
    log.info(
        "Transcribing %s — detected language: %s (%.0f%%)",
        audio_path.name,
        info.language,
        info.language_probability * 100,
    )
    transcript = " ".join(seg.text.strip() for seg in segments)
    return transcript.strip()


def start_transcription(audio_path: Path) -> str:
    """
    Start async transcription in background thread.
    Returns job_id for status polling.
    """
    job_id = _new_job()

    def _run():
        _update_job(job_id, status="running")
        try:
            transcript = transcribe_sync(audio_path)
            _update_job(job_id, status="done", done=True, transcript=transcript)
            log.info("Transcription complete for %s (%d chars)", audio_path.name, len(transcript))
        except Exception as e:
            log.error("Transcription failed for %s: %s", audio_path, e)
            _update_job(job_id, status="error", done=True, error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return job_id


SUPPORTED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}


def is_audio_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_AUDIO_EXTS
