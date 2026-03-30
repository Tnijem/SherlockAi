"""
Stream 50 random cases from harvard-lil/cold-cases and save as .txt files in SampleData.
Uses streaming so we don't download the full 2.5GB parquet.
"""
import random
import re
import sys
from pathlib import Path

from datasets import load_dataset

OUTPUT_DIR = Path("/Users/nijemtech/Sherlock/SampleData")
OUTPUT_DIR.mkdir(exist_ok=True)

WANT = 50
SAMPLE_FROM = 5000  # reservoir sample from first 5000 rows to get variety

print(f"Streaming harvard-lil/cold-cases, reservoir-sampling {WANT} from first {SAMPLE_FROM}...")

ds = load_dataset("harvard-lil/cold-cases", split="train", streaming=True)

reservoir = []
for i, row in enumerate(ds):
    if i >= SAMPLE_FROM:
        break
    # Reservoir sampling
    if len(reservoir) < WANT:
        reservoir.append(row)
    else:
        j = random.randint(0, i)
        if j < WANT:
            reservoir[j] = row
    if (i + 1) % 500 == 0:
        print(f"  scanned {i+1}/{SAMPLE_FROM}...", flush=True)

print(f"Writing {len(reservoir)} files to {OUTPUT_DIR}")

def safe_name(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s or "unknown")
    s = re.sub(r"\s+", "_", s.strip())
    return s[:maxlen]

written = 0
for row in reservoir:
    # Build a readable text file from the case fields
    name      = row.get("case_name") or row.get("case_name_short") or "Unknown"
    date      = str(row.get("date_filed") or row.get("date_created") or "unknown_date")[:10]
    court     = row.get("court_id") or "unknown_court"
    docket    = row.get("docket_number") or ""
    status    = row.get("precedential_status") or ""
    judges    = row.get("judges") or ""
    attorneys = row.get("attorneys") or ""
    syllabus  = row.get("syllabus") or ""
    headnotes = row.get("headnotes") or ""
    summary   = row.get("summary") or ""
    opinion   = row.get("plain_text") or row.get("html_with_citations") or ""

    # Strip any residual HTML tags from opinion
    opinion = re.sub(r"<[^>]+>", " ", opinion)
    opinion = re.sub(r"\s+", " ", opinion).strip()

    lines = [
        f"CASE: {name}",
        f"DATE: {date}",
        f"COURT: {court}",
    ]
    if docket:   lines.append(f"DOCKET: {docket}")
    if status:   lines.append(f"STATUS: {status}")
    if judges:   lines.append(f"JUDGES: {judges}")
    if attorneys: lines.append(f"ATTORNEYS: {attorneys}")
    if syllabus:
        lines += ["", "SYLLABUS:", syllabus.strip()]
    if headnotes:
        lines += ["", "HEADNOTES:", headnotes.strip()]
    if summary:
        lines += ["", "SUMMARY:", summary.strip()]
    if opinion:
        lines += ["", "OPINION:", opinion[:50000]]  # cap at 50k chars

    content = "\n".join(lines)
    if len(content) < 100:
        continue  # skip near-empty rows

    fname = f"{date}_{safe_name(name)}_{court}.txt"
    out   = OUTPUT_DIR / fname
    out.write_text(content, encoding="utf-8")
    written += 1
    print(f"  [{written:2d}] {fname}")

print(f"\nDone — {written} files written to {OUTPUT_DIR}")
