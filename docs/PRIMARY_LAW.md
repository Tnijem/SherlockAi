# Primary-Law Ingest & Retrieval

Sherlock's `primary_law` subsystem ingests **authoritative legal sources** —
statutes, court rules, watched legislation, and case law — into a dedicated
Chroma collection (`primary_law`) that is queried alongside the firm's NAS
documents and boosted at retrieval time so the LLM cites real law rather than
hallucinating case numbers.

This document covers:

1. Architecture overview
2. Config files (`firm.yaml` and `jurisdictions/<CODE>.yaml`)
3. Running the ingest
4. How retrieval uses it
5. Adding a new jurisdiction

---

## 1. Architecture

```
┌─────────────────┐      ┌─────────────────────┐
│  firm.yaml      │      │ jurisdictions/      │
│  (tenant cfg)   │      │   GA.yaml, FL.yaml  │
└────────┬────────┘      │   (product cfg)     │
         │               └──────────┬──────────┘
         │                          │
         └────────┬─────────────────┘
                  ▼
        primary_law.registry (loader)
                  │
                  ▼
        primary_law.ingest.run_ingest()
                  │
        ┌─────────┼────────┬───────────┬─────────┐
        ▼         ▼        ▼           ▼         ▼
   statute     rule     legislation  case      (future)
   fetcher   fetcher     fetcher   fetcher
        │         │        │           │
        ▼         ▼        ▼           ▼
              Document → chunker → embedder
                                     │
                                     ▼
                           Chroma `primary_law`
                                     │
                                     ▼
                        rag.retrieve() merge + boost
```

Two-layer config separation is intentional:

- **`firm.yaml`** is *per-tenant*: which states, which practice areas, case-law
  depth. This file is what a new customer edits.
- **`jurisdictions/*.yaml`** is *per-product*: how to fetch each state's
  statutes, which CourtListener slugs map to which courts, what the citation
  format looks like. This file ships as part of Sherlock and gets updated
  centrally when, e.g., Florida reorganizes its rules.

Adding a new firm is a one-file change. Adding a new state ships for every
firm at once.

---

## 2. Config reference

### 2.1 `config/firm.yaml`

```yaml
firm:
  # Display name (shown in UI headers, logs)
  name: "Dennis Law"

  # Two-letter state code of the firm's home jurisdiction. Used as a
  # retrieval tie-breaker and for citation formatting defaults.
  primary_jurisdiction: GA

  # All jurisdictions this firm works in. Each must have a matching file
  # at config/jurisdictions/<CODE>.yaml.
  jurisdictions: [GA, FL]

  # Practice-area keys. Each jurisdiction file maps these to statute
  # titles/chapters. Unknown keys are ignored with a warning.
  practice_areas:
    - personal_injury
    - contracts
    - civil_procedure
    - estates

  # Case-law ingest scope (applies to every jurisdiction unless overridden
  # on the command line).
  case_law:
    lookback_years: 10     # only opinions filed in the last N years
    max_per_court: 2000    # cap per CourtListener court slug
```

**Built-in practice_area keys** (shipped in the reference jurisdiction files):

| Key               | Typical scope                                                  |
| ----------------- | -------------------------------------------------------------- |
| `personal_injury` | Torts, negligence, damages caps                                |
| `contracts`       | Contract formation, SOL, UCC, interest                         |
| `civil_procedure` | Rules of civil procedure, pleadings, motions, SOL              |
| `estates`         | Wills, probate, trusts                                         |
| `criminal`        | Criminal code, offenses, sentencing                            |
| `family`          | Dissolution, custody, adoption                                 |
| `property`        | Real property, conveyances, landlord/tenant, condos            |
| `business`        | Corporations, LLCs, partnerships                               |

A firm only ingests the areas listed in its `practice_areas`, which keeps
Chroma small and retrieval sharp.

### 2.2 `config/jurisdictions/<CODE>.yaml`

Two concrete examples ship with Sherlock: `GA.yaml` and `FL.yaml`. Full
schema:

