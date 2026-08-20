# VeriRefer — Literature Search · DOI Check · Reference Completion

> Open-source academic reference skill. Six-layer search across nine scholarly APIs,
> DOI verification against doi.org/Crossref, and metadata completion that emits
> a clean `references.bib`.

## What This Skill Does

Four independent but composable reference operations, each runnable on its own:

| # | Feature | What it does | Entry point |
|---|---|---|---|
| 1 | Literature search | 6-layer search across Semantic Scholar, OpenAlex, CORE, Scopus, Zenodo, DOAJ, arXiv, Crossref-free, EuropePMC → deduplicated, scored `corpus.json` (typically 80–100 papers) | `run_search.py` |
| 2 | DOI check | Resolves every DOI against `doi.org`; flags unresolvable / hallucinated DOIs; Crossref title+author fallback to confirm papers exist | `run_check.py` |
| 3 | Reference completion | Backfills missing authors / journal / year / volume / issue / pages from Crossref + OpenAlex; emits a clean `references.bib` with unique citation keys | `run_check.py` |
| 4 | Semantic Scholar direct access | Paper search/details/match/citations/references, author lookup, **recommended papers** (forpaper + forlist), **S2AG bulk datasets** (releases / download links / incremental diffs) | `run_s2.py` |

Features 2 and 3 run together in a single pass (both need the same Crossref lookups).

## Invocation

You have a research topic. Two ways to drive the skill:

### A. Keywords only (fastest)

```bash
python run_search.py \
  --run-dir ./output \
  --keywords "generative AI" "higher education" "self-regulated learning" \
  --time-range 20YY-20YY
```

### B. From an `rq_final.json`

```bash
python run_search.py --run-dir ./output --rq ./rq_final.json
```

### Then run DOI check + reference completion

```bash
python run_check.py --run-dir ./output
```

### Optional: Semantic Scholar recommendations & datasets

```bash
# Recommended papers (single seed, or positive/negative lists)
python run_s2.py recommend --id DOI:10.1038/nature12373 --limit 10
python run_s2.py recommend --positive DOI:10.1038/nature12373,arXiv:1706.03762

# Author / paper lookups
python run_s2.py author "Oren Etzioni"
python run_s2.py paper "attention is all you need" --limit 5
python run_s2.py match "Attention Is All You Need"

# S2AG bulk datasets: releases, per-dataset download links, incremental diffs
python run_s2.py datasets
python run_s2.py datasets --release latest
python run_s2.py datasets --release latest --name abstracts
python run_s2.py datasets --name abstracts --diffs-from 2026-07-28
```

The search pipeline itself can grow the corpus with S2 recommendations (Layer 1b):
the highest-cited corpus papers seed `forpaper` recommendations. Enable it via
the `semantic_scholar.recommendations` config flag (off by default for unattended
runs since the recommender needs a working key to avoid the heavily-throttled
keyless pool), or pass `--s2-recommend-seeds N` to `run_search.py` (0 disables,
default 3, or `semantic_scholar.recommendations_seeds` in config). Scoring
reserves ~10% of the `--top-n` slots for recommender papers — they are topically
relevant by construction but rarely contain the literal search keywords.

The agent should always run **search → check** in sequence when the user asks for a full reference list. Either step may be run alone when the user only wants one capability.

## Pipeline

```
User input (sentence / keywords / rq_final.json)
        |
        v
[1] LITERATURE SEARCH  (run_search.py)
        |-- Layer 1: Semantic Scholar   (abstracts, high quality)
        |-- Layer 1b: S2 recommendations (forpaper expansion from top seeds, optional)
        |-- Layer 2: OpenAlex           (primary metadata backbone)
        |-- Layer 3: CORE               (open access + full-text URLs)
        |-- Layer 4: Scopus             (premium academic)
        |-- Layer 5: Zenodo + DOAJ + arXiv + Crossref-free + EuropePMC
        |-- Layer 6: Unpaywall + Crossref enrichment (in-place)
        |-- Dedup (source-priority) + relevance scoring + methodology tag
        v
   run_dir/corpus.json  (80-100 papers, metadata-enriched)
        |
        v
[2+3] DOI CHECK + REFERENCE COMPLETION  (run_check.py)
        |-- For each paper:
        |     - Crossref lookup by DOI, then title+author fallback
        |     - validate_doi() via doi.org HEAD request
        |     - OpenAlex biblio backfill (volume/issue/pages)
        |     - Known-hallucination blocklist (config/hallucinations.json)
        |-- Build unique citation keys (collision-resistant)
        v
   run_dir/references.bib         (clean BibTeX)
   run_dir/citation_keys.json     (verification status + incompleteness flags)
```

