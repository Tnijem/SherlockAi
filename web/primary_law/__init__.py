"""
primary_law - Sherlock primary-law ingestion pipeline.

Pulls statutes, court rules, case law, and watched legislation from authoritative
sources into a dedicated Chroma collection (`primary_law`) so the RAG stack can
cite real statutory text instead of hallucinating citations.

Architecture:
  - config/firm.yaml                       what this firm practices and where
  - config/jurisdictions/<CODE>.yaml       how to fetch each jurisdiction
  - primary_law.registry                   loads and validates config
  - primary_law.fetchers.*                 one fetcher per source type
  - primary_law.chunker                    section-aware splitter
  - primary_law.ingest                     orchestrator (fetch -> chunk -> embed -> upsert)

New jurisdictions: drop a config/jurisdictions/<CODE>.yaml file, add the code to
firm.yaml, re-run `scripts/ingest_primary_law.py`. No code changes required.
"""

PRIMARY_LAW_COLLECTION = "primary_law"