```yaml
code: GA                 # Two-letter USPS code (matches filename)
name: Georgia

statutes:
  # How a section is cited in text. {section} is substituted per item.
  citation_format: "O.C.G.A. § {section}"

  source:
    # Which fetcher class handles this state. Current supported values:
    #   resource_org_ga  - Georgia OCGA via Public.Resource.Org zip
    #   flsenate         - Florida Statutes via flsenate.gov /Chapter/All
    type: resource_org_ga
    base_url: "https://law.resource.org/pub/us/code/ga/"
    # Optional: pin a specific release (otherwise fetcher uses its default)
    release: "gov.ga.ocga.2019.08.21.release.73.zip"

  # practice_area_key -> list of statute titles (GA, NY, TX…) or chapters (FL).
  # For states with a flat chapter numbering (FL, LA), list chapter numbers.
  # For title/chapter states (GA, NY), list title numbers.
  practice_area_map:
    personal_injury: [51]          # GA Title 51 - Torts
    contracts:       [13]          # GA Title 13 - Contracts
    civil_procedure: [9]           # GA Title 9 - Civ Proc (embeds GACPA)
    estates:         [53]
    criminal:        [16, 17]
    family:          [19]
    property:        [44]
    business:        [14]

court_rules:
  # Each entry dispatches by `type`.
  # Types:
  #   pdf_url   - direct URL to a PDF (fetched by PdfUrlFetcher)
  #   html      - HTML index (TODO, currently skipped with warning)
  #   alias     - "content is already covered by statutes title.X.chapter.Y";
  #               noop, kept as documentation

  - name: "Uniform Superior Court Rules"
    type: html
    url: "https://www.gasupreme.us/court-information/uscr/"

  - name: "Georgia Rules of Civil Procedure"
    type: alias
    see: "statutes.title.9.chapter.11"

case_law:
  # CourtListener court slugs to pull opinions from. See
  # https://www.courtlistener.com/help/api/rest/#courts-endpoint
  # for the full list. Keep this to appellate courts only - trial-level
  # opinions flood the index with low-value memoranda.
  courtlistener_courts:
    - ga          # Supreme Court of Georgia
    - gactapp     # Georgia Court of Appeals

legislation:
  # Optional "watched bills" — recent statutes that fundamentally change the
  # law in ways still percolating through case law. Ingested as a separate
  # source_type so retrieval can weight them alongside the underlying code.
  # FL ships one entry here: HB 837 (2023 tort reform).
  []
```

**FL-specific note**: Florida uses a flat chapter.section numbering
(`768.81`), so `practice_area_map` in `FL.yaml` lists chapter numbers, not
titles. The `flsenate` fetcher treats each list entry as a chapter number to
hit `/Laws/Statutes/<year>/ChapterNN/All`.

---

## 3. Running the ingest

The `scripts/ingest_primary_law.py` CLI is the single entry point.

```bash
cd ~/Sherlock
./venv/bin/python scripts/ingest_primary_law.py [flags]
```

### Common invocations

```bash
# Full run for every jurisdiction in firm.yaml (statutes + rules + cases + legs)
./venv/bin/python scripts/ingest_primary_law.py

# Only Georgia, only statutes
./venv/bin/python scripts/ingest_primary_law.py \
    --jurisdictions GA --source-types statute

# Smoke test: GA Title 9 Chapter 3 only, no embedding/upsert
./venv/bin/python scripts/ingest_primary_law.py \
    --titles 9 --chapters 9:3 --dry-run

# Case-law smoke: 5 recent GA Supreme Court opinions
./venv/bin/python scripts/ingest_primary_law.py \
    --jurisdictions GA --source-types case \
    --case-lookback-years 1 --case-max-per-court 5

# Case-law full pull (takes hours on anonymous CourtListener tier;
# set COURTLISTENER_TOKEN for 5k req/day)
COURTLISTENER_TOKEN=xxx ./venv/bin/python scripts/ingest_primary_law.py \
    --source-types case
```

### CLI flags

| Flag                          | Default                                  | Notes                                    |
| ----------------------------- | ---------------------------------------- | ---------------------------------------- |
| `--jurisdictions GA,FL`       | firm.yaml jurisdictions                  | Subset to run this time                  |
| `--source-types statute,rule` | `statute,rule,legislation,case`          | Which fetchers to run                    |
| `--dry-run`                   | off                                      | Fetch+chunk only; no embed, no Chroma    |
| `--titles 9,13`               | all titles for firm's practice areas     | Restrict statute fetchers                |
| `--chapters 9:3;13:1,2`       | all chapters                             | Per-title chapter filter (smoke tests)   |
| `--case-lookback-years 10`    | firm.yaml `case_law.lookback_years`      | Opinions filed within N years            |
| `--case-max-per-court 2000`   | firm.yaml `case_law.max_per_court`       | Cap per court slug                       |
| `--case-query "tort reform"`  | empty                                    | Free-text filter passed to CourtListener |
| `-v`                          | off                                      | Debug logging                            |

