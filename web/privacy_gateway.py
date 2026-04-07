"""
Sherlock Privacy Gateway — entity detection, scrubbing, and re-identification.

Ensures no PII/confidential client data leaves the local network when
queries are escalated to cloud LLMs. Operates entirely in memory;
mapping tables are never persisted.
"""

import re
import logging
from typing import Optional

log = logging.getLogger("sherlock.privacy")

# ── Try to load spaCy for NER (falls back to regex if unavailable) ───────────
_nlp = None
try:
    import spacy
    _nlp = spacy.load("en_core_web_sm")
    log.info("privacy_gateway: spaCy NER loaded")
except Exception:
    log.info("privacy_gateway: spaCy not available, using regex-only mode")


# ── Regex patterns for structured PII ────────────────────────────────────────

_PATTERNS = {
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "PHONE": re.compile(r"\b\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "DOB": re.compile(
        r"(?:date\s+of\s+birth|DOB|born)[:\s]*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
    "CASE_NUM": re.compile(
        r"\b(?:"
        r"\d{1,2}:\d{2}-[a-zA-Z]{2,4}-\d{3,6}"   # Federal: 1:23-cv-12345
        r"|[A-Z]{2,4}\d{4,12}"                      # State: SUCV2022050672
        r"|\d{2,4}-?[A-Z]{2,4}-?\d{3,6}"            # Alt: 2020CV130
        r")\b"
    ),
    "ADDRESS": re.compile(
        r"\b\d{1,6}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*"
        r"\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Way|Court|Ct|Circle|Cir)"
        r"\.?\b",
        re.IGNORECASE,
    ),
}

# Name-like pattern for regex fallback when spaCy is unavailable
_NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})?)\b"
)

# Common legal words that look like names but aren't
_FALSE_POSITIVE_NAMES = {
    "Superior Court", "Supreme Court", "District Court", "Circuit Court",
    "County Georgia", "County Florida", "County Alabama", "County Tennessee",
    "State Georgia", "State Florida", "Civil Action", "Medical Center",
    "General Hospital", "Family Medicine", "Insurance Company",
    "Dear Sir", "Very Truly", "First Defense", "Second Defense",
    "Third Defense", "Fourth Defense", "Fifth Defense",
    "Comes Now", "Motion Dismiss", "Request Production",
}

# Judge/court officer prefixes
_JUDGE_PREFIXES = {"judge", "hon.", "hon", "honorable", "justice", "magistrate"}

# ── Sensitivity keywords ─────────────────────────────────────────────────────

_RED_KEYWORDS = [
    "privileged communication", "attorney-client privilege", "work product",
    "privileged and confidential", "attorney work product",
    "do not disclose", "do not share", "confidential settlement",
    "plea agreement", "plea deal", "immunity agreement",
]

_RED_PATH_KEYWORDS = ["privileged", "work product", "attorney-client"]


# ══════════════════════════════════════════════════════════════════════════════
# Entity Map — per-request bidirectional mapping
# ══════════════════════════════════════════════════════════════════════════════

