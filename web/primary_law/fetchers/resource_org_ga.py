"""
primary_law.fetchers.resource_org_ga - Georgia OCGA fetcher.

Source: Public.Resource.Org's OCGA release bundle. They publish the Official
Code of Georgia Annotated as public-domain content under the authority of the
2020 SCOTUS ruling in Georgia v. Public.Resource.Org, Inc. (590 U.S. ___).

Format: a single zip containing per-title ODT + RTF files, e.g.
    gov.ga.ocga.2019.08.21.r73.title.09.odt

We parse the ODT (content.xml) and emit one Document per statutory section,
stripping annotations (case notes, cross references, history) so retrieval
returns only authoritative statutory text. Annotations are secondary and
introduce fabricated-looking citations that hurt grounding.

Caching: the zip and the extracted per-title ODT files live under
    ~/Sherlock/data/primary_law_cache/GA/statutes/
and are reused across runs. Delete that directory to force re-download.
"""
from __future__ import annotations

import datetime
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Iterator

from .base import Document, Fetcher

log = logging.getLogger(__name__)

# Public.Resource.Org S3 mirror - stable since 2019 release.
DEFAULT_BASE_URL = "https://law.resource.org/pub/us/code/ga/"
DEFAULT_RELEASE = "gov.ga.ocga.2019.08.21.release.73.zip"

# Cache directory for the zip + extracted ODT files.
CACHE_ROOT = Path("~/Sherlock/data/primary_law_cache/GA/statutes").expanduser()

# ODT XML namespace.
NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"

# Section header pattern: "9-3-24. Actions on simple written contracts; exceptions."
# Capture group 1 = section code, group 2 = section heading.
SECTION_HEADER_RE = re.compile(r"^(\d+-\d+-\d+(?:\.\d+)?)\.\s+(.+?)(?:\n|$)")

# "Statute text" marker appears as its own paragraph right before the authoritative
# body. Everything after this until the next annotation marker is the statute.
STATUTE_TEXT_MARKER = "Statute text"

# Annotation section markers. These introduce non-authoritative material (case
# notes, history, cross refs, research refs) that we DROP from the ingested chunk.
# First match wins - truncate the statute body there.
ANNOTATION_MARKERS = (
    "Cross references.",
    "CROSS REFERENCES",
    "JUDICIAL DECISIONS",
    "OPINIONS OF THE ATTORNEY GENERAL",
    "RESEARCH REFERENCES",
    "History.",
    "Law reviews.",
    "ALR.",
    "Am. Jur.",
    "C.J.S.",
    "Code Commission notes.",
    "Editor's notes.",
    "Effective date.",
    "The 2",  # catches "The 2019 amendment..." / "The 2020 amendment..." history notes
)


