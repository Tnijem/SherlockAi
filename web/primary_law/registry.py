"""
primary_law.registry - load and validate firm + jurisdiction config.

Reads:
    ~/Sherlock/config/firm.yaml
    ~/Sherlock/config/jurisdictions/<CODE>.yaml

Exposes typed accessors used by the ingest pipeline and by rag.py retrieval.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    raise


# Resolve config dir the same way config.py resolves DB_PATH - relative to the
# Sherlock root, with an env var override for deployments that move it.
SHERLOCK_ROOT = Path(os.environ.get("SHERLOCK_ROOT", Path(__file__).resolve().parents[2]))
CONFIG_DIR = Path(os.environ.get("SHERLOCK_CONFIG_DIR", SHERLOCK_ROOT / "config"))


@dataclass
class StatutesConfig:
    citation_format: str
    source_type: str
    source_base_url: str
    practice_area_map: dict[str, list[int]]


@dataclass
class CourtRule:
    name: str
    type: str
    url: str | None = None
    see: str | None = None


@dataclass
class Legislation:
    name: str
    year: int
    description: str
    type: str
    url: str
    effective_date: str | None = None


@dataclass
class Jurisdiction:
    code: str
    name: str
    statutes: StatutesConfig
    court_rules: list[CourtRule] = field(default_factory=list)
    courtlistener_courts: list[str] = field(default_factory=list)
    legislation: list[Legislation] = field(default_factory=list)

    def titles_for_practice_areas(self, areas: list[str]) -> list[int]:
        """Union of statute titles across the requested practice areas."""
        titles: set[int] = set()
        for area in areas:
            for t in self.statutes.practice_area_map.get(area, []):
                titles.add(int(t))
        return sorted(titles)


@dataclass
class FirmConfig:
    name: str
    primary_jurisdiction: str
    jurisdictions: list[str]
    practice_areas: list[str]
    case_law_lookback_years: int
    case_law_max_per_court: int


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected top-level mapping, got {type(data).__name__}")
    return data


def load_firm(config_dir: Path = CONFIG_DIR) -> FirmConfig:
    data = _load_yaml(config_dir / "firm.yaml")
    firm = data.get("firm", {})
    required = ["name", "primary_jurisdiction", "jurisdictions", "practice_areas"]
    missing = [k for k in required if k not in firm]
    if missing:
        raise ValueError(f"firm.yaml missing required keys: {missing}")
    case_law = firm.get("case_law", {}) or {}
    return FirmConfig(
        name=firm["name"],
        primary_jurisdiction=firm["primary_jurisdiction"],
        jurisdictions=list(firm["jurisdictions"]),
        practice_areas=list(firm["practice_areas"]),
        case_law_lookback_years=int(case_law.get("lookback_years", 10)),
        case_law_max_per_court=int(case_law.get("max_per_court", 2000)),
    )


def load_jurisdiction(code: str, config_dir: Path = CONFIG_DIR) -> Jurisdiction:
    data = _load_yaml(config_dir / "jurisdictions" / f"{code}.yaml")

    st = data.get("statutes", {}) or {}
    src = st.get("source", {}) or {}
    statutes = StatutesConfig(
        citation_format=st.get("citation_format", "§ {section}"),
        source_type=src.get("type", "unknown"),
        source_base_url=src.get("base_url", ""),
        practice_area_map={k: list(v) for k, v in (st.get("practice_area_map") or {}).items()},
    )

    rules = [
        CourtRule(name=r["name"], type=r["type"], url=r.get("url"), see=r.get("see"))
        for r in (data.get("court_rules") or [])
    ]

    case_law = data.get("case_law", {}) or {}
    courts = list(case_law.get("courtlistener_courts") or [])

    legs = [
        Legislation(
            name=l["name"],
            year=int(l["year"]),
            description=l.get("description", ""),
            type=l.get("type", "pdf_url"),
            url=l["url"],
            effective_date=l.get("effective_date"),
        )
        for l in (data.get("legislation") or [])
    ]

    return Jurisdiction(
        code=data["code"],
        name=data["name"],
        statutes=statutes,
        court_rules=rules,
        courtlistener_courts=courts,
        legislation=legs,
    )


def load_all(config_dir: Path = CONFIG_DIR) -> tuple[FirmConfig, dict[str, Jurisdiction]]:
    firm = load_firm(config_dir)
    jurisdictions = {code: load_jurisdiction(code, config_dir) for code in firm.jurisdictions}
    return firm, jurisdictions


if __name__ == "__main__":
    # Smoke test: `python -m primary_law.registry` or direct invocation.
    firm, jurs = load_all()
    print(f"Firm: {firm.name}")
    print(f"Primary jurisdiction: {firm.primary_jurisdiction}")
    print(f"Practice areas: {firm.practice_areas}")
    print()
    for code, j in jurs.items():
        titles = j.titles_for_practice_areas(firm.practice_areas)
        print(f"[{code}] {j.name}")
        print(f"  statute source: {j.statutes.source_type} @ {j.statutes.source_base_url}")
        print(f"  titles to pull: {titles}")
        print(f"  court rules: {len(j.court_rules)}")
        print(f"  CL courts: {j.courtlistener_courts}")
        print(f"  legislation: {[l.name for l in j.legislation]}")