## Inputs

### Search input — pick ONE

| Source | How |
|---|---|
| Explicit keywords | `--keywords "kw1" "kw2" ...` |
| `rq_final.json` | `--rq path/to/rq_final.json` — needs `search_keywords`, `search_boundaries.time_range`, optional `key_concepts` |
| Sentence in chat | The agent should first derive 8+ keywords from the user's sentence, then pass them via `--keywords` |

### Configuration

Every credential is **optional**. Missing keys are skipped gracefully — the
search degrades rather than fails. Credentials resolve in the order
**environment variable → config file → skip**.

`config/api_keys.json` (git-ignored; copy from `config/api_keys.example.json`):

```json
{
  "openalex":          { "enabled": true, "api_key": "", "mailto": "" },
  "core":              { "enabled": true, "api_key": "" },
  "scopus":            { "enabled": true, "api_key": "" },
  "semantic_scholar":  { "enabled": true, "api_key": "",
                         "recommendations": false, "recommendations_seeds": 3 },
  "unpaywall":         { "enabled": true, "email": "" },
  "crossref":          { "enabled": true, "mailto": "" },
  "arxiv":             { "enabled": true },
  "zenodo":            { "enabled": true, "access_token": "" },
  "doaj":              { "enabled": true, "api_key": "" }
}
```

Equivalently:

```bash
export SEMANTIC_SCHOLAR_API_KEY="..."
export CORE_API_KEY="..."
export SCOPUS_API_KEY="..."
export OPENALEX_MAILTO="you@example.com"
export UNPAYWALL_EMAIL="you@example.com"
export CROSSREF_MAILTO="you@example.com"
export ZENODO_ACCESS_TOKEN="..."
export DOAJ_API_KEY="..."
```

`semantic_scholar.recommendations: true` enables the Layer-1b recommendation
expansion; `recommendations_seeds` (default 3) sets how many seed papers it uses.

`config/hallucinations.json` (git-ignored; copy from
`config/hallucinations.example.json`) is an optional blocklist of
known-hallucinated records. Each `[surname, year]` pair blocks a Crossref record
whose publication year matches and whose authors or title contain the surname.
Empty by default.

`.mcp.json` — Zotero MCP integration (optional). Enables corpus archiving into a
local Zotero library via the `zotero-mcp` server.

## Outputs

### After `run_search.py`

`run_dir/corpus.json`:
```json
{
  "papers": [
    {
      "paper_id": "src-001",
      "title": "...",
      "authors": ["Family, I.", "..."],
      "year": 20YY,
      "doi": "10....",
      "source": "Journal Name",
      "abstract": "...",
      "full_text_url": "...",
      "volume": "12", "issue": "3", "pages": "45-67",
      "citation_count": 42,
      "is_oa": true,
      "relevance_score": 0.7,
      "methodology": "qualitative | quantitative | mixed | review | unknown",
      "key_findings": "First 3 sentences of abstract",
      "search_source": "openalex | semantic_scholar | core | scopus | zenodo | doaj | arxiv | crossref_free | europe_pmc | s2_recommendations"
    }
  ],
  "search_metadata": { "...": "per-layer counts, dedup totals, date, keywords, warnings" }
}
```

### After `run_check.py`

`run_dir/references.bib` — clean BibTeX with collision-resistant citation keys:
```bibtex
@article{smith_20YY,
  author = {Smith, J. and Doe, A.},
  title   = {...},
  journal = {...},
  year    = {20YY},
  volume  = {12},
  number  = {3},
  pages   = {45-67},
  doi     = {10....}
}
```

`run_dir/citation_keys.json` — verification + completeness report:
```json
{
  "paper_id_to_citekey": { "src-001": "smith_20YY", "...": "..." },
  "verification_status": { "src-001": "verified", "src-042": "unverified" },
  "unverified_paper_ids": ["src-042"],
  "metadata_incomplete_paper_ids": ["src-007"]
}
```

`run_check.py` also prints a summary report:
- required-field ratio
- DOI resolvable ratio
- Crossref confirmation ratio
- unverified ratio (warning if > 20%)
- volume coverage ratio (warning if < 70%)

## Hard Constraints

