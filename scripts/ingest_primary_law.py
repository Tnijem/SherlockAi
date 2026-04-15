#!/usr/bin/env python3
"""
CLI: ingest primary-law sources into the `primary_law` Chroma collection.

Usage:
    python scripts/ingest_primary_law.py                      # full run per firm.yaml
    python scripts/ingest_primary_law.py --jurisdictions GA   # only GA
    python scripts/ingest_primary_law.py --titles 9 --chapters 9:3    # smoke test
    python scripts/ingest_primary_law.py --dry-run            # no embedding, no upsert

Env overrides:
    SHERLOCK_ROOT           root of Sherlock install (default: repo root)
    SHERLOCK_CONFIG_DIR     override config dir
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the primary_law package importable. This script is installed at
# ~/Sherlock/scripts/ingest_primary_law.py; the package is at ~/Sherlock/web/primary_law/
WEB_DIR = Path(os.environ.get("SHERLOCK_WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
sys.path.insert(0, str(WEB_DIR))

# Also chdir to web dir so relative paths inside config.py work the same as
# when run under uvicorn. (config.py uses os.path.dirname(__file__) which is
# resilient, but other modules might not be.)
os.chdir(WEB_DIR)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest primary-law sources into Chroma.")
    p.add_argument("--jurisdictions", help="comma-separated state codes, e.g. GA,FL (default: firm.yaml)")
    p.add_argument("--source-types", default="statute,rule,legislation,case",
                   help="comma-separated source types (statute,rule,legislation,case). "
                        "Default: all.")
    p.add_argument("--dry-run", action="store_true", help="no embedding, no Chroma writes")
    p.add_argument("--titles", help="comma-separated integer titles to restrict to (smoke test)")
    p.add_argument("--chapters", help="per-title chapter filter, e.g. '9:3' or '9:3,11;51:1'")
    p.add_argument("--case-lookback-years", type=int,
                   help="override firm.yaml case_law.lookback_years")
    p.add_argument("--case-max-per-court", type=int,
                   help="override firm.yaml case_law.max_per_court (useful for smoke test)")
    p.add_argument("--case-query", default="",
                   help="optional free-text filter passed to CourtListener search")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args()


def parse_chapter_filter(s: str | None) -> dict[int, list[int]] | None:
    if not s:
        return None
    out: dict[int, list[int]] = {}
    for part in s.split(";"):
        if not part:
            continue
        title_s, _, chaps_s = part.partition(":")
        if not chaps_s:
            continue
        out[int(title_s)] = [int(c) for c in chaps_s.split(",") if c]
    return out


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from primary_law.ingest import IngestOptions, run_ingest

    opts = IngestOptions(
        jurisdictions=[j.strip() for j in args.jurisdictions.split(",")] if args.jurisdictions else None,
        source_types=[s.strip() for s in args.source_types.split(",")],
        dry_run=args.dry_run,
        title_filter=[int(t) for t in args.titles.split(",")] if args.titles else None,
        chapter_filter=parse_chapter_filter(args.chapters),
        case_lookback_years=args.case_lookback_years,
        case_max_per_court=args.case_max_per_court,
        case_query=args.case_query,
    )

    stats = run_ingest(opts)
    print()
    print("=" * 60)
    print(f"fetchers_run:       {stats.fetchers_run}")
    print(f"documents_fetched:  {stats.documents_fetched}")
    print(f"chunks_created:     {stats.chunks_created}")
    print(f"chunks_embedded:    {stats.chunks_embedded}")
    print(f"chunks_upserted:    {stats.chunks_upserted}")
    print(f"errors:             {stats.errors}")
    print(f"elapsed:            {stats.elapsed_s:.1f}s")
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
