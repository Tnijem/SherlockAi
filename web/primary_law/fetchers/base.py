"""
primary_law.fetchers.base - Fetcher ABC and Document type.

Every source-specific fetcher (justia, flsenate, pdf_url, html_url, courtlistener)
returns a list of Document instances with a uniform metadata schema. The ingest
orchestrator doesn't care where a document came from - it just chunks, embeds,
and upserts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator


@dataclass
class Document:
    """A single authoritative-source document before chunking.

    `text` is the full plain-text body. `metadata` follows a fixed schema so
    Chroma filters work uniformly across source types.
    """
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Fixed metadata keys every Document must set before chunking. The chunker
# copies these onto each resulting chunk and adds `chunk_index`/`chunk_total`.
REQUIRED_METADATA_KEYS = {
    "jurisdiction",   # "GA", "FL", ...
    "source_type",    # "statute" | "rule" | "case" | "legislation"
    "citation",       # human-readable cite, e.g. "O.C.G.A. § 9-3-24"
    "official_url",   # where it was fetched from
    "retrieved_at",   # ISO date
}

# Optional but commonly used.
OPTIONAL_METADATA_KEYS = {
    "title",          # statute title ("9", "51", "768")
    "chapter",        # "3"
    "section",        # "9-3-24"
    "topic",          # practice area tag, e.g. "contracts"
    "court",          # for cases: "ga", "fla"
    "case_name",      # for cases
    "decision_date",  # for cases, ISO
    "docket",         # for cases
}


def validate_metadata(md: dict[str, Any]) -> None:
    missing = REQUIRED_METADATA_KEYS - set(md.keys())
    if missing:
        raise ValueError(f"Document metadata missing required keys: {sorted(missing)}")


class Fetcher(ABC):
    """Base class. Each source type implements fetch() as a generator of Documents."""

    source_type: str = "unknown"

    def __init__(self, jurisdiction_code: str, **kwargs: Any):
        self.jurisdiction_code = jurisdiction_code
        self.opts = kwargs

    @abstractmethod
    def fetch(self) -> Iterator[Document]:
        """Yield Documents one at a time. Implementations should be resumable
        and idempotent - skipping already-fetched items if a cache exists."""
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} jur={self.jurisdiction_code} opts={self.opts}>"