### Idempotency

Every chunk gets a deterministic ID:

    plaw_<sha1(jurisdiction|citation|chunk_index)[:20]>

Re-running the ingest overwrites (upserts) existing rows instead of creating
duplicates. Safe to run daily from cron.

### Caching

All remote fetches are cached under `~/Sherlock/data/primary_law_cache/`:

```
primary_law_cache/
├── GA/
│   ├── statutes/     # OCGA zip + extracted per-title ODT files
│   ├── rules/        # pdf_url fetcher cache
│   └── cases/        # one JSON per CourtListener opinion
└── FL/
    ├── statutes/     # per-chapter HTML pages
    ├── rules/        # FRCP PDF
    ├── legislation/  # HB 837 PDF
    └── cases/
```

Delete a subdirectory to force re-fetch of that source on the next run.

---

## 4. Retrieval integration

`rag.py` imports `primary_law.PRIMARY_LAW_COLLECTION` and, on every user query,
runs a *second* vector search against that collection in parallel with the
usual NAS / user-docs retrieval. Results are merged into the ranked context
list with a score boost:

```python
PRIMARY_LAW_SCORE_BOOST = 0.15   # additive, capped at 1.0
PRIMARY_LAW_TOP_N       = 6      # how many primary-law hits to inject
```

The boost reflects the fact that primary law is authoritative: an OCGA
section that *directly* answers the question is always more trustworthy than
a NAS memo that *talks about* the section.

Results are filtered by the firm's configured jurisdictions (`$in`), so a
firm that works only in GA/FL will never see, e.g., a Texas statute even if
the collection contains one.

If the `primary_law` package can't be imported (broken install, missing
config), rag.py logs a warning and falls back to NAS-only retrieval — the
chat system stays online.

---

## 5. Adding a new jurisdiction

**For a new firm in an already-supported state**:
1. Add the state code to `firm.yaml` → `firm.jurisdictions`
2. Confirm the `practice_areas` in `firm.yaml` match what you want to index
3. Run `scripts/ingest_primary_law.py --jurisdictions NEWCODE`

**For a state Sherlock doesn't ship yet**:
1. Create `config/jurisdictions/<CODE>.yaml` following the schema above
2. Write (or reuse) a fetcher in `web/primary_law/fetchers/`:
   - If the state publishes statutes as a downloadable zip like GA → copy
     `resource_org_ga.py` and change the release name and regexes
   - If the state publishes per-chapter HTML like FL → copy `flsenate.py`
     and retune the CSS class names
   - Otherwise write a new fetcher subclassing `Fetcher` from
     `primary_law.fetchers.base`
3. Register the new `source.type` string in `ingest.build_statute_fetcher()`
4. Smoke test: `--jurisdictions NEWCODE --chapters <title>:<ch> --dry-run`
5. Full ingest: `--jurisdictions NEWCODE`

Court rules and case law work without any code changes if the new
jurisdiction's YAML uses `pdf_url` rules and standard CourtListener slugs.

---

## 6. Troubleshooting

| Symptom                                           | Cause / fix                                               |
| ------------------------------------------------- | --------------------------------------------------------- |
| `primary_law: firm config unreadable`             | `config/firm.yaml` missing/typo. `yaml.safe_load` fails.  |
| `unsupported statute source type: <x>`            | YAML references a fetcher that isn't wired in ingest.py   |
| `embed_failed (all fallbacks exhausted)`          | Ollama returning 500 on chunk. Usually PDF table/forms.   |
|                                                   | Logged as warning; ingest continues. Check ~0.1–10% loss. |
| `no statute titles for practice areas ...`        | `practice_area_map` in jurisdiction YAML has no overlap   |
|                                                   | with firm's `practice_areas`. Add the key or broaden.     |
| `BM25 search failed: no such table: chunk_fts`    | Pre-existing FTS index missing; vector search still runs. |
| FRCP / legislation PDF URL 404                    | Florida Bar rotates URLs. Fetch fresh link from           |
|                                                   | https://www.floridabar.org/rules/ctproc/ and update FL.yaml |
| CourtListener `429 Too Many Requests`             | Anonymous limit is ~60/hr. Register and set               |
|                                                   | `COURTLISTENER_TOKEN` for 5,000 req/day.                  |
