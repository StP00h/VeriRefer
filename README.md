# VeriRefer

Academic reference toolkit: 6-layer literature search across nine scholarly APIs, DOI verification against doi.org/Crossref, metadata completion, and clean BibTeX/CSL-JSON export. Zero required dependencies, all credentials optional.

Searches nine scholarly APIs, verifies that every DOI actually resolves, backfills
missing bibliographic fields from Crossref and OpenAlex, and emits clean BibTeX
with collision-resistant citation keys.

Built for a specific problem: **LLM-assisted literature citations that do not
exist.** VeriRefer resolves every DOI against `doi.org`, cross-checks
each record against Crossref, and flags anything it cannot confirm — so
fabricated references are caught before they reach your bibliography.

```bash
python run_search.py --run-dir ./output --keywords "keyword1" "keyword2"
python run_check.py  --run-dir ./output
```

```
[REPORT]
  references.bib     : ./output/references.bib
  citation_keys.json : ./output/citation_keys.json
  entries            : 50
  required fields ok : 96.0%
  DOI resolvable     : 94.0%
  Crossref confirmed : 92.0%
  unverified         : 6.0% (3 papers)
```

---

## Install

```bash
git clone https://github.com/StP00h/VeriRefer.git
cd VeriRefer
pip install -r requirements.txt    # optional — see below
```

Python 3.9+. **No required dependencies**: `lib/http_client.py` falls back to the
standard library's `urllib` when `requests` is absent. Installing `requests` is
recommended only because connection pooling speeds up the many small API calls.

---

## Configuration

**Every credential is optional.** Layers without a key either fall back to
unauthenticated access or are skipped — the search degrades, it does not fail.
You can run VeriRefer with no configuration at all.

Credentials resolve in this order: **environment variable → config file → skip**.

### Option A — environment variables (recommended)

Keeps secrets off disk entirely:

```bash
export SEMANTIC_SCHOLAR_API_KEY="..."
export CORE_API_KEY="..."
export SCOPUS_API_KEY="..."
export OPENALEX_MAILTO="you@example.com"
export UNPAYWALL_EMAIL="you@example.com"    # required for Unpaywall OA lookup
export CROSSREF_MAILTO="you@example.com"    # joins Crossref's faster polite pool
export VERIREF_MAILTO="you@example.com" # contact address for run_check.py
```

### Option B — config file

```bash
cp config/api_keys.example.json config/api_keys.json
# then edit config/api_keys.json
```

`config/api_keys.json` is git-ignored. **Do not commit it.**

`config/hallucinations.json` is an optional, git-ignored blocklist of
known-hallucinated records — see `config/hallucinations.example.json` for the
format. It ships empty; add `[surname, year]` pairs only if a specific
fabricated record keeps slipping past DOI/Crossref verification.

### Where the keys come from

| Service | Key needed? | Register |
|---|---|---|
| OpenAlex | Yes | <https://developers.openalex.org/> |
| Crossref | Email | — |
| arXiv | No | — |
| Europe PMC | No | — |
| Semantic Scholar | Key | <https://www.semanticscholar.org/product/api> |
| Zenodo | Optional | <https://zenodo.org/account/settings/applications/> |
| DOAJ | Optional | <https://doaj.org/apply-for-api-key/> |
| CORE | Yes | <https://core.ac.uk/services/api/#form> |
| Scopus | Yes | <https://dev.elsevier.com/> |
| Unpaywall | Email only | — |

---

## What it does

### 1. Literature search — `run_search.py`

Six layers across nine APIs, then deduplication, relevance scoring, and
enrichment:

| Layer | Sources | Auth |
|---|---|---|
| 1 | Semantic Scholar | Key |
| 2 | OpenAlex | Key |
| 3 | CORE | key |
| 4 | Scopus | key |
| 5 | Zenodo · DOAJ · arXiv · Crossref · Europe PMC | mixed / none |
| 6 | Unpaywall + Crossref enrichment | email |

Results are deduplicated by normalized title with **source-priority merging** —
when the same paper appears in several databases, the highest-priority record
wins and missing fields are backfilled from the others.

Conservative rate limiting throughout (inter-request delays, exponential backoff
on HTTP 429). Designed for unattended runs.

### 2. DOI verification — `run_check.py`

For every paper:

- Resolves the DOI against `doi.org` via a HEAD request
- Looks it up on Crossref by DOI, falling back to title + author
- Rejects known-hallucinated records via a configurable blocklist
  (`config/hallucinations.json`, empty by default)
- Records a per-paper `verified` / `unverified` status

### 3. Metadata completion — `run_check.py`

