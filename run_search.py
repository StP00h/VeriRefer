#!/usr/bin/env python3
"""
VeriRefer — Literature Search CLI (文献检索)

Thin CLI wrapper around lib.search.run_search(). Runs the 6-layer academic
search pipeline and writes corpus.json into the target run directory.

Usage:
    # From keywords directly
    python run_search.py --run-dir ./output --keywords "keyword1" "keyword2" --time-range 20YY-20YY

    # From an rq_final.json (research-question spec file)
    python run_search.py --run-dir ./output --rq ./rq_final.json

    # Override config / corpus path
    python run_search.py --run-dir ./output --keywords ... --config ./config/api_keys.json --corpus ./custom_corpus.json
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the lib/ importable when this script is run from anywhere
_SKILL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_SKILL_ROOT / "lib"))

from search import default_time_range, run_search  # noqa: E402

_DEFAULT_CONFIG = str(_SKILL_ROOT / "config" / "api_keys.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VeriRefer: run 6-layer academic literature search -> corpus.json",
    )
    parser.add_argument("--run-dir", required=True, help="Output directory (corpus.json written here by default)")
    parser.add_argument("--config", default=_DEFAULT_CONFIG, help=f"Path to api_keys.json (default: {_DEFAULT_CONFIG})")
    parser.add_argument("--corpus", help="Override corpus.json output path (default: <run-dir>/corpus.json)")
    parser.add_argument("--rq", help="Path to rq_final.json (provides keywords/time_range/key_concepts)")
    parser.add_argument("--keywords", nargs="+", help="Explicit keywords (required if --rq is omitted)")
    parser.add_argument("--concepts", nargs="+", help="Key concepts for Scopus query construction (defaults to keywords)")
    parser.add_argument("--time-range", default=None, help=f"Year range, e.g. 20YY-20YY (default: {default_time_range()}, used only without --rq)")
    parser.add_argument("--top-n", type=int, default=100, help="Max papers to keep after scoring (default 100)")
    parser.add_argument("--s2-recommend-seeds", type=int, default=None, metavar="N",
                        help="S2 recommended-papers expansion: number of seed papers (0 disables; default: config / 3). Requires a working Semantic Scholar key.")
    args = parser.parse_args()

    if not args.rq and not args.keywords:
        parser.error("Provide either --rq <path> or --keywords <kw1> <kw2> ...")

    os.makedirs(args.run_dir, exist_ok=True)

    corpus = run_search(
        run_dir=args.run_dir,
        config_path=args.config,
        rq_path=args.rq,
        corpus_path=args.corpus,
        keywords=args.keywords,
        key_concepts=args.concepts,
        time_range=args.time_range,
        top_n=args.top_n,
        s2_recommend_seeds=args.s2_recommend_seeds,
    )
    n = len(corpus.get("papers", []))
    print(f"\n[DONE] Literature search complete: {n} papers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