class ResourceOrgGAFetcher(Fetcher):
    """OCGA fetcher from Public.Resource.Org.

    Options:
        base_url (str): override the S3 base url
        release (str): override the release zip filename
        titles (list[int]): OCGA titles to emit (required)
        chapters (dict[int, list[int]]): optional filter - only emit these
            chapters from each title. Used for smoke testing.
        practice_area_reverse_map (dict[str, str]): section-code-prefix -> topic
            for tagging chunks with a practice_area string. Built by ingest.py
            from the jurisdiction config.
    """

    source_type = "statute"

    def __init__(
        self,
        jurisdiction_code: str = "GA",
        base_url: str = DEFAULT_BASE_URL,
        release: str = DEFAULT_RELEASE,
        titles: list[int] | None = None,
        chapters: dict[int, list[int]] | None = None,
        topic_map: dict[int, str] | None = None,
        citation_format: str = "O.C.G.A. § {section}",
    ):
        super().__init__(jurisdiction_code=jurisdiction_code)
        self.base_url = base_url.rstrip("/") + "/"
        self.release = release
        self.titles = titles or []
        self.chapters = chapters or {}
        self.topic_map = topic_map or {}  # title_int -> topic string
        self.citation_format = citation_format

    # ------- public API -------

    def fetch(self) -> Iterator[Document]:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        zip_path = self._ensure_zip()

        for title_num in self.titles:
            odt_path = self._extract_title_odt(zip_path, title_num)
            if odt_path is None:
                log.warning("OCGA title %d not present in release %s", title_num, self.release)
                continue
            log.info("Parsing OCGA title %d from %s", title_num, odt_path.name)
            yield from self._parse_title(odt_path, title_num)

    # ------- download / extract -------

    def _ensure_zip(self) -> Path:
        zip_path = CACHE_ROOT / self.release
        if zip_path.exists() and zip_path.stat().st_size > 1_000_000:
            log.info("OCGA zip cached: %s (%d bytes)", zip_path.name, zip_path.stat().st_size)
            return zip_path
        url = self.base_url + self.release
        log.info("Downloading OCGA release: %s", url)
        tmp = zip_path.with_suffix(".zip.tmp")
        req = urllib.request.Request(url, headers={"User-Agent": "Sherlock-PrimaryLaw/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            out.write(resp.read())
        tmp.rename(zip_path)
        log.info("OCGA release downloaded: %d bytes", zip_path.stat().st_size)
        return zip_path

    def _extract_title_odt(self, zip_path: Path, title_num: int) -> Path | None:
        """Extract (or return cached) ODT file for one title."""
        # Release naming convention: gov.ga.ocga.2019.08.21.r73.title.09.odt
        # We match by suffix since the prefix changes across releases.
        pattern = re.compile(rf"\.title\.0*{title_num}\.odt$")
        out_path = CACHE_ROOT / f"title.{title_num:02d}.odt"
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        with zipfile.ZipFile(zip_path) as z:
            matches = [n for n in z.namelist() if pattern.search(n)]
            if not matches:
                return None
            # Prefer exact title match (avoids "title.09" matching "title.092" etc.)
            name = matches[0]
            with z.open(name) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
        return out_path

    # ------- ODT parsing -------

    def _parse_title(self, odt_path: Path, title_num: int) -> Iterator[Document]:
        paragraphs = self._odt_paragraphs(odt_path)
        current_chapter: int | None = None

        # Walk paragraphs accumulating sections. A section starts at a header
        # matching SECTION_HEADER_RE and ends at the next section header (or
        # at chapter boundary / end of file).
        i = 0
        n = len(paragraphs)
        while i < n:
            para = paragraphs[i]

            # Chapter boundary detection - "CHAPTER 3\nLIMITATIONS OF ACTIONS"
            ch_match = re.match(r"^CHAPTER\s+(\d+)", para)
            if ch_match:
                current_chapter = int(ch_match.group(1))
                i += 1
                continue

            # Section header detection. A section header paragraph starts with
            # e.g. "9-3-24. Actions on simple written contracts; exceptions."
            sec_match = SECTION_HEADER_RE.match(para)
            if sec_match:
                section_code = sec_match.group(1)
                section_title = sec_match.group(2).strip()

                # Validate section belongs to this title
                expected_prefix = f"{title_num}-"
                if not section_code.startswith(expected_prefix):
                    i += 1
                    continue

                # Derive chapter from section code if we missed the CHAPTER header
                # (happens for section headers that cross-reference out of order)
                parts = section_code.split("-")
                if len(parts) >= 2:
                    derived_chapter = int(parts[1])
                    if current_chapter != derived_chapter:
                        current_chapter = derived_chapter

                # Apply chapter filter if set
                allowed = self.chapters.get(title_num)
                if allowed and current_chapter not in allowed:
                    i += 1
                    continue

                # Collect body paragraphs until the next section header
                body_parts: list[str] = []
                in_statute_body = False
                truncated = False
                j = i + 1
                while j < n:
                    p = paragraphs[j]
                    # Next section header ends this section
                    if SECTION_HEADER_RE.match(p):
                        break
                    if re.match(r"^CHAPTER\s+\d+", p):
                        break

                    # Detect statute-body start
                    if not in_statute_body:
                        if p.strip() == STATUTE_TEXT_MARKER:
                            in_statute_body = True
                        j += 1
                        continue

                    # Inside statute body - stop at annotation markers
                    if not truncated:
                        stripped = p.strip()
                        if any(stripped.startswith(m) for m in ANNOTATION_MARKERS):
                            truncated = True
                        else:
                            body_parts.append(p)
                    j += 1

                if body_parts:
                    full_text = (
                        f"{self.citation_format.format(section=section_code)}\n"
                        f"{section_title}\n\n"
                        + "\n\n".join(body_parts).strip()
                    )
                    citation = self.citation_format.format(section=section_code)
                    topic = self.topic_map.get(title_num, "")
                    md = {
                        "jurisdiction": self.jurisdiction_code,
                        "source_type": self.source_type,
                        "citation": citation,
                        "title": str(title_num),
                        "chapter": str(current_chapter) if current_chapter else "",
                        "section": section_code,
                        "topic": topic,
                        "official_url": f"{self.base_url}{self.release}",
                        "retrieved_at": datetime.date.today().isoformat(),
                    }
                    yield Document(text=full_text, metadata=md)

                i = j
                continue

            i += 1

    def _odt_paragraphs(self, odt_path: Path) -> list[str]:
        """Extract plain-text paragraphs from an ODT content.xml.

        Preserves line-breaks inside paragraphs but one list element = one
        ODT paragraph element.
        """
        with zipfile.ZipFile(odt_path) as z:
            with z.open("content.xml") as f:
                xml_bytes = f.read()
        # Use iterparse for memory efficiency on 14+ MB files
        root = ET.fromstring(xml_bytes)
        body = root.find(f"{{{NS_OFFICE}}}body/{{{NS_OFFICE}}}text")
        if body is None:
            return []
        out: list[str] = []
        for child in body.iter():
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag in ("p", "h"):
                text = self._element_text(child)
                if text:
                    out.append(text)
        return out

    def _element_text(self, elem) -> str:
        """Recursively extract text from an ODT paragraph element, handling
        line-break / space / tab markers."""
        parts: list[str] = []
        if elem.text:
            parts.append(elem.text)
        for child in elem:
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag == "line-break":
                parts.append("\n")
            elif tag == "s":
                n_attr = child.get(f"{{{NS_TEXT}}}c", "1")
                try:
                    parts.append(" " * int(n_attr))
                except ValueError:
                    parts.append(" ")
            elif tag == "tab":
                parts.append("\t")
            elif tag == "p" or tag == "h":
                # Nested paragraph - add its text then newline (rare in OCGA)
                parts.append(self._element_text(child))
                parts.append("\n")
            else:
                # span, a, etc - recurse for text
                parts.append(self._element_text(child))
            if child.tail:
                parts.append(child.tail)
        return "".join(parts).strip()
