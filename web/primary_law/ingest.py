"""
primary_law.ingest - orchestrator.

Reads firm + jurisdiction configs, runs the appropriate fetchers, chunks
output, embeds via Ollama, and upserts into the `primary_law` Chroma
collection with deterministic IDs so re-runs are idempotent.

Run from CLI via scripts/ingest_primary_law.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

# Allow this module to be imported both as `primary_law.ingest` and executed
# as a script. When executed from scripts/ingest_primary_law.py we've already
# inserted ~/Sherlock/web into sys.path.

from .registry import FirmConfig, Jurisdiction, load_all
from .chunker import Chunk, chunk_document
from .fetchers.base import Document, Fetcher
from .fetchers.resource_org_ga import ResourceOrgGAFetcher
from .fetchers.flsenate import FLSenateFetcher
from .fetchers.pdf_url import PdfUrlFetcher
from .fetchers.courtlistener_cases import CourtListenerFetcher

from . import PRIMARY_LAW_COLLECTION

log = logging.getLogger(__name__)


# ---------- configuration ----------

@dataclass
class IngestOptions:
    jurisdictions: list[str] | None = None  # None = use firm.yaml
    source_types: list[str] = field(
        default_factory=lambda: ["statute", "rule", "legislation", "case"]
    )
    dry_run: bool = False
    # Smoke-test controls
    title_filter: list[int] | None = None
    chapter_filter: dict[int, list[int]] | None = None
    # Case-law controls (overrides firm.yaml defaults when set)
    case_lookback_years: int | None = None
    case_max_per_court: int | None = None
    case_query: str = ""


@dataclass
class IngestStats:
    fetchers_run: int = 0
    documents_fetched: int = 0
    chunks_created: int = 0
    chunks_embedded: int = 0
    chunks_upserted: int = 0
    errors: int = 0
    started_at: float = 0.0
    elapsed_s: float = 0.0


# ---------- fetcher factory ----------

def build_statute_fetcher(jur: Jurisdiction, firm: FirmConfig, opts: IngestOptions) -> Fetcher | None:
    """Pick the right statute fetcher for a jurisdiction based on source type."""
    st = jur.statutes
    src_type = st.source_type

    # Union of titles for this firm's practice areas
    titles = (
        opts.title_filter
        if opts.title_filter is not None
        else jur.titles_for_practice_areas(firm.practice_areas)
    )
    if not titles:
        log.info("[%s] no statute titles for practice areas %s", jur.code, firm.practice_areas)
        return None

    # Build reverse map: title_int -> first matching practice area (for topic tagging).
    # If a title appears under multiple areas, first one wins - good enough for MVP.
    topic_map: dict[int, str] = {}
    for area, title_list in st.practice_area_map.items():
        for t in title_list:
            topic_map.setdefault(int(t), area)

    chapters = opts.chapter_filter or {}

    if src_type == "resource_org_ga":
        # Look for release override in YAML; fetcher defaults to latest known.
        # (registry.py doesn't currently surface 'release' — we read it from env as fallback.)
        return ResourceOrgGAFetcher(
            jurisdiction_code=jur.code,
            base_url=st.source_base_url,
            titles=titles,
            chapters=chapters,
            topic_map=topic_map,
            citation_format=st.citation_format,
        )

    if src_type == "flsenate":
        # For FL, titles in the practice_area_map are actually CHAPTER numbers
        # (Florida statute numbering is chapter.section, no title layer).
        # We reuse the `titles` variable from above but it represents chapters here.
        chapter_topic_map: dict[int, str] = {}
        for area, ch_list in st.practice_area_map.items():
            for c in ch_list:
                chapter_topic_map.setdefault(int(c), area)
        return FLSenateFetcher(
            jurisdiction_code=jur.code,
            base_url=st.source_base_url,
            chapters=titles,
            topic_map=chapter_topic_map,
            citation_format=st.citation_format,
        )

    # Future: nyleg, njleg, txleg, etc.
    log.warning("[%s] unsupported statute source type: %s", jur.code, src_type)
    return None


def build_rule_fetchers(jur: Jurisdiction) -> list[Fetcher]:
    """Build fetchers for court rules. Today: pdf_url and alias types.
    Returns a list (possibly empty). `alias` rules are no-ops because the
    referenced content is already covered by statutes."""
    out: list[Fetcher] = []

    pdf_items: list[dict] = []
    for rule in jur.court_rules:
        if rule.type == "pdf_url" and rule.url:
            pdf_items.append({
                "url": rule.url,
                "citation": rule.name,
                "source_type": "rule",
                "topic": "civil_procedure",
            })
        elif rule.type == "alias":
            log.info("[%s] court rule '%s' is alias → %s (skipped, covered by statutes)",
                     jur.code, rule.name, rule.see)
        elif rule.type == "html":
            log.info("[%s] html court rule '%s' skipped (html_url fetcher TODO)",
                     jur.code, rule.name)
        else:
            log.warning("[%s] unsupported court_rule type: %s", jur.code, rule.type)

    if pdf_items:
        out.append(PdfUrlFetcher(
            jurisdiction_code=jur.code,
            items=pdf_items,
            cache_subdir="rules",
        ))
    return out


def build_case_fetchers(jur: Jurisdiction, firm: FirmConfig, opts: IngestOptions) -> list[Fetcher]:
    """CourtListener case-law fetcher per jurisdiction.

    Reads court slugs from `jurisdiction.courtlistener_courts` and
    caps/lookback from the firm config unless overridden in IngestOptions.
    Returns an empty list if the jurisdiction has no courts configured.
    """
    if not jur.courtlistener_courts:
        return []
    lookback = opts.case_lookback_years or firm.case_law_lookback_years
    max_per = opts.case_max_per_court or firm.case_law_max_per_court
    return [CourtListenerFetcher(
        jurisdiction_code=jur.code,
        courts=list(jur.courtlistener_courts),
        lookback_years=int(lookback),
        max_per_court=int(max_per),
        query=opts.case_query,
    )]


def build_legislation_fetchers(jur: Jurisdiction) -> list[Fetcher]:
    """Watched legislation (e.g. FL HB 837). Uses pdf_url + metadata."""
    if not jur.legislation:
        return []
    pdf_items: list[dict] = []
    for leg in jur.legislation:
        if leg.type != "pdf_url":
            log.warning("[%s] legislation %s: unsupported type %s", jur.code, leg.name, leg.type)
            continue
        pdf_items.append({
            "url": leg.url,
            "citation": f"{jur.code} {leg.name} ({leg.year})" if leg.year else f"{jur.code} {leg.name}",
            "source_type": "legislation",
            "topic": "legislation",
            "year": leg.year,
            "effective_date": leg.effective_date,
        })
    if pdf_items:
        return [PdfUrlFetcher(
            jurisdiction_code=jur.code,
            items=pdf_items,
            cache_subdir="legislation",
        )]
    return []


# ---------- embedding (matches embed_worker.py pattern) ----------

_CTRL_RE = __import__("re").compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_for_embed(text: str) -> str:
    """Strip control chars, normalize whitespace, coerce to UTF-8 clean.
    Ollama's embeddings endpoint returns 500 on certain PDF artifacts
    (null bytes, lone surrogates, very long runs of punctuation)."""
    t = _CTRL_RE.sub(" ", text)
    # Drop lone surrogates / invalid UTF-8 by round-tripping
    t = t.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    # Collapse absurd whitespace runs
    t = __import__("re").sub(r"\s{3,}", "\n\n", t)
    return t.strip()


def _embed_text(text: str, ollama_url: str, model: str, max_retries: int = 3) -> list[float]:
    """L2-normalized embedding vector from Ollama.

    Fallback chain if Ollama returns 500 on the raw input:
      1. Raw text truncated to 2048 chars
      2. Sanitized text truncated to 2048 chars (strips control chars, etc.)
      3. Sanitized text truncated to 1200 chars (covers token-limit overflows
         for dense legal text - mxbai-embed-large's 512-token limit can be
         hit by table-heavy PDF chunks even under 2048 chars)
      4. Sanitized text truncated to 600 chars (last resort)
    """
    last_err: Exception | None = None
    sanitized = _sanitize_for_embed(text)
    payloads = [
        text[:2048],
        sanitized[:2048] if sanitized != text[:2048] else "",
        sanitized[:1200],
        sanitized[:600],
    ]
    seen: set[str] = set()
    for payload in payloads:
        if not payload or payload in seen:
            continue
        seen.add(payload)
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(
                    f"{ollama_url}/api/embeddings",
                    data=json.dumps({"model": model, "prompt": payload}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                emb = data["embedding"]
                norm = math.sqrt(sum(x * x for x in emb)) or 1.0
                return [x / norm for x in emb]
            except Exception as e:
                last_err = e
                # Don't retry a 500 at the same length — step to next payload.
                if "500" in str(e):
                    break
                time.sleep(1 + attempt)
    raise RuntimeError(f"embed_failed (all fallbacks exhausted): {last_err}")


# ---------- Chroma upsert ----------

def _deterministic_id(jurisdiction: str, citation: str, chunk_index: int) -> str:
    """Stable ID so re-running ingest is idempotent (overwrites, not duplicates)."""
    key = f"{jurisdiction}|{citation}|{chunk_index}"
    return "plaw_" + hashlib.sha1(key.encode()).hexdigest()[:20]


def _get_or_create_collection(chroma_url: str):
    import chromadb
    host, _, port_s = chroma_url.replace("http://", "").replace("https://", "").partition(":")
    port = int(port_s or "8000")
    client = chromadb.HttpClient(host=host, port=port)
    coll = client.get_or_create_collection(
        name=PRIMARY_LAW_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    return coll


# ---------- orchestrator ----------

def run_ingest(opts: IngestOptions) -> IngestStats:
    stats = IngestStats(started_at=time.time())
    firm, jurisdictions = load_all()
    log.info(
        "Ingest starting: firm=%s jurisdictions=%s practice_areas=%s dry_run=%s",
        firm.name, firm.jurisdictions, firm.practice_areas, opts.dry_run,
    )

    # Filter to requested jurisdictions (default: all from firm.yaml)
    jur_codes = opts.jurisdictions or firm.jurisdictions

    # Resolve config for embedding + chroma the same way embed_worker does.
    # We import config.py from the web package (it's in sys.path because we're
    # a subpackage of web/).
    import config
    ollama_url = getattr(config, "OLLAMA_URL", "http://localhost:11434")
    embed_model = getattr(config, "EMBED_MODEL", "mxbai-embed-large")
    chroma_url = getattr(config, "CHROMA_URL", "http://localhost:8000")

    coll = None
    if not opts.dry_run:
        coll = _get_or_create_collection(chroma_url)
        log.info("Chroma collection ready: %s @ %s", PRIMARY_LAW_COLLECTION, chroma_url)

    # Upsert buffer (flush every 50 chunks to keep HTTP payloads small)
    buf_ids: list[str] = []
    buf_texts: list[str] = []
    buf_embs: list[list[float]] = []
    buf_meta: list[dict] = []

    def _flush():
        nonlocal buf_ids, buf_texts, buf_embs, buf_meta
        if not buf_ids:
            return
        if coll is not None:
            coll.upsert(
                ids=buf_ids,
                documents=buf_texts,
                embeddings=buf_embs,
                metadatas=buf_meta,
            )
        stats.chunks_upserted += len(buf_ids)
        buf_ids, buf_texts, buf_embs, buf_meta = [], [], [], []

    def _process_fetcher(fetcher: Fetcher, code: str):
        """Fetch → chunk → embed → buffer → upsert. Shared by all source types."""
        for doc in fetcher.fetch():
            stats.documents_fetched += 1
            chunks = chunk_document(doc)
            stats.chunks_created += len(chunks)

            for ch in chunks:
                try:
                    if opts.dry_run:
                        stats.chunks_embedded += 1
                        continue
                    emb = _embed_text(ch.text, ollama_url, embed_model)
                    stats.chunks_embedded += 1
                    time.sleep(0.02)  # modest throttle
                except Exception as e:
                    log.warning("embed failed for %s chunk %s: %s",
                                ch.metadata.get("citation"),
                                ch.metadata.get("chunk_index"), e)
                    stats.errors += 1
                    continue

                doc_id = _deterministic_id(
                    ch.metadata["jurisdiction"],
                    ch.metadata["citation"],
                    ch.metadata["chunk_index"],
                )
                buf_ids.append(doc_id)
                buf_texts.append(ch.text)
                buf_embs.append(emb)
                buf_meta.append(ch.metadata)

                if len(buf_ids) >= 50:
                    _flush()

            if stats.documents_fetched % 50 == 0:
                log.info(
                    "[%s] progress: docs=%d chunks=%d embedded=%d upserted=%d",
                    code, stats.documents_fetched, stats.chunks_created,
                    stats.chunks_embedded, stats.chunks_upserted,
                )

    for code in jur_codes:
        jur = jurisdictions.get(code)
        if jur is None:
            log.warning("jurisdiction %s not found in registry, skipping", code)
            continue

        # Statute fetcher (one per jurisdiction)
        if "statute" in opts.source_types:
            fetcher = build_statute_fetcher(jur, firm, opts)
            if fetcher is not None:
                stats.fetchers_run += 1
                log.info("[%s] running statute fetcher: %r", code, fetcher)
                _process_fetcher(fetcher, code)

        # Court-rule fetchers (PDFs, possibly multiple)
        if "rule" in opts.source_types:
            for fetcher in build_rule_fetchers(jur):
                stats.fetchers_run += 1
                log.info("[%s] running rule fetcher: %r", code, fetcher)
                _process_fetcher(fetcher, code)

        # Watched-legislation fetchers (PDFs)
        if "legislation" in opts.source_types:
            for fetcher in build_legislation_fetchers(jur):
                stats.fetchers_run += 1
                log.info("[%s] running legislation fetcher: %r", code, fetcher)
                _process_fetcher(fetcher, code)

        # Case-law fetchers (CourtListener opinions)
        if "case" in opts.source_types:
            for fetcher in build_case_fetchers(jur, firm, opts):
                stats.fetchers_run += 1
                log.info("[%s] running case fetcher: %r", code, fetcher)
                _process_fetcher(fetcher, code)

    _flush()
    stats.elapsed_s = time.time() - stats.started_at
    log.info(
        "Ingest complete: fetchers=%d docs=%d chunks=%d embedded=%d upserted=%d errors=%d elapsed=%.1fs",
        stats.fetchers_run, stats.documents_fetched, stats.chunks_created,
        stats.chunks_embedded, stats.chunks_upserted, stats.errors, stats.elapsed_s,
    )
    return stats
