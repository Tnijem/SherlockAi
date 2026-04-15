"""
primary_law.fetchers.flsenate - Florida Statutes fetcher from flsenate.gov.

Source: https://www.flsenate.gov/Laws/Statutes/<year>/ChapterNN/All

The "/All" endpoint returns every section of a chapter on one HTML page in a
stable semantic structure:

  <span class="Section">
    <span class="SectionNumber">95.11&#x2003;</span>
    <span class="Catchline"><span class="CatchlineText">Limitations other than...</span>...</span>
    <span class="SectionBody">
      <span class="Text Intro Justify">Actions other than for recovery...</span>
      <div class="Subsection">
        <span class="Number">(1)&#x2003;</span>
        <span class="Text Intro Justify">WITHIN TWENTY YEARS.&#x2014;...</span>
        <div class="Paragraph">...</div>
      </div>
      ...
    </span>
  </span>

We parse with ElementTree after wrapping the fragment and emit one Document per
SectionNumber. The FL section-numbering convention is "chapter.section" (e.g.
95.11, 768.81) rather than the title/chapter/section triple used by OCGA, so
the `chapter` metadata field holds the integer chapter (e.g. "95") and the
`title` field is empty.
"""
from __future__ import annotations

import datetime
import html
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Iterator

from .base import Document, Fetcher

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://www.flsenate.gov/Laws/Statutes/2024/"
CACHE_ROOT = Path("~/Sherlock/data/primary_law_cache/FL/statutes").expanduser()
USER_AGENT = "Sherlock-PrimaryLaw/1.0 (+legal research)"
FETCH_DELAY_SEC = 0.5  # be polite to gov server


