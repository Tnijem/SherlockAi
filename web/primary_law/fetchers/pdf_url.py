"""
primary_law.fetchers.pdf_url - generic fetcher for PDFs at a stable URL.

Used for:
  - state court rules (Florida Rules of Civil Procedure)
  - watched legislation (FL HB 837, etc.)
  - any other "one URL = one PDF document" source type

Output: one Document per PDF containing the full extracted text. Downstream
the chunker will split long PDFs into overlapping chunks, all sharing the same
citation metadata.

PDF extraction: uses pdfplumber if available, falls back to pypdf. Both are
already pulled in by Sherlock's requirements (indexer.py uses them).
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import re
import urllib.request
from pathlib import Path
from typing import Iterator

from .base import Document, Fetcher

log = logging.getLogger(__name__)

CACHE_ROOT = Path("~/Sherlock/data/primary_law_cache").expanduser()
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 "
    "Sherlock-PrimaryLaw/1.0 (+legal research)"
)


def _extract_pdf_text(pdf_path: Path) -> str:
    """Try pdfplumber, fall back to pypdf."""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(pdf_path) as pdf:
            parts = []
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t:
                    parts.append(t)
        text = "\n\n".join(parts)
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception as e:
        log.warning("pdfplumber failed for %s: %s", pdf_path.name, e)

    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n\n".join(p for p in parts if p)
    except Exception as e:
        log.error("pypdf failed for %s: %s", pdf_path.name, e)
        return ""


class PdfUrlFetcher(Fetcher):
    """Fetch one or more PDFs at stable URLs and yield them as Documents.

    Options:
        items: list of dicts, each with keys:
            - url (str): direct URL to the PDF
            - citation (str): how this doc should be cited, e.g.
                "Fla. R. Civ. P. 1.510" or "Fla. HB 837 (2023)"
            - source_type (str): "rule" | "legislation" | "case"
            - topic (str, optional)
            - year (int, optional)
            - effective_date (str ISO, optional)
        jurisdiction_code (str): jurisdiction metadata
        cache_subdir (str): where to cache the PDFs under primary_law_cache/
    """

    def __init__(
        self,
        jurisdiction_code: str,
        items: list[dict],
        cache_subdir: str = "misc",
    ):
        super().__init__(jurisdiction_code=jurisdiction_code)
        self.items = items
        self.cache_dir = CACHE_ROOT / jurisdiction_code / cache_subdir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def source_type(self) -> str:  # type: ignore[override]
        # Dynamic - each item carries its own source_type
        return "mixed"

    def fetch(self) -> Iterator[Document]:
        for item in self.items:
            url = item["url"]
            citation = item["citation"]
            src_type = item.get("source_type", "rule")
            topic = item.get("topic", "")

            # Deterministic cache filename from URL
            h = hashlib.sha1(url.encode()).hexdigest()[:16]
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", citation)[:60]
            pdf_path = self.cache_dir / f"{safe_name}__{h}.pdf"

            if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
                log.info("Downloading PDF: %s", url)
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        pdf_path.write_bytes(resp.read())
                    log.info("  wrote %d bytes to %s", pdf_path.stat().st_size, pdf_path.name)
                except Exception as e:
                    log.error("PDF download failed (%s): %s", url, e)
                    continue
            else:
                log.info("PDF cached: %s (%d bytes)", pdf_path.name, pdf_path.stat().st_size)

            text = _extract_pdf_text(pdf_path)
            if not text or len(text) < 200:
                log.warning("PDF text extraction empty/short for %s", citation)
                continue

            # Normalize whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]+", " ", text)

            md = {
                "jurisdiction": self.jurisdiction_code,
                "source_type": src_type,
                "citation": citation,
                "title": "",
                "chapter": "",
                "section": item.get("section", ""),
                "topic": topic,
                "official_url": url,
                "retrieved_at": datetime.date.today().isoformat(),
            }
            if "year" in item:
                md["year"] = str(item["year"])
            if item.get("effective_date"):
                md["effective_date"] = item["effective_date"]

            yield Document(text=text, metadata=md)
