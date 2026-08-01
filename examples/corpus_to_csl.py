#!/usr/bin/env python3
"""
Example: corpus.json  ->  CSL-JSON  ->  Zotero (via zotero-cli)

VeriRef emits `corpus.json` (search results) and `citation_keys.json`
(per-paper DOI verification status). Zotero does not read those formats, but it
does read CSL-JSON — the same interchange format used by Pandoc and most
reference managers. This script performs that translation.

Typical pipeline:

    python run_search.py --run-dir ./output --keywords "keyword1" "keyword2"
    python run_check.py  --run-dir ./output
    python examples/corpus_to_csl.py --run-dir ./output --verified-only
    zotero-cli add csl-json --file ./output/zotero_import.json \
        -c "Collection-Name" --create-collections --tags "veriref"

`--verified-only` is the reason this step is worth doing: it drops any paper
whose DOI did not resolve and which Crossref could not confirm, so hallucinated
or malformed records never reach your library.

Requires no third-party packages. See examples/README.md for the full walkthrough.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

# CJK ranges — names in these scripts are not "Given Family" and must not be split.
_CJK_RE = re.compile(r"[一-鿿぀-ゟ゠-ヿ가-힯]")

# Particles that belong to the family name, not the given name
# ("van der Berg" -> family "van der Berg", not family "Berg").
_PARTICLES = {"de", "del", "da", "di", "van", "von", "der", "den", "bin", "al", "la", "le"}

# Corporate authors ("World Health Organization") must not be split into
# given/family. This list is a heuristic, not exhaustive — it catches the
# institutional authors that realistically appear in academic corpora.
_ORG_KEYWORDS = {
    "university", "universite", "universidad", "institute", "institut",
    "organization", "organisation", "association", "committee", "commission",
    "department", "ministry", "council", "society", "foundation", "college",
    "center", "centre", "agency", "consortium", "laboratory", "laboratories",
    "group", "network", "board", "bureau", "office", "school", "academy",
    "hospital", "trust", "programme", "program", "initiative", "collaboration",
    "inc", "ltd", "llc", "gmbh", "corp", "corporation", "company",
}

# VeriRef `source_type` values (Crossref/OpenAlex vocabularies) -> CSL types.
_CSL_TYPE_BY_SOURCE_TYPE = {
    "journal-article": "article-journal",
    "article": "article-journal",
    "proceedings-article": "paper-conference",
    "conference-paper": "paper-conference",
    "book-chapter": "chapter",
    "chapter": "chapter",
    "book": "book",
    "monograph": "book",
    "edited-book": "book",
    "reference-entry": "entry-encyclopedia",
    "dissertation": "thesis",
    "thesis": "thesis",
    "report": "report",
    "posted-content": "article",  # preprints
    "preprint": "article",
    "dataset": "dataset",
    "peer-review": "review",
}


def _clean(value: Any) -> str:
    """Collapse whitespace and coerce to a stripped string."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_author(name: str) -> dict[str, str]:
    """Convert one author string into a CSL name object.

    VeriRef's corpus mixes two conventions depending on the source API:
    Crossref yields "Family, G. I." while OpenAlex yields "Given Family".
    Anything we cannot split confidently becomes a CSL `literal` name, which
    Zotero renders verbatim rather than mangling.
    """
    cleaned = _clean(name)
    if not cleaned:
        return {}

    # CJK names have no reliable given/family split — keep them intact.
    if _CJK_RE.search(cleaned):
        return {"literal": cleaned}

    # Corporate authors are single entities, not "Given Family".
    words = {w.lower().strip(".,") for w in cleaned.split()}
    if words & _ORG_KEYWORDS:
        return {"literal": cleaned}

    # "Family, Given" — the unambiguous case.
    if "," in cleaned:
        family, given = cleaned.split(",", 1)
        family, given = _clean(family), _clean(given)
        if family and given:
            return {"family": family, "given": given}
        return {"literal": family or given}

    tokens = [t for t in cleaned.split() if t]
    if len(tokens) < 2:
        # A single token (mononym, organisation, "Anonymous") is not a surname.
        return {"literal": cleaned}

    # Walk back over trailing particles so "Ludwig van Beethoven" keeps
    # "van Beethoven" together as the family name.
    split_at = len(tokens) - 1
    while split_at > 1 and tokens[split_at - 1].lower().strip(".") in _PARTICLES:
        split_at -= 1

    given = " ".join(tokens[:split_at])
    family = " ".join(tokens[split_at:])
    if not given or not family:
        return {"literal": cleaned}
    return {"family": family, "given": given}


def _csl_type(paper: dict[str, Any]) -> str:
    raw = _clean(paper.get("source_type")).lower()
    if raw in _CSL_TYPE_BY_SOURCE_TYPE:
        return _CSL_TYPE_BY_SOURCE_TYPE[raw]
    # Fall back on the venue name when the API gave us no explicit type.
    venue = _clean(paper.get("journal") or paper.get("container_title") or paper.get("source")).lower()
    if any(kw in venue for kw in ("conference", "proceedings", "symposium", "workshop", "congress")):
        return "paper-conference"
    if "arxiv" in venue or _clean(paper.get("search_source")).lower() == "arxiv":
        return "article"
    return "article-journal"