class FLSenateFetcher(Fetcher):
    """Florida Statutes fetcher.

    Options:
        base_url: https://www.flsenate.gov/Laws/Statutes/<year>/
        chapters (list[int]): chapter numbers to pull (from practice_area_map)
        topic_map (dict[int, str]): chapter -> practice_area string
        citation_format (str): defaults to "Fla. Stat. § {section}"
    """

    source_type = "statute"

    def __init__(
        self,
        jurisdiction_code: str = "FL",
        base_url: str = DEFAULT_BASE_URL,
        chapters: list[int] | None = None,
        topic_map: dict[int, str] | None = None,
        citation_format: str = "Fla. Stat. § {section}",
    ):
        super().__init__(jurisdiction_code=jurisdiction_code)
        self.base_url = base_url.rstrip("/") + "/"
        self.chapters = chapters or []
        self.topic_map = topic_map or {}
        self.citation_format = citation_format

    def fetch(self) -> Iterator[Document]:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        for ch in self.chapters:
            html_text = self._fetch_chapter_all(ch)
            if not html_text:
                log.warning("FL chapter %d returned empty/missing", ch)
                continue
            log.info("Parsing FL chapter %d (%d bytes)", ch, len(html_text))
            yield from self._parse_chapter(html_text, ch)
            time.sleep(FETCH_DELAY_SEC)

    # ------- HTTP + cache -------

    def _fetch_chapter_all(self, chapter: int) -> str | None:
        cache_file = CACHE_ROOT / f"chapter_{chapter:04d}.html"
        if cache_file.exists() and cache_file.stat().st_size > 1000:
            log.info("FL chapter %d cached", chapter)
            return cache_file.read_text(encoding="utf-8", errors="replace")

        url = f"{self.base_url}Chapter{chapter}/All"
        log.info("Fetching FL chapter: %s", url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            log.error("FL fetch failed for chapter %d: %s", chapter, e)
            return None
        cache_file.write_text(body, encoding="utf-8")
        return body

    # ------- HTML parsing -------

    # We avoid a full HTML parser dep. The relevant nodes have predictable
    # class names and we don't need to handle arbitrary HTML5. Regex is fine.

    # The chapter page contains:
    #   <div class="IndexItem">  ... table-of-contents entries we want to SKIP
    #   <div class="Section">    ... actual authoritative section bodies
    # Both contain a <span class="SectionNumber">. We only split on the Section
    # container so the index entries are ignored.
    SECTION_SPLIT_RE = re.compile(r'<div\s+class="Section"\s*>', re.IGNORECASE)
    SECTION_NUMBER_RE = re.compile(
        r'<span\s+class="SectionNumber"\s*>\s*([^<]+?)\s*</span>', re.IGNORECASE,
    )
    CATCHLINE_RE = re.compile(
        r'<span\s+xml:space="preserve"\s+class="CatchlineText"\s*>([\s\S]*?)</span>',
        re.IGNORECASE,
    )
    # SectionBody is a <span> inside the Section <div>. Stop at the next Section
    # container, footer, or end of chapter.
    SECTION_BODY_RE = re.compile(
        r'<span\s+class="SectionBody"\s*>([\s\S]*?)(?=<div\s+class="Section"\s*>|</article|</main|</body)',
        re.IGNORECASE,
    )

    def _parse_chapter(self, html_text: str, chapter: int) -> Iterator[Document]:
        segments = self.SECTION_SPLIT_RE.split(html_text)
        # First segment is everything before the first section - skip
        for seg in segments[1:]:
            m_num = self.SECTION_NUMBER_RE.search(seg)
            if not m_num:
                continue
            raw_num = html.unescape(m_num.group(1)).strip()
            section_num = re.sub(r"\s+", "", raw_num)
            # Remove any trailing em-space / ideographic-space
            section_num = section_num.rstrip("\u2003\u00a0\u2009 ")
            if not re.match(r"^\d+\.\d+$", section_num):
                continue

            catch_m = self.CATCHLINE_RE.search(seg)
            catchline = self._strip_tags(catch_m.group(1)).strip() if catch_m else ""

            body_m = self.SECTION_BODY_RE.search(seg)
            body_raw = body_m.group(1) if body_m else seg

            body_text = self._extract_body_text(body_raw)
            if not body_text or len(body_text) < 50:
                continue

            citation = self.citation_format.format(section=section_num)
            full_text = f"{citation}\n{catchline}\n\n{body_text}".strip()

            md = {
                "jurisdiction": self.jurisdiction_code,
                "source_type": self.source_type,
                "citation": citation,
                "title": "",
                "chapter": str(chapter),
                "section": section_num,
                "topic": self.topic_map.get(chapter, ""),
                "official_url": f"{self.base_url}{section_num}",
                "retrieved_at": datetime.date.today().isoformat(),
            }
            yield Document(text=full_text, metadata=md)

    # ------- HTML -> text helpers -------

    _TAG_RE = re.compile(r"<[^>]+>")
    _WS_RE = re.compile(r"[ \t\u00a0\u2003\u2009]+")

    def _strip_tags(self, s: str) -> str:
        return self._TAG_RE.sub("", s)

    def _extract_body_text(self, s: str) -> str:
        """Convert the SectionBody HTML into clean paragraph-separated text.

        - Subsection / Paragraph / Sub-paragraph divs become paragraph breaks
        - <br> becomes paragraph break
        - Everything else: strip tags, normalize whitespace
        - Unescape HTML entities
        """
        # Convert block boundaries to newlines
        s = re.sub(r'<div\s+class="Subsection"[^>]*>', "\n\n", s, flags=re.IGNORECASE)
        s = re.sub(r'<div\s+class="Paragraph"[^>]*>', "\n", s, flags=re.IGNORECASE)
        s = re.sub(r'<div\s+class="Sub[Pp]aragraph"[^>]*>', "\n", s, flags=re.IGNORECASE)
        s = re.sub(r'<div\s+class="SubSub[Pp]aragraph"[^>]*>', "\n", s, flags=re.IGNORECASE)
        s = re.sub(r"</div>", "\n", s, flags=re.IGNORECASE)
        s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
        # Remove everything else
        s = self._strip_tags(s)
        # Unescape
        s = html.unescape(s)
        # Normalize whitespace within each line
        lines = [self._WS_RE.sub(" ", ln).strip() for ln in s.split("\n")]
        lines = [ln for ln in lines if ln]
        # Collapse runs of short numbered-marker-only lines into the next line
        return "\n\n".join(lines)
