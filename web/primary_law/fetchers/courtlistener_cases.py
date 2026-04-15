"""
primary_law.fetchers.courtlistener_cases - case-law fetcher.

Pulls recent opinions from CourtListener for a specified list of court slugs
and yields them as `Document` objects with rich citation metadata so they can
be indexed into the `primary_law` Chroma collection alongside statutes and
court rules.

Why a new fetcher (vs. reusing web/courtlistener.py):
  - The existing adapter writes .txt files into SampleData/ and then re-indexes
    via the generic NAS indexer. That gives case text, but no structured
    citation metadata (case_name, court, decision_date, docket) — which is
    exactly what we need for primary-law retrieval boosting and citation
    verification.
  - This fetcher talks to the same CourtListener v4 search API but yields
    Documents directly into the primary_law ingest pipeline.

API docs: https://www.courtlistener.com/help/api/rest/
Auth: optional. Set COURTLISTENER_TOKEN env var for the 5,000 req/day tier.
      Unauthenticated access is ~60 req/hr.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

from .base import Document, Fetcher
from .pdf_url import _extract_pdf_text

log = logging.getLogger(__name__)

API_BASE = "https://www.courtlistener.com/api/rest/v4"
CACHE_ROOT = Path("~/Sherlock/data/primary_law_cache").expanduser()
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 "
    "Sherlock-PrimaryLaw/1.0 (+legal research)"
)
# Anonymous limit is ~60/hr → 1 req/min worst case, but bursts of short
# requests are tolerated. Be polite.
REQUEST_DELAY_SEC = 1.2
MAX_TEXT_CHARS = 80_000  # cap per opinion


_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _clean_html(s: str) -> str:
    s = _HTML_TAG.sub(" ", s)
    s = _WS.sub("\n\n", s)
    return s.strip()


def _url_quote_path(url: str) -> str:
    """Percent-encode just the path portion of a URL, leaving scheme/host/query
    alone. Safe for URLs that already have some encoding — we only touch raw
    unsafe characters. Needed for court-hosted PDFs whose filenames contain
    spaces or ampersands."""
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url
    # Keep common path characters unquoted; spaces and `&` etc. get encoded.
    safe = "/%-._~!$'()*+,;=:@"
    new_path = urllib.parse.quote(parts.path, safe=safe)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, new_path, parts.query, parts.fragment,
    ))


def _http_get_json(url: str, params: dict | None = None, token: str | None = None) -> dict:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


class CourtListenerFetcher(Fetcher):
    """Fetch recent opinions from CourtListener for a set of court slugs.

    Options:
        jurisdiction_code: e.g. "GA" / "FL" - stored on every yielded Document
        courts (list[str]): CourtListener court slugs (e.g. ["ga","gactapp"])
        lookback_years (int): pull opinions filed within the last N years
        max_per_court (int): cap total opinions per court
        query (str): optional free-text search filter (empty = all)
        token (str|None): CourtListener API token, else $COURTLISTENER_TOKEN
        cache_subdir (str): where to cache raw JSON, under primary_law_cache/
    """

    source_type = "case"

    def __init__(
        self,
        jurisdiction_code: str,
        courts: list[str],
        lookback_years: int = 10,
        max_per_court: int = 2000,
        query: str = "",
        token: str | None = None,
        cache_subdir: str = "cases",
    ):
        super().__init__(jurisdiction_code=jurisdiction_code)
        self.courts = courts
        self.lookback_years = lookback_years
        self.max_per_court = max_per_court
        self.query = query
        self.token = token or os.environ.get("COURTLISTENER_TOKEN", "") or None
        self.cache_dir = CACHE_ROOT / jurisdiction_code / cache_subdir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------- main loop -------

    def fetch(self) -> Iterator[Document]:
        after_date = (
            datetime.date.today() - datetime.timedelta(days=365 * self.lookback_years)
        ).isoformat()
        for court in self.courts:
            log.info("[%s] CourtListener: court=%s after=%s max=%d",
                     self.jurisdiction_code, court, after_date, self.max_per_court)
            yield from self._fetch_court(court, after_date)

    def _fetch_court(self, court: str, after_date: str) -> Iterator[Document]:
        collected = 0
        attempted = 0
        page = 1
        # Hard cap on attempts so a court with pathological data can't burn
        # hours of wall time when every hit is unusable (e.g. PDFs all 404).
        max_attempts = self.max_per_court * 4
        while collected < self.max_per_court and attempted < max_attempts:
            try:
                result = _http_get_json(
                    f"{API_BASE}/search/",
                    params={
                        "type": "o",           # opinions
                        "court": court,
                        "filed_after": after_date,
                        "order_by": "dateFiled desc",
                        "page_size": 20,
                        "page": page,
                        "format": "json",
                        "q": self.query or "",
                    },
                    token=self.token,
                )
            except Exception as e:
                log.error("CourtListener search failed (%s p%d): %s", court, page, e)
                return

            hits = result.get("results", []) or []
            if not hits:
                log.info("CourtListener %s: no more results (page %d)", court, page)
                return

            for hit in hits:
                if collected >= self.max_per_court or attempted >= max_attempts:
                    return
                attempted += 1
                doc = self._hit_to_document(hit, court)
                if doc is not None:
                    collected += 1
                    yield doc
                time.sleep(REQUEST_DELAY_SEC)

            log.info("CourtListener %s: page %d done (collected=%d/%d attempted=%d)",
                     court, page, collected, self.max_per_court, attempted)

            if not result.get("next"):
                return
            page += 1

    # ------- per-hit processing -------

    def _hit_to_document(self, hit: dict, court: str) -> Document | None:
        case_name = hit.get("caseName") or hit.get("case_name") or "Unknown"
        date_filed = (hit.get("dateFiled") or "")[:10]
        docket_number = hit.get("docketNumber") or hit.get("docket_number") or ""
        citations = hit.get("citation") or []
        if isinstance(citations, str):
            citations = [citations]

        # Choose the first reporter citation if available, else synthesize one.
        primary_citation = next(
            (c for c in citations if isinstance(c, str) and c.strip()),
            None,
        )
        if not primary_citation:
            primary_citation = f"{case_name}, No. {docket_number} ({court.upper()} {date_filed})"

        # The CourtListener v4 `/opinions/<id>/` endpoint requires auth (401
        # anonymous), but each search hit inlines a `download_url` pointing at
        # the court's public PDF of the opinion. Prefer that: no token needed
        # and the text is authoritative.
        #
        # Fallbacks, in order:
        #   1. download_url  (court-hosted PDF)
        #   2. local_path    (CourtListener-hosted PDF under /pdf/...)
        #   3. snippet       (short, but better than nothing for the index)
        pdf_url: str | None = None
        local_path: str | None = None
        snippet: str = ""
        opinions_list = hit.get("opinions", []) or []
        absolute_url = hit.get("absolute_url") or ""

        for op in opinions_list:
            if not isinstance(op, dict):
                continue
            if not pdf_url and op.get("download_url"):
                pdf_url = op["download_url"]
            if not local_path and op.get("local_path"):
                local_path = op["local_path"]
            if not snippet and op.get("snippet"):
                snippet = _clean_html(op["snippet"])
            if pdf_url and snippet:
                break

        # Cache key is stable per (court, filed date, docket)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", case_name)[:60]
        cache_stem = f"{court}__{date_filed}__{safe}"
        pdf_cache = self.cache_dir / f"{cache_stem}.pdf"
        meta_cache = self.cache_dir / f"{cache_stem}.json"

        text = ""

        # 1. Try the PDF path (download + extract).
        if pdf_url or local_path:
            if not pdf_cache.exists() or pdf_cache.stat().st_size < 1000:
                target = pdf_url or (
                    f"https://www.courtlistener.com/{local_path.lstrip('/')}"
                    if local_path else None
                )
                if target:
                    # Some court PDFs have spaces or special chars in the path
                    # (e.g. FL Supreme Court multi-docket opinions). urllib
                    # rejects raw spaces, so encode the path portion while
                    # leaving scheme/host/query intact.
                    target = _url_quote_path(target)
                    try:
                        req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            pdf_cache.write_bytes(resp.read())
                    except Exception as e:
                        log.warning("PDF download failed (%s): %s", case_name, e)
                        pdf_cache = None  # signal: no PDF
            if pdf_cache and pdf_cache.exists() and pdf_cache.stat().st_size >= 1000:
                text = _extract_pdf_text(pdf_cache)

        # 2. Fallback to snippet if the PDF path failed or produced nothing.
        if len(text.strip()) < 200 and snippet:
            log.debug("using snippet for %s (len=%d)", case_name, len(snippet))
            text = snippet

        text = text.strip()
        if len(text) < 100:
            log.debug("skip (no usable text): %s", case_name)
            return None
        text = text[:MAX_TEXT_CHARS]

        # Cache the structured metadata so future runs can skip reprocessing
        try:
            meta_cache.write_text(json.dumps({
                "case_name": case_name,
                "date_filed": date_filed,
                "docket_number": docket_number,
                "citations": citations,
                "court": court,
                "pdf_url": pdf_url,
                "local_path": local_path,
            }), encoding="utf-8")
        except Exception:
            pass

        opinion_url = (
            "https://www.courtlistener.com" + absolute_url
            if absolute_url.startswith("/")
            else (absolute_url or pdf_url or "")
        )

        header = (
            f"{case_name}\n"
            f"{primary_citation}\n"
            f"Court: {court.upper()}   Filed: {date_filed}\n"
            f"{'-' * 60}\n\n"
        )

        md = {
            "jurisdiction": self.jurisdiction_code,
            "source_type": "case",
            "citation": primary_citation,
            "case_name": case_name,
            "court": court,
            "decision_date": date_filed,
            "docket_number": str(docket_number or ""),
            "title": "",
            "chapter": "",
            "section": "",
            "topic": "case_law",
            "official_url": opinion_url,
            "retrieved_at": datetime.date.today().isoformat(),
        }
        return Document(text=header + text, metadata=md)
