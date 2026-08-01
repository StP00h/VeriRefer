#!/usr/bin/env python3
"""
VeriRef — DOI Check + Reference Completion CLI (DOI检查 + 文献信息补全)

Thin CLI wrapper around lib.reference_checker. Takes a corpus.json (from
run_search.py or any compatible source) and:
    1. Validates every DOI by resolving it against doi.org
    2. Looks up each paper on Crossref (by DOI, then by title+author fallback)
    3. Backfills missing volume/issue/pages/authors/journal from Crossref + OpenAlex
    4. Generates a clean references.bib with unique citation keys
    5. Emits citation_keys.json with per-paper verification status + incompleteness flags

Outputs (written into the run directory):
    - references.bib
    - citation_keys.json  (verification status, unverified/metadata-incomplete IDs, warnings)

Usage:
    python run_check.py --run-dir ./output
    python run_check.py --run-dir ./output --corpus ./custom_corpus.json --out-bib ./refs.bib
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_SKILL_ROOT / "lib"))

from reference_checker import build_references_bib  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VeriRef: validate DOIs and complete reference metadata -> references.bib + citation_keys.json",
    )
    parser.add_argument("--run-dir", required=True, help="Directory containing corpus.json (outputs written here)")
    parser.add_argument("--corpus", help="Override corpus.json input path (default: <run-dir>/corpus.json)")
    parser.add_argument("--out-bib", help="Override references.bib output path")
    parser.add_argument("--out-keys", help="Override citation_keys.json output path")
    args = parser.parse_args()

    corpus_path = args.corpus or os.path.join(args.run_dir, "corpus.json")
    if not os.path.exists(corpus_path):
        print(f"[ERROR] corpus.json not found: {corpus_path}", file=sys.stderr)
        print("        Run run_search.py first, or pass --corpus <path>.", file=sys.stderr)
        return 2

    with open(corpus_path) as f:
        corpus = json.load(f)
    papers = corpus.get("papers") if isinstance(corpus, dict) else None
    if not isinstance(papers, list) or not papers:
        print(f"[ERROR] No papers found in {corpus_path}", file=sys.stderr)
        return 2

    print(f"[CHECK] Validating DOI + completing metadata for {len(papers)} papers...")

    report = build_references_bib(run_dir=args.run_dir, papers=papers)

    # Honor output path overrides if provided (the lib writes to run_dir/references.bib
    # and run_dir/citation_keys.json by default). If overridden, relocate the files.
    default_bib = os.path.join(args.run_dir, "references.bib")
    default_keys = os.path.join(args.run_dir, "citation_keys.json")
    if args.out_bib and os.path.abspath(args.out_bib) != os.path.abspath(default_bib):
        os.replace(default_bib, args.out_bib)
        report["references_bib"] = args.out_bib
    if args.out_keys and os.path.abspath(args.out_keys) != os.path.abspath(default_keys):
        os.replace(default_keys, args.out_keys)
        report["citation_keys"] = args.out_keys

    print("\n[REPORT]")
    print(f"  references.bib     : {report['references_bib']}")
    print(f"  citation_keys.json : {report['citation_keys']}")
    print(f"  entries            : {report['entry_count']}")
    print(f"  required fields ok : {report['required_field_ratio']:.1%}")
    print(f"  DOI resolvable     : {report['doi_resolvable_ratio']:.1%}")
    print(f"  Crossref confirmed : {report['crossref_confirm_ratio']:.1%}")
    print(f"  unverified         : {report['unverified_ratio']:.1%} ({len(report['unverified_paper_ids'])} papers)")
    print(f"  volume coverage    : {report['vip_coverage_ratio']:.1%}")
    if report["warnings"]:
        print("  WARNINGS:")
        for w in report["warnings"]:
            print(f"    - {w}")
    print("\n[DONE] DOI check + reference completion complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