| Constraint | Rule |
|---|---|
| **Provenance** | Every BibTeX entry must trace back to a real, resolvable DOI or Crossref record. Unverified entries are flagged, not silently kept. |
| **Hallucination guard** | Known hallucinated records (configurable in `config/hallucinations.json`) are blocklisted and rejected. |
| **Citation key uniqueness** | Collision-resistant keys: `<first_author><year>` → add initials → add title hash. Never produces duplicate keys. |
| **Conservative rate limits** | Inter-query delays per source, exponential backoff with jitter on 429 (Semantic Scholar retries up to 6 times). Designed for unattended execution. |
| **Missing keys graceful** | APIs without keys are skipped — never crash the run. |
| **Failure visibility** | Every layer logs non-200 responses; layers that return 0 papers are collected into a `[WARNINGS]` block and `search_metadata.warnings` in corpus.json — a dead key is never silent. |
| **Revoked-key fallback** | A Semantic Scholar key that answers 403 (revoked/expired) is dropped mid-run and the keyless public pool is used instead; a warning tells you to renew the key. |
| **Atomic writes** | All outputs written via `.tmp` → `os.replace()` to survive interruption. |

## Execution Environment

```bash
# Optional but faster: install requests for connection pooling
pip install requests
# Otherwise falls back to stdlib urllib transparently

PYTHON = sys.executable        # any Python 3.9+
CONFIG  = config/api_keys.json  # read at startup (optional — env vars also work)
```

No virtualenv required. The bundled `lib/http_client.py` works with stdlib only; `requests` is used opportunistically if installed.

## API Coverage (Layer Sources)

| Layer | Source | Auth | Rate Limit | Role |
|---|---|---|---|---|
| 1 | Semantic Scholar | API key (`x-api-key`) | 1 req/s | High-quality abstracts |
| 1b | S2 Recommendations | API key (`x-api-key`) | 1 req/s | Recommended-papers expansion (`forpaper`; note: the old `/papers/forlist` route is retired — the client uses `POST /papers/`) |
| 2 | OpenAlex | API key (Bearer) | polite pool | Primary metadata backbone |
| 3 | CORE | API key (Bearer) | 10 req/10s | Open access + full-text URLs |
| 4 | Scopus | API key (`X-ELS-APIKey`) | 5k/week | Premium academic quality |
| 5a | Zenodo | optional token (tool UA required — Zenodo blocks browser UAs) | — | Cross-disciplinary OA |
| 5b | DOAJ | optional key | 100 req/min | OA journal directory |
| 5c | arXiv | none | — | Preprints |
| 5d | Crossref (free) | mailto for polite pool | 50 req/5min | Metadata + citation counts |
| 5e | Europe PMC | none | 1 req/s | Biomed OA full text |
| 6a | Unpaywall | email param | 100k/day | OA PDF URL resolution |
| 6b | Crossref | mailto | 50 req/5min | DOI → full biblio metadata |
| S2 datasets | `datasets/v1` (`/release/`, `/release/{id}/dataset/{name}`, `/diffs/...`) | API key | 1 req/s | S2AG bulk snapshot manifests (`run_s2.py datasets`) |

## File Layout

```
VeriRefer/
├── SKILL.md                      # this file
├── README.md                     # usage guide with examples
├── .mcp.json.example             # Zotero MCP config template
├── config/
│   ├── api_keys.example.json     # API-key template (copy to api_keys.json)
│   └── hallucinations.example.json  # blocklist template (copy to hallucinations.json)
├── lib/
│   ├── http_client.py            # stdlib-first HTTP client (requests fallback, GET + POST)
│   ├── s2.py                     # Semantic Scholar client: graph + recommendations + datasets
│   ├── search.py                 # 6-layer search + S2 rec expansion + dedup + scoring [Feature 1]
│   └── reference_checker.py      # DOI validation + biblio completion + BibTeX [Features 2+3]
├── examples/
│   ├── README.md                 # Zotero import walkthrough
│   ├── corpus_to_csl.py          # corpus.json -> CSL-JSON converter
│   └── sample_corpus.json        # runnable fixture, no API keys required
├── run_search.py                 # CLI: literature search -> corpus.json
├── run_check.py                  # CLI: DOI check + reference completion -> references.bib
└── run_s2.py                     # CLI: S2 paper/author/recommendations/datasets [Feature 4]
```

## When to Use Which Step

| User says | Run |
|---|---|
| "find papers on X" | `run_search.py` only |
| "check these DOIs are real" | `run_check.py` only (needs existing corpus.json) |
| "complete the reference info" | `run_check.py` only |
| "build me a reference list on X" | `run_search.py` then `run_check.py` |
| "generate a .bib from my corpus" | `run_check.py` only |
| "papers similar to X" | `run_s2.py recommend --id <paper>` |
| "who is author Y / what did they publish" | `run_s2.py author ...` |
| "what bulk datasets does S2 offer" | `run_s2.py datasets ...` |