#!/usr/bin/env python3
"""
Fetch 50 random cases from CourtListener and save PDFs/text to Sherlock uploads.
Usage: python3 fetch_courtlistener.py
"""

import os
import re
import sys
import time
import random
import hashlib
import requests
from pathlib import Path

TOKEN   = os.environ.get("COURTLISTENER_TOKEN", "")
BASE    = "https://www.courtlistener.com"
API     = f"{BASE}/api/rest/v4"
OUT_DIR = Path(__file__).parent.parent / "SampleData"

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "User-Agent":    "Sherlock-RAG/1.0",
}

TARGET   = 50
TIMEOUT  = 30

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── helpers ──────────────────────────────────────────────────────────────────

def safe_name(s: str, max_len: int = 80) -> str:
    s = re.sub(r'[^\w\s\-]', '', s).strip()
    s = re.sub(r'\s+', '_', s)
    return s[:max_len]

def already_done() -> set[str]:
    return {f.stem for f in OUT_DIR.iterdir() if f.is_file()}

def save_text(content: str, stem: str) -> Path:
    p = OUT_DIR / f"{stem}.txt"
    p.write_text(content, encoding="utf-8")
    return p

def download_pdf(url: str, stem: str) -> Path | None:
    if not url:
        return None
    ext = Path(url.split("?")[0]).suffix.lower() or ".pdf"
    p   = OUT_DIR / f"{stem}{ext}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        if r.status_code == 200 and int(r.headers.get("content-length", 1)) > 0:
            with p.open("wb") as fh:
                for chunk in r.iter_content(8192):
                    fh.write(chunk)
            return p
    except Exception as e:
        print(f"    ⚠ download failed: {e}")
    return None

# ── fetch opinion list across random pages ────────────────────────────────────

def fetch_opinion_page(page: int) -> list[dict]:
    try:
        r = requests.get(
            f"{API}/opinions/",
            headers=HEADERS,
            params={
                "format":    "json",
                "page_size": 20,
                "page":      page,
                "order_by":  "random",          # CourtListener supports this
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 400:
            # random ordering not supported — fall back to date desc
            r = requests.get(
                f"{API}/opinions/",
                headers=HEADERS,
                params={
                    "format":    "json",
                    "page_size": 20,
                    "page":      page,
                    "order_by":  "-date_created",
                },
                timeout=TIMEOUT,
            )
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        print(f"  ⚠ page {page} error: {e}")
        return []

# ── fetch cluster (case) metadata to get a nice case name ────────────────────

def fetch_cluster(cluster_url: str) -> dict:
    try:
        r = requests.get(cluster_url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    done = already_done()
    saved = 0
    pages_tried = set()

    print(f"CourtListener → Sherlock  |  target: {TARGET} files  |  output: {OUT_DIR}")
    print("-" * 70)

    # Pick random starting pages spread across a wide range
    candidate_pages = random.sample(range(1, 500), 60)

    for page in candidate_pages:
        if saved >= TARGET:
            break
        if page in pages_tried:
            continue
        pages_tried.add(page)

        opinions = fetch_opinion_page(page)
        if not opinions:
            continue

        random.shuffle(opinions)

        for op in opinions:
            if saved >= TARGET:
                break

            op_id       = op.get("id", "")
            cluster_url = op.get("cluster", "")
            download_url= op.get("download_url", "") or ""
            plain_text  = op.get("plain_text", "")    or ""
            html        = op.get("html_with_citations", "") or op.get("html", "") or ""
            op_type     = op.get("type", "")

            # Build a safe filename stem
            cluster = fetch_cluster(cluster_url) if cluster_url else {}
            case_name  = cluster.get("case_name", "") or cluster.get("case_name_short", "") or f"opinion_{op_id}"
            docket     = cluster.get("docket") if isinstance(cluster.get("docket"), dict) else {}
            court      = cluster.get("court_citation_string", docket.get("court", ""))
            date_filed = cluster.get("date_filed", "")[:10] if cluster.get("date_filed") else ""

            stem = safe_name(f"{date_filed}_{case_name}_{op_id}" if date_filed else f"{case_name}_{op_id}")

            if stem in done:
                print(f"  → skip (exists): {stem[:60]}")
                continue

            saved_path = None

            # Prefer actual document download
            if download_url and download_url.startswith("http"):
                print(f"  [{saved+1}/{TARGET}] Downloading PDF: {case_name[:55]}…")
                saved_path = download_pdf(download_url, stem)

            # Fall back to plain text
            if not saved_path and plain_text and len(plain_text) > 200:
                header = f"Case: {case_name}\nCourt: {court}\nDate Filed: {date_filed}\nOpinion Type: {op_type}\n"
                header += f"Source: {BASE}{op.get('absolute_url','')}\n\n"
                saved_path = save_text(header + plain_text, stem)
                print(f"  [{saved+1}/{TARGET}] Saved text:      {case_name[:55]}… ({len(plain_text):,} chars)")

            # Last resort: save HTML stripped to text
            if not saved_path and html and len(html) > 200:
                # Strip HTML tags for readability
                text = re.sub(r'<[^>]+>', ' ', html)
                text = re.sub(r'\s+', ' ', text).strip()
                if len(text) > 200:
                    header = f"Case: {case_name}\nCourt: {court}\nDate Filed: {date_filed}\nSource: {BASE}{op.get('absolute_url','')}\n\n"
                    saved_path = save_text(header + text, stem)
                    print(f"  [{saved+1}/{TARGET}] Saved HTML→txt: {case_name[:55]}… ({len(text):,} chars)")

            if saved_path:
                saved += 1
                done.add(stem)
                size = saved_path.stat().st_size
                print(f"    ✓ {saved_path.name}  ({size/1024:.1f} KB)")
            else:
                print(f"  ✗ No content for opinion {op_id}: {case_name[:50]}")

            time.sleep(0.3)   # polite rate limit

        time.sleep(0.5)

    print()
    print(f"{'─'*70}")
    print(f"Done. {saved}/{TARGET} files saved to {OUT_DIR}")

    if saved < TARGET:
        print(f"⚠ Only got {saved} files — CourtListener may have rate-limited some pages.")

    return saved


if __name__ == "__main__":
    n = main()
    sys.exit(0 if n > 0 else 1)
