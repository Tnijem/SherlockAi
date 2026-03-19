"""Output file management — save AI responses to the firm-designated outputs dir."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from config import OUTPUTS_DIR
from models import Message, Output, User

log = logging.getLogger("sherlock.outputs")


def _safe_filename(text: str, max_len: int = 60) -> str:
    """Slugify text for use in a filename."""
    text = re.sub(r"[^\w\s\-]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] or "response"


def _format_output(
    user: User,
    matter_name: str,
    query_text: str,
    message: Message,
    now: datetime,
) -> str:
    sources = message.sources_list()
    sources_text = ""
    if sources:
        sources_text = "\n\nSources:\n" + "\n".join(
            f"  [{i+1}] {s['file']} (relevance: {s.get('score', '?')})\n"
            f"      {s.get('excerpt', '')[:120]}..."
            for i, s in enumerate(sources)
        )

    return (
        f"Sherlock Legal Research Output\n"
        f"{'=' * 60}\n"
        f"Date:    {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"User:    {user.display_name or user.username}\n"
        f"Matter:  {matter_name or '(general)'}\n"
        f"{'=' * 60}\n\n"
        f"QUERY\n{'-' * 40}\n{query_text}\n\n"
        f"RESPONSE\n{'-' * 40}\n{message.content}"
        f"{sources_text}\n"
    )


def save_response(
    db: Session,
    user: User,
    message: Message,
    matter_name: str = "",
) -> Output:
    """
    Write an AI response to the primary outputs dir AND mirror to any
    additional output paths configured in sherlock.conf (NAS shares, etc.).
    Returns the Output DB record. The file_path in the record always points
    to the primary copy (for reliable download even if NAS is offline).
    """
    now = datetime.utcnow()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")

    # Resolve user query (previous message in matter)
    query_msg = (
        db.query(Message)
        .filter(
            Message.matter_id == message.matter_id,
            Message.role == "user",
            Message.id < message.id,
        )
        .order_by(Message.id.desc())
        .first()
    )
    query_text = query_msg.content if query_msg else "query"
    query_slug = _safe_filename(query_text)

    matter_slug = _safe_filename(matter_name) if matter_name else "general"
    filename = f"{matter_slug}_{time_str}_{query_slug}.txt"

    # Format content
    content = _format_output(user, matter_name, query_text, message, now)

    # ── Write to primary outputs dir ─────────────────────────────────────
    primary_dir = Path(OUTPUTS_DIR) / user.username / date_str
    primary_dir.mkdir(parents=True, exist_ok=True)
    primary_path = primary_dir / filename
    primary_path.write_text(content, encoding="utf-8")

    # ── Mirror to additional output paths (NAS, shared folders) ──────────
    from config import OUTPUT_MIRROR_PATHS
    for mirror_root in OUTPUT_MIRROR_PATHS:
        try:
            mirror_dir = Path(mirror_root) / user.username / date_str
            mirror_dir.mkdir(parents=True, exist_ok=True)
            mirror_path = mirror_dir / filename
            shutil.copy2(str(primary_path), str(mirror_path))
            log.info("Mirrored output to %s", mirror_path)
        except Exception as e:
            log.warning("Failed to mirror output to %s: %s", mirror_root, e)

    # ── Record in DB ─────────────────────────────────────────────────────
    record = Output(
        user_id=user.id,
        matter_id=message.matter_id,
        message_id=message.id,
        file_path=str(primary_path),
        filename=filename,
        saved_at=now,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return record


def list_outputs(db: Session, user: User, admin_view: bool = False) -> list[Output]:
    """Return saved outputs. Admin can see all users."""
    q = db.query(Output)
    if not admin_view:
        q = q.filter(Output.user_id == user.id)
    return q.order_by(Output.saved_at.desc()).all()
