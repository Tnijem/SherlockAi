"""
Download random cases from the harvard-lil/cold-cases HuggingFace dataset
and save them as plain-text files for Sherlock indexing.

Usage:
    python fetch_cold_cases.py [--count N] [--court COURT_ID] [--out DIR] [--skip N]

Examples:
    python fetch_cold_cases.py --count 50
    python fetch_cold_cases.py --count 20 --court scotus
    python fetch_cold_cases.py --count 100 --out /data/cases --skip 5000

Requires:
    pip install datasets huggingface_hub
"""

import argparse
import re
import random
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Fetch cold cases from HuggingFace")
    p.add_argument("--count",  type=int,   default=50,  help="Number of case files to write (default: 50)")
    p.add_argument("--court",  type=str,   default=None, help="Filter by court_id (e.g. scotus, ca9)")
    p.add_argument("--out",    type=str,   default=None, help="Output directory (default: SampleData next to this script)")
    p.add_argument("--skip",   type=int,   default=0,    help="Skip first N rows of the dataset (for variety)")
    p.add_argument("--scan",   type=int,   default=10000, help="Rows to scan for reservoir sampling (default: 10000)")
    return p.parse_args()


def safe_name(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s or "unknown")
    s = re.sub(r"\s+", "_", s.strip())
    return s[:maxlen] or "unknown"


def row_to_text(row: dict) -> str:
    name      = row.get("case_name") or row.get("case_name_short") or "Unknown"
    date      = str(row.get("date_filed") or row.get("date_created") or "unknown")[:10]
    court     = row.get("court_id") or "unknown"
    docket    = row.get("docket_number") or ""
    status    = row.get("precedential_status") or ""
    judges    = row.get("judges") or ""
    attorneys = row.get("attorneys") or ""
    syllabus  = row.get("syllabus") or ""
    headnotes = row.get("headnotes") or ""
    summary   = row.get("summary") or ""
    opinion   = row.get("plain_text") or row.get("html_with_citations") or ""
    opinion   = re.sub(r"<[^>]+>", " ", opinion)
    opinion   = re.sub(r"\s+", " ", opinion).strip()

    lines = [f"CASE: {name}", f"DATE: {date}", f"COURT: {court}"]
    if docket:    lines.append(f"DOCKET: {docket}")
    if status:    lines.append(f"STATUS: {status}")
    if judges:    lines.append(f"JUDGES: {judges}")
    if attorneys: lines.append(f"ATTORNEYS: {attorneys}")
    if syllabus:  lines += ["", "SYLLABUS:", syllabus.strip()]
    if headnotes: lines += ["", "HEADNOTES:", headnotes.strip()]
    if summary:   lines += ["", "SUMMARY:", summary.strip()]
    if opinion:   lines += ["", "OPINION:", opinion[:50000]]

    return "\n".join(lines)


def row_filename(row: dict) -> str:
    name  = row.get("case_name") or row.get("case_name_short") or "Unknown"
    date  = str(row.get("date_filed") or row.get("date_created") or "unknown")[:10]
    court = row.get("court_id") or "unknown"
    return f"{date}_{safe_name(name)}_{court}.txt"


def main():
    args = parse_args()

    script_dir = Path(__file__).parent
    out_dir = Path(args.out) if args.out else script_dir / "SampleData"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Deduplication: track existing filenames to avoid overwrites
    existing_names = {f.name for f in out_dir.iterdir() if f.is_file()}
    print(f"Output: {out_dir}  ({len(existing_names)} existing files)")

    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: 'datasets' library not installed. Run: pip install datasets huggingface_hub")
        raise SystemExit(1)

    want      = args.count
    scan      = args.scan
    skip      = args.skip
    court_flt = args.court

    oversample = want * 5  # reservoir oversize to handle empty/filtered rows
    print(f"Streaming dataset (skip={skip}, scan={scan}, court={court_flt or 'any'})...")

    ds = load_dataset("harvard-lil/cold-cases", split="train", streaming=True)

    reservoir = []
    scanned   = 0
    for i, row in enumerate(ds):
        if i < skip:
            continue
        if scanned >= scan:
            break

        if court_flt and row.get("court_id") != court_flt:
            scanned += 1
            continue

        if len(reservoir) < oversample:
            reservoir.append(row)
        else:
            j = random.randint(0, scanned)
            if j < oversample:
                reservoir[j] = row

        scanned += 1
        if scanned % 2000 == 0:
            print(f"  scanned {scanned}/{scan}...", flush=True)

    print(f"Reservoir: {len(reservoir)} candidates. Writing up to {want} files...")

    written = 0
    for row in reservoir:
        if written >= want:
            break
        fname   = row_filename(row)
        content = row_to_text(row)

        if len(content) < 100:
            continue  # skip near-empty
        if fname in existing_names:
            continue  # skip duplicates

        (out_dir / fname).write_text(content, encoding="utf-8")
        existing_names.add(fname)
        written += 1
        print(f"  [{written:3d}] {fname}")

    print(f"\nDone — {written} files written to {out_dir}")
    if written < want:
        print(f"  Note: only {written}/{want} written — increase --scan to find more candidates")


if __name__ == "__main__":
    main()