class EntityMap:
    """Bidirectional mapping between real entities and placeholders.

    Created fresh for each cloud request, discarded after re-identification.
    Never persisted to disk or transmitted.
    """

    def __init__(self):
        self._forward: dict[str, str] = {}   # "John Smith" -> "[PERSON_1]"
        self._reverse: dict[str, str] = {}   # "[PERSON_1]" -> "John Smith"
        self._counters: dict[str, int] = {}  # entity_type -> next_index

    def add(self, entity: str, entity_type: str) -> str:
        """Register an entity and return its placeholder."""
        entity = entity.strip()
        if not entity:
            return entity

        # Deduplicate: same entity always gets same placeholder
        if entity in self._forward:
            return self._forward[entity]

        # Also check case-insensitive
        for existing in self._forward:
            if existing.lower() == entity.lower():
                return self._forward[existing]

        idx = self._counters.get(entity_type, 0) + 1
        self._counters[entity_type] = idx
        placeholder = f"[{entity_type}_{idx}]"

        self._forward[entity] = placeholder
        self._reverse[placeholder] = entity
        return placeholder

    def scrub(self, text: str) -> str:
        """Replace all known entities in text with their placeholders.

        Replaces longest matches first to avoid partial replacement issues.
        """
        if not text:
            return text

        # Sort by length descending so "John Michael Smith" is replaced before "John"
        for entity in sorted(self._forward.keys(), key=len, reverse=True):
            placeholder = self._forward[entity]
            # Case-insensitive replacement
            pattern = re.compile(re.escape(entity), re.IGNORECASE)
            text = pattern.sub(placeholder, text)

        return text

    def reidentify(self, text: str) -> str:
        """Restore all placeholders in text with real entities."""
        if not text:
            return text

        for placeholder, entity in self._reverse.items():
            text = text.replace(placeholder, entity)

        return text

    @property
    def entity_count(self) -> int:
        return len(self._forward)

    def summary(self) -> dict:
        """Return scrubbing stats (no actual entities)."""
        return {
            "total_entities": len(self._forward),
            "types": dict(self._counters),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Entity Detection
# ══════════════════════════════════════════════════════════════════════════════

def _detect_entities(text: str, entity_map: EntityMap) -> None:
    """Detect entities in text and register them in the entity map.

    Modifies entity_map in place. Runs regex patterns first (high precision),
    then spaCy NER for names and organizations.
    """
    # ── Phase 1: Regex patterns (structured PII) ─────────────────────────
    for match in _PATTERNS["SSN"].finditer(text):
        entity_map.add(match.group(), "SSN")

    for match in _PATTERNS["PHONE"].finditer(text):
        entity_map.add(match.group(), "PHONE")

    for match in _PATTERNS["EMAIL"].finditer(text):
        entity_map.add(match.group(), "EMAIL")

    for match in _PATTERNS["DOB"].finditer(text):
        entity_map.add(match.group(1) if match.group(1) else match.group(), "DOB")

    for match in _PATTERNS["CASE_NUM"].finditer(text):
        entity_map.add(match.group(), "CASE_NUM")

    for match in _PATTERNS["ADDRESS"].finditer(text):
        entity_map.add(match.group(), "ADDRESS")

    # ── Phase 2: Named Entity Recognition ────────────────────────────────
    if _nlp:
        doc = _nlp(text[:100000])  # Cap at 100K chars for performance
        for ent in doc.ents:
            if ent.label_ == "PERSON":
                # Check if preceded by judge-like prefix
                prefix = text[max(0, ent.start_char - 15):ent.start_char].lower().strip()
                prefix_words = prefix.split()
                if prefix_words and prefix_words[-1] in _JUDGE_PREFIXES:
                    entity_map.add(ent.text, "JUDGE")
                else:
                    entity_map.add(ent.text, "PERSON")
            elif ent.label_ == "ORG":
                entity_map.add(ent.text, "ORG")
            elif ent.label_ in ("GPE", "LOC"):
                # Only add specific location names, not generic ones
                if len(ent.text) > 3 and not ent.text.lower() in ("georgia", "florida", "state"):
                    entity_map.add(ent.text, "LOCATION")
    else:
        # ── Fallback: regex-based name detection ─────────────────────────
        for match in _NAME_PATTERN.finditer(text):
            name = match.group()
            if name not in _FALSE_POSITIVE_NAMES and len(name) > 4:
                entity_map.add(name, "PERSON")


# ══════════════════════════════════════════════════════════════════════════════
# Sensitivity Classifier
# ══════════════════════════════════════════════════════════════════════════════

def classify_sensitivity(
    query: str,
    chunks: list[dict],
) -> str:
    """Classify the sensitivity level of a query + context.

    Returns:
        'GREEN'  — General legal question, no case-specific data
        'YELLOW' — Contains case data that can be de-identified
        'RED'    — Privileged/confidential, never send to cloud
    """
    combined = query.lower()
    for c in chunks:
        combined += " " + (c.get("text", "") or "").lower()
        path = (c.get("path", "") or "").lower()
        # Check file paths for privileged markers
        for kw in _RED_PATH_KEYWORDS:
            if kw in path:
                log.info("sensitivity=RED (path keyword: %s)", kw)
                return "RED"

    # Check for RED keywords
    for kw in _RED_KEYWORDS:
        if kw in combined:
            log.info("sensitivity=RED (keyword: %s)", kw)
            return "RED"

    # Check if query has any case-specific content
    has_names = bool(_NAME_PATTERN.search(query))
    has_case_data = any(
        p.search(query) for p in [_PATTERNS["SSN"], _PATTERNS["CASE_NUM"]]
    )
    has_chunks = len(chunks) > 0

    if not has_names and not has_case_data and not has_chunks:
        return "GREEN"

    return "YELLOW"


# ══════════════════════════════════════════════════════════════════════════════
# Main Scrub Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def scrub_for_cloud(
    query: str,
    chunks: list[dict],
    system_prompt: str = "",
) -> Optional[tuple[str, str, list[dict], "EntityMap"]]:
    """Scrub a query and its context chunks for cloud transmission.

    Returns:
        (scrubbed_query, scrubbed_system, scrubbed_chunks, entity_map)
        or None if sensitivity is RED (refuse to send).
    """
    sensitivity = classify_sensitivity(query, chunks)

    if sensitivity == "RED":
        log.warning("privacy_gateway: RED sensitivity — refusing cloud escalation")
        return None

    entity_map = EntityMap()

    if sensitivity == "GREEN":
        # No scrubbing needed — general legal question
        log.info("privacy_gateway: GREEN — no scrubbing needed")
        return (query, system_prompt, chunks, entity_map)

    # YELLOW: detect entities across all text, then scrub everything
    log.info("privacy_gateway: YELLOW — scrubbing entities")

    # Detect entities in query
    _detect_entities(query, entity_map)

    # Detect entities in all chunk text
    for c in chunks:
        text = c.get("text", "") or ""
        _detect_entities(text, entity_map)
        # Also scan source filenames for names
        source = c.get("source", "") or c.get("path", "") or ""
        _detect_entities(source, entity_map)

    # Detect entities in system prompt (may contain firm name, etc.)
    _detect_entities(system_prompt, entity_map)

    # Now scrub everything
    scrubbed_query = entity_map.scrub(query)
    scrubbed_system = entity_map.scrub(system_prompt)

    scrubbed_chunks = []
    for c in chunks:
        sc = dict(c)
        sc["text"] = entity_map.scrub(sc.get("text", ""))
        sc["source"] = entity_map.scrub(sc.get("source", ""))
        sc["path"] = entity_map.scrub(sc.get("path", ""))
        scrubbed_chunks.append(sc)

    log.info(
        "privacy_gateway: scrubbed %d entities (%s)",
        entity_map.entity_count,
        entity_map.summary()["types"],
    )

    return (scrubbed_query, scrubbed_system, scrubbed_chunks, entity_map)


