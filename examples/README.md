# Example — from literature search to your Zotero library

This walkthrough runs the full VeriRef pipeline and imports the verified
results into a local Zotero library using
[`zotero-cli`](https://github.com/54yyyu/zotero-mcp).

```
run_search.py          run_check.py            corpus_to_csl.py         zotero-cli
     │                      │                        │                      │
 keywords ──▶ corpus.json ──▶ references.bib ──▶ zotero_import.json ──▶ Zotero library
                              citation_keys.json    (CSL-JSON)
```

VeriRef speaks `corpus.json`; Zotero speaks CSL-JSON. `corpus_to_csl.py`
is the adapter between them — and, importantly, the place where unverified
records get dropped.

---

## Prerequisites

**1. Zotero 7+, running, with the local API enabled**

In Zotero: **Settings → Advanced → Allow other applications on this computer to
communicate with Zotero**. Zotero must stay open during the import.

**2. `zotero-cli`** (ships with the `zotero-mcp-server` package):

```bash
uv tool install zotero-mcp-server     # recommended
# or
pip install zotero-mcp-server
```

**3. Credentials for the local API**

```bash
export ZOTERO_LOCAL=true
export ZOTERO_API_KEY="your-key"        # from https://www.zotero.org/settings/keys
export ZOTERO_LIBRARY_ID="your-id"      # numeric user ID, same page
export ZOTERO_LIBRARY_TYPE=user
```

Reading a local library needs only `ZOTERO_LOCAL=true`. **Writing** — which is
what an import does — additionally needs the API key and library ID.

Verify the connection before importing anything:

```bash
zotero-cli library list
```

---

## Quick try — no API keys needed

`sample_corpus.json` is a five-record fixture in VeriRef's corpus format,
so you can exercise the conversion without running a real search:

```bash
mkdir -p /tmp/rc-demo
cp examples/sample_corpus.json /tmp/rc-demo/corpus.json

python examples/corpus_to_csl.py --run-dir /tmp/rc-demo
```

```
[OK] Wrote 5 CSL-JSON items -> /tmp/rc-demo/zotero_import.json
```

Inspect `/tmp/rc-demo/zotero_import.json` — that array is exactly what Zotero
ingests.

---

## The full pipeline

### Step 1 — Search

```bash
python run_search.py --run-dir ./output \
  --keywords "keyword1" "keyword2" "keyword3" \
  --time-range 20YY-20YY \
  --top-n 50
```

Writes `output/corpus.json`. Layers without credentials are skipped, so this
works even with an empty config — you simply get fewer sources.

### Step 2 — Verify DOIs and complete metadata

```bash
python run_check.py --run-dir ./output
```

Writes `output/references.bib` and `output/citation_keys.json`. The latter
records, per paper, whether the DOI actually resolved and whether Crossref
confirmed the record — this is what makes the next step trustworthy.

### Step 3 — Convert to CSL-JSON

```bash
python examples/corpus_to_csl.py --run-dir ./output --verified-only
```

```
[OK] Wrote 43 CSL-JSON items -> ./output/zotero_import.json
     skipped 7 unverified (DOI unresolvable, no Crossref match)
```

`--verified-only` is the point of the exercise: papers whose DOI did not
resolve **and** which Crossref could not confirm never reach your library.
If you are importing results that originated from an LLM-assisted search, run
it with this flag.

| Flag | Effect |
|---|---|
| `--verified-only` | Drop papers marked `unverified` in `citation_keys.json` |
| `--require-doi` | Drop papers with no DOI at all |
| `--limit N` | Emit at most N items — use for a trial import |
| `--out PATH` | Override the output path |

### Step 4 — Import into Zotero

Start small. Import two items, confirm they look right, then do the rest:

```bash
python examples/corpus_to_csl.py --run-dir ./output --verified-only --limit 2 \
  --out ./output/trial.json

zotero-cli add csl-json --file ./output/trial.json \
  -c "VeriRef Trial" --create-collections --tags "veriref"
```

Check the result in Zotero, then import everything:

```bash
zotero-cli add csl-json --file ./output/zotero_import.json \
  -c "Collection-Name" --create-collections \
  --tags "veriref,20YY-review" \
  --if-exists file
```

Useful `add csl-json` flags:

| Flag | Meaning |
|---|---|
| `--file PATH` | Read CSL-JSON from a file (a top-level array is accepted) |
| `--json -` | Read CSL-JSON from stdin instead |
| `-c SPEC` | Target collection by name, key, or `Parent/Child` path (repeatable) |
| `--create-collections` | Create the collection if it does not exist |
| `--tags "a,b"` | Comma-separated tags applied to every imported item |
| `--if-exists file` | On a DOI match, reuse the existing item and add missing collections/tags instead of duplicating (this is the default) |
| `--if-exists skip` | Leave existing matches untouched |
| `--if-exists duplicate` | Always create a new item |

Piping works too, if you would rather not write an intermediate file:

```bash
python examples/corpus_to_csl.py --run-dir ./output --verified-only --out /dev/stdout \
  | zotero-cli add csl-json --json -
```

---

## What the conversion does

| corpus.json | CSL-JSON | Notes |
|---|---|---|
| `paper_id` / citekey | `id` | Prefers the citekey from `citation_keys.json`; becomes the Better BibTeX key in Zotero |
| `source_type` | `type` | `journal-article` → `article-journal`, `proceedings-article` → `paper-conference`, `posted-content` → `article`, … |
| `authors` | `author[]` | Parsed into `{family, given}`; see below |
| `year` | `issued.date-parts` | Omitted when no 4-digit year is present |
| `journal` / `container_title` | `container-title` | |
| `volume`, `issue`, `pages` | `volume`, `issue`, `page` | Empty values are omitted, not written as `""` |
| `doi` | `DOI` | URL prefix stripped — Zotero wants a bare `10.x/y` |
| `full_text_url` | `URL` | |

**Author name handling.** Corpus author strings arrive in two conventions
depending on the source API (`"Devlin, J."` from Crossref, `"Kaiming He"` from
OpenAlex). The parser handles both, plus the cases that usually break naive
splitting:

| Input | Output |
|---|---|
| `Vaswani, A.` | `{family: Vaswani, given: A.}` |
| `Kaiming He` | `{family: He, given: Kaiming}` |
| `Ludwig van Beethoven` | `{family: van Beethoven, given: Ludwig}` |
| `李明` | `{literal: 李明}` |
| `World Health Organization` | `{literal: World Health Organization}` |
| `Madonna` | `{literal: Madonna}` |

Anything that cannot be split confidently becomes a CSL `literal`, which Zotero
renders verbatim rather than inventing a surname.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Connection refused` / no library found | Zotero is not running, or the local API is off (Settings → Advanced) |
| Import reports success, nothing appears | Wrong `ZOTERO_LIBRARY_ID`, or items landed in a different collection — check `zotero-cli library list` |
| Writes rejected, reads fine | Writing needs `ZOTERO_API_KEY` + `ZOTERO_LIBRARY_ID`; `ZOTERO_LOCAL=true` alone is read-only |
| Duplicates on re-import | Default `--if-exists file` matches on DOI — items *without* a DOI cannot be matched and will duplicate. Use `--require-doi` |
| `--verified-only` drops everything | `run_check.py` has not been run, or the network blocked DOI resolution — check `citation_keys.json` |
| Dates missing in Zotero | The source record had no parseable year; `issued` is omitted by design |