- Backfills authors, journal, year, volume, issue, and pages from Crossref
- Falls back to OpenAlex `biblio` when Crossref lacks volume/issue/pages
- Canonicalizes author names (CJK parentheticals, name particles, initials)
- Generates collision-resistant citation keys:
  `surname20YY` → `surname_j20YY` → `surname_j20YY_title1a2b`
- Writes a clean `references.bib`

---

## Outputs

| File | Contents |
|---|---|
| `corpus.json` | Full search corpus — metadata, abstracts, scores, provenance |
| `references.bib` | BibTeX with unique citation keys |
| `citation_keys.json` | Per-paper verification status and completeness flags |

Quality metrics reported by `run_check.py`: `doi_resolvable_ratio`,
`crossref_confirm_ratio`, `unverified_ratio`, `vip_coverage_ratio`. Warnings are
raised when the unverified ratio exceeds 20% or volume coverage falls below 70%.

---

## Usage

### Search from keywords

```bash
python run_search.py \
  --run-dir ./output \
  --keywords "keyword1" "keyword2" "keyword3" \
             "concept1" "concept2" \
  --time-range 20YY-20YY \
  --top-n 100
```

### Search from a research-question file

```bash
python run_search.py --run-dir ./output --rq ./rq_final.json
```

```json
{
  "search_keywords": ["kw1", "kw2"],
  "search_boundaries": { "time_range": "20YY-20YY" },
  "key_concepts": ["concept1", "concept2"]
}
```

### All options

```
run_search.py
  --run-dir DIR       Output directory (required)
  --config PATH       api_keys.json path (default: config/api_keys.json)
  --corpus PATH       Override corpus.json output path
  --rq PATH           rq_final.json input (alternative to --keywords)
  --keywords KW...    Explicit keywords
  --concepts C...     Key concepts for Scopus queries (default: keywords)
  --time-range R      Year range, e.g. 20YY-20YY (ignored with --rq)
  --top-n N           Max papers after scoring (default 100)

run_check.py
  --run-dir DIR       Directory containing corpus.json (required)
  --corpus PATH       Override corpus.json input path
  --out-bib PATH      Override references.bib output path
  --out-keys PATH     Override citation_keys.json output path
```

### As a library

```python
import sys
sys.path.insert(0, "lib")

from search import run_search
from reference_checker import build_references_bib

corpus = run_search(
    run_dir="./output",
    keywords=["keyword1", "keyword2"],
    time_range="20YY-20YY",
)

report = build_references_bib(run_dir="./output", papers=corpus["papers"])
print(report["doi_resolvable_ratio"], report["unverified_paper_ids"])
```

---

## Importing into Zotero

[`examples/`](examples/) contains a complete walkthrough: convert `corpus.json`
to CSL-JSON and import it into a local Zotero library with
[`zotero-cli`](https://github.com/54yyyu/zotero-mcp), dropping unverified
records along the way.

```bash
python examples/corpus_to_csl.py --run-dir ./output --verified-only
zotero-cli add csl-json --file ./output/zotero_import.json \
  -c "Collection-Name" --create-collections --tags "verirefer"
```

A five-record sample corpus is included, so the conversion can be tried without
any API keys. See [examples/README.md](examples/README.md).

---

## Layout

```
VeriRefer/
├── run_search.py               # CLI: literature search
├── run_check.py                # CLI: DOI verification + metadata completion
├── lib/
│   ├── http_client.py          # HTTP client (stdlib, optional requests)
│   ├── search.py               # 6-layer search, dedup, scoring, enrichment
│   └── reference_checker.py    # DOI validation, Crossref/OpenAlex, BibTeX
├── config/
│   ├── api_keys.example.json       # credential template (copy to api_keys.json)
│   └── hallucinations.example.json # blocklist template (copy to hallucinations.json)
├── examples/
│   ├── README.md               # Zotero import walkthrough
│   ├── corpus_to_csl.py        # corpus.json -> CSL-JSON converter
│   └── sample_corpus.json      # runnable fixture, no API keys required
└── .mcp.json.example           # optional Zotero MCP server config
```

---

## Notes and limitations

- **Rate limits are real.** A 100-paper run makes several hundred API calls and
  takes minutes. Delays are deliberate; removing them will get you throttled.
- **Scopus and CORE require keys**, and Scopus generally requires institutional
  access. Both layers are skipped without one.
- **Verification is not proof.** A resolvable DOI plus a Crossref record means
  the paper exists — not that it says what a summary claims it says.
- **Relevance scoring is keyword-based**, deliberately simple and transparent.
  Treat `relevance_score` as a coarse sort, not a judgement of quality.

## License

MIT — see [LICENSE](LICENSE).