# ══════════════════════════════════════════════════════════════════════════════
# Streaming Re-identification Buffer
# ══════════════════════════════════════════════════════════════════════════════

class StreamReidentifier:
    """Buffers streaming tokens to handle placeholders split across chunks.

    Usage:
        reid = StreamReidentifier(entity_map)
        for token in cloud_stream:
            clean = reid.feed(token)
            if clean:
                yield clean
        final = reid.flush()
        if final:
            yield final
    """

    def __init__(self, entity_map: EntityMap):
        self._map = entity_map
        self._buffer = ""

    def feed(self, token: str) -> str:
        """Feed a token, return any safely re-identified text."""
        self._buffer += token

        # If buffer contains no bracket starts, it's safe to emit
        if "[" not in self._buffer:
            out = self._map.reidentify(self._buffer)
            self._buffer = ""
            return out

        # Find the last '[' that might be an incomplete placeholder
        last_open = self._buffer.rfind("[")

        # If there's a ']' after the last '[', the placeholder is complete
        if "]" in self._buffer[last_open:]:
            # All placeholders in buffer are complete — emit everything
            out = self._map.reidentify(self._buffer)
            self._buffer = ""
            return out

        # Incomplete placeholder — emit everything before the '[', keep the rest buffered
        safe = self._buffer[:last_open]
        self._buffer = self._buffer[last_open:]

        if safe:
            return self._map.reidentify(safe)
        return ""

    def flush(self) -> str:
        """Flush remaining buffer (call at end of stream)."""
        if self._buffer:
            out = self._map.reidentify(self._buffer)
            self._buffer = ""
            return out
        return ""