def _issued(paper: dict[str, Any]) -> dict[str, Any] | None:
    """Build a CSL `issued` block. Omitted entirely when there is no usable year."""
    match = re.search(r"(19|20)\d{2}", _clean(paper.get("year")))
    return {"date-parts": [[int(match.group(0))]]} if match else None


def paper_to_csl(paper: dict[str, Any], citekey: str | None = None) -> dict[str, Any]:
    """Translate one VeriRef paper record into a CSL-JSON item."""
    authors = [parse_author(a) for a in (paper.get("authors") or []) if _clean(a)]
    authors = [a for a in authors if a]

    venue = _clean(paper.get("journal") or paper.get("container_title") or paper.get("source"))

    item: dict[str, Any] = {
        # `id` becomes the Better BibTeX citation key in Zotero, so prefer the
        # collision-resistant key run_check.py already computed.
        "id": citekey or _clean(paper.get("paper_id")) or "veriref-item",
        "type": _csl_type(paper),
        "title": _clean(paper.get("title")) or "Untitled",
    }

    if authors:
        item["author"] = authors
    issued = _issued(paper)
    if issued:
        item["issued"] = issued
    if venue:
        item["container-title"] = venue

    # Optional scalar fields — emitted only when non-empty, because Zotero
    # displays empty strings as blank-but-present fields.
    for csl_key, paper_key in (
        ("volume", "volume"),
        ("issue", "issue"),
        ("page", "pages"),
        ("publisher", "publisher"),
        ("abstract", "abstract"),
        ("ISSN", "issn"),
    ):
        value = _clean(paper.get(paper_key))
        if value:
            item[csl_key] = value

    doi = _clean(paper.get("doi"))
    if doi:
        # Store a bare DOI; Zotero's DOI field expects "10.x/y", not a URL.
        item["DOI"] = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)

    url = _clean(paper.get("full_text_url"))
    if url:
        item["URL"] = url

    return item


def load_citekeys(run_dir: str, explicit_path: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Load paper_id -> citekey and paper_id -> verification status.

    Both maps come from citation_keys.json, which run_check.py writes. Missing
    file is not an error: we simply fall back to paper_id and treat every paper
    as unverified-but-included (unless --verified-only demands otherwise).
    """
    path = explicit_path or os.path.join(run_dir, "citation_keys.json")
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return (
        data.get("paper_id_to_citekey") or {},
        data.get("verification_status") or {},
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert VeriRef corpus.json into CSL-JSON for import into Zotero.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python examples/corpus_to_csl.py --run-dir ./output --verified-only\n"
            "  zotero-cli add csl-json --file ./output/zotero_import.json -c \"My Review\"\n"
        ),
    )
    parser.add_argument("--run-dir", required=True, help="Directory containing corpus.json")
    parser.add_argument("--corpus", help="Override corpus.json input path")
    parser.add_argument("--keys", help="Override citation_keys.json input path")
    parser.add_argument("--out", help="Output path (default: <run-dir>/zotero_import.json)")
    parser.add_argument(
        "--verified-only",
        action="store_true",
        help="Skip papers marked 'unverified' in citation_keys.json (unresolvable DOI and no Crossref match)",
    )
    parser.add_argument(
        "--require-doi",
        action="store_true",
        help="Skip papers with no DOI at all",
    )
    parser.add_argument("--limit", type=int, help="Emit at most N items (useful for a trial import)")
    args = parser.parse_args()

    corpus_path = args.corpus or os.path.join(args.run_dir, "corpus.json")
    out_path = args.out or os.path.join(args.run_dir, "zotero_import.json")

    if not os.path.exists(corpus_path):
        print(f"[ERROR] corpus.json not found: {corpus_path}", file=sys.stderr)
        print("        Run run_search.py first, or pass --corpus <path>.", file=sys.stderr)
        return 2

    with open(corpus_path, encoding="utf-8") as f:
        corpus = json.load(f)
    papers = corpus.get("papers") if isinstance(corpus, dict) else None
    if not isinstance(papers, list) or not papers:
        print(f"[ERROR] No papers found in {corpus_path}", file=sys.stderr)
        return 2

    citekeys, statuses = load_citekeys(args.run_dir, args.keys)
    if args.verified_only and not statuses:
        print(
            "[WARN] --verified-only was requested but no citation_keys.json was found.\n"
            "       Run run_check.py first, otherwise nothing has been verified.",
            file=sys.stderr,
        )

    items: list[dict[str, Any]] = []
    skipped_unverified = 0
    skipped_no_doi = 0

    for paper in papers:
        paper_id = _clean(paper.get("paper_id"))

        if args.verified_only and statuses.get(paper_id) == "unverified":
            skipped_unverified += 1
            continue
        if args.require_doi and not _clean(paper.get("doi")):
            skipped_no_doi += 1
            continue

        items.append(paper_to_csl(paper, citekeys.get(paper_id)))
        if args.limit and len(items) >= args.limit:
            break

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[OK] Wrote {len(items)} CSL-JSON items -> {out_path}")
    if skipped_unverified:
        print(f"     skipped {skipped_unverified} unverified (DOI unresolvable, no Crossref match)")
    if skipped_no_doi:
        print(f"     skipped {skipped_no_doi} without a DOI")
    print("\nNext step — import into Zotero (Zotero must be running):")
    print(f"     zotero-cli add csl-json --file {out_path} \\")
    print('         -c "VeriRef Import" --create-collections --tags "veriref"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
