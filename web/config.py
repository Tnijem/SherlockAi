"""Sherlock configuration — reads sherlock.conf from project root."""

import os
import configparser
from pathlib import Path

_ROOT = Path(__file__).parent.parent  # ~/Sherlock/
_CONF_PATH = _ROOT / "sherlock.conf"


def _load() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if _CONF_PATH.exists():
        # configparser requires a section header; wrap bare key=value with [sherlock]
        text = "[sherlock]\n" + _CONF_PATH.read_text()
        cfg.read_string(text)
    return cfg


_cfg = _load()


def _get(key: str, default: str = "") -> str:
    """Read key from [sherlock] section, falling back to environment variable, then default."""
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    try:
        return _cfg.get("sherlock", key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def _expand(path: str) -> str:
    return str(Path(path).expanduser().resolve()) if path else path


# ── Core service URLs ────────────────────────────────────────────────────────
OLLAMA_URL: str = _get("OLLAMA_URL", "http://localhost:11434")
CHROMA_URL: str = _get("CHROMA_URL", "http://localhost:8000")

# ── Paths ────────────────────────────────────────────────────────────────────
OUTPUTS_DIR: str = _expand(_get("OUTPUTS_DIR", str(_ROOT / "outputs")))
UPLOADS_DIR: str = _expand(_get("UPLOADS_DIR", str(_ROOT / "uploads")))
DB_PATH: str = _expand(_get("DB_PATH", str(_ROOT / "data" / "sherlock.db")))
WHISPER_MODEL_DIR: str = _expand(_get("WHISPER_MODEL_DIR", str(_ROOT / "models" / "whisper")))

# ── Auth ─────────────────────────────────────────────────────────────────────
JWT_SECRET: str = _get("JWT_SECRET", "CHANGE-THIS-SECRET-IN-PRODUCTION")
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRY_HOURS: int = int(_get("JWT_EXPIRY_HOURS", "8"))

# ── Models ───────────────────────────────────────────────────────────────────
EMBED_MODEL: str = _get("EMBED_MODEL", "mxbai-embed-large")
LLM_MODEL: str = _get("LLM_MODEL", "sherlock-rag")
WHISPER_MODEL: str = _get("WHISPER_MODEL", "medium")
WHISPER_LANGUAGE: str = _get("WHISPER_LANGUAGE", "")  # empty = auto-detect

# ── Limits ───────────────────────────────────────────────────────────────────
MAX_UPLOAD_MB: int = int(_get("MAX_UPLOAD_MB", "500"))
RAG_TOP_N: int = int(_get("RAG_TOP_N", "5"))


# ── Cloud LLM (privacy-gated) ────────────────────────────────────────────────
CLOUD_ENABLED: bool = _get("CLOUD_ENABLED", "false").lower() in ("true", "1", "yes")
CLOUD_PROVIDER: str = _get("CLOUD_PROVIDER", "anthropic")     # "anthropic" | "openai"
CLOUD_MODEL: str = _get("CLOUD_MODEL", "claude-sonnet-4-20250514")
CLOUD_API_KEY: str = _get("CLOUD_API_KEY", "")
CLOUD_MODE: str = _get("CLOUD_MODE", "fallback")              # "fallback" | "always" | "manual"
SENSITIVITY_THRESHOLD: str = _get("SENSITIVITY_THRESHOLD", "YELLOW").upper()

# ── Cloud LLM (privacy-gated) ────────────────────────────────────────────────
CLOUD_ENABLED: bool = _get("CLOUD_ENABLED", "false").lower() in ("true", "1", "yes")
CLOUD_PROVIDER: str = _get("CLOUD_PROVIDER", "anthropic")     # "anthropic" | "openai"
CLOUD_MODEL: str = _get("CLOUD_MODEL", "claude-sonnet-4-20250514")
CLOUD_API_KEY: str = _get("CLOUD_API_KEY", "")
CLOUD_MODE: str = _get("CLOUD_MODE", "fallback")              # "fallback" | "always" | "manual"
SENSITIVITY_THRESHOLD: str = _get("SENSITIVITY_THRESHOLD", "YELLOW").upper()
# ── NAS paths (multiple comma-separated source directories) ─────────────────
_nas_raw: str = _get("NAS_PATHS", "")
NAS_PATHS: list[str] = [p.strip() for p in _nas_raw.split(",") if p.strip()]

# ── Output mirror paths (additional copies of saved outputs — NAS shares) ───
_mirror_raw: str = _get("OUTPUT_MIRROR_PATHS", "")
OUTPUT_MIRROR_PATHS: list[str] = [p.strip() for p in _mirror_raw.split(",") if p.strip()]

# ── Branding ─────────────────────────────────────────────────────────────────
SYSTEM_NAME: str = _get("SYSTEM_NAME", "Sherlock")

# ── ChromaDB collection names ────────────────────────────────────────────────
GLOBAL_COLLECTION: str = "sherlock_global"


def user_collection(user_id: int) -> str:
    return f"user_{user_id}_docs"


# ── Rate limiting ────────────────────────────────────────────────────────────
RATE_LIMIT_RPM: int = int(_get("RATE_LIMIT_RPM", "30"))    # requests per minute per user
RATE_LIMIT_ADMIN_RPM: int = int(_get("RATE_LIMIT_ADMIN_RPM", "120"))

# ── Ensure directories exist ─────────────────────────────────────────────────
for _dir in [OUTPUTS_DIR, UPLOADS_DIR, Path(DB_PATH).parent, WHISPER_MODEL_DIR]:
    Path(_dir).mkdir(parents=True, exist_ok=True)
