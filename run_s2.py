#!/usr/bin/env python3
"""
VeriRefer — Semantic Scholar CLI (paper / author / recommendations / datasets)

Direct access to the Semantic Scholar API family on top of lib/s2.py:

    # Paper search / details / title match / citations / references
    python run_s2.py paper "attention is all you need" --limit 5
    python run_s2.py paper --id DOI:10.1038/nature12373
    python run_s2.py match "Attention Is All You Need"
    python run_s2.py paper --id DOI:10.1038/nature12373 --citations
    python run_s2.py paper --id DOI:10.1038/nature12373 --references

    # Author lookup
    python run_s2.py author "Oren Etzioni"
    python run_s2.py author --id 1741101
    python run_s2.py author --id 1741101 --papers

    # Recommendations
    python run_s2.py recommend --id DOI:10.1038/nature12373 --limit 10
    python run_s2.py recommend --positive DOI:10.1038/nature12373,arXiv:1706.03762 \
        [--negative DOI:10.1xxx/ignored] --limit 10

    # Datasets (S2AG bulk snapshots)
    python run_s2.py datasets                          # list releases
    python run_s2.py datasets --release latest         # datasets in latest release
    python run_s2.py datasets --release latest --name abstracts
    python run_s2.py datasets --name abstracts --diffs-from 2026-07-28

Every subcommand prints JSON (stdout, or a file via --out).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_SKILL_ROOT / "lib"))

from s2 import S2Error, load_s2_client  # noqa: E402

_DEFAULT_CONFIG = str(_SKILL_ROOT / "config" / "api_keys.json")


def _emit(payload: object, out: str | None) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"[OK] wrote {out}")
    else:
        print(text)


def _split_ids(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def cmd_paper(args: argparse.Namespace, client) -> None:
    if args.id:
        if args.citations:
            _emit(client.paper_citations(args.id, limit=args.limit), args.out)
        elif args.references:
            _emit(client.paper_references(args.id, limit=args.limit), args.out)
        else:
            _emit(client.paper_details(args.id), args.out)
    elif args.query:
        results = client.paper_search(args.query, limit=args.limit, year=args.year)
        _emit(results, args.out)
    else:
        raise SystemExit("paper: provide a search query or --id <paper_id>")


def cmd_match(args: argparse.Namespace, client) -> None:
    hit = client.paper_match(args.query)
    _emit(hit if hit else {"match": None, "query": args.query}, args.out)


def cmd_author(args: argparse.Namespace, client) -> None:
    if args.id:
        if args.papers:
            _emit(client.author_papers(args.id, limit=args.limit), args.out)
        else:
            _emit(client.author_details(args.id), args.out)
    elif args.query:
        _emit(client.author_search(args.query, limit=args.limit), args.out)
    else:
        raise SystemExit("author: provide a search query or --id <author_id>")


def cmd_recommend(args: argparse.Namespace, client) -> None:
    if args.id:
        recs = client.recommend_for_paper(args.id, limit=args.limit)
    elif args.positive:
        recs = client.recommend_for_list(
            _split_ids(args.positive),
            negative=_split_ids(args.negative) if args.negative else [],
            limit=args.limit,
        )
    else:
        raise SystemExit("recommend: provide --id <paper_id> or --positive id1,id2[,...]")
    payload = {
        "count": len(recs),
        "recommendedPapers": recs,
        "key_status": client.key_status(),
    }
    _emit(payload, args.out)


def cmd_datasets(args: argparse.Namespace, client) -> None:
    release = args.release or "latest"
    if args.name and args.diffs_from:
        _emit(client.dataset_diffs(args.name, args.diffs_from, end_release=release), args.out)
    elif args.name:
        _emit(client.dataset_download_links(args.name, release_id=release), args.out)
    else:
        releases = client.dataset_releases()
        if args.release:
            # explicit --release without --name: datasets contained in that release
            _emit(client.datasets_in_release(args.release), args.out)
        else:
            _emit({"count": len(releases), "latest": releases[-1] if releases else None,
                   "releases": releases}, args.out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VeriRefer: Semantic Scholar paper/author/recommendations/datasets CLI",
    )
    parser.add_argument("--config", default=_DEFAULT_CONFIG, help=f"Path to api_keys.json (default: {_DEFAULT_CONFIG})")
    parser.add_argument("--out", help="Write JSON to this file instead of stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("paper", help="Paper search / details / citations / references")
    p.add_argument("query", nargs="?", help="Search query")
    p.add_argument("--id", help="Paper id: s2 id | DOI:10.x | arXiv:xxxx | CorpusId:nnn | url")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--year", help="Year filter, e.g. 2023-2026")
    p.add_argument("--citations", action="store_true", help="List papers citing --id")
    p.add_argument("--references", action="store_true", help="List papers cited by --id")

    p = sub.add_parser("match", help="Single best title match (paper/search/match)")
    p.add_argument("query", help="Paper title")

    p = sub.add_parser("author", help="Author search / details / papers")
    p.add_argument("query", nargs="?", help="Author name search query")
    p.add_argument("--id", help="Semantic Scholar author id")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--papers", action="store_true", help="List papers by --id")

    p = sub.add_parser("recommend", help="Recommended papers (single seed or positive/negative lists)")
    p.add_argument("--id", help="Seed paper id for forpaper recommendations")
    p.add_argument("--positive", help="Comma-separated positive paper ids (forlist)")
    p.add_argument("--negative", help="Comma-separated negative paper ids (forlist)")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("datasets", help="S2AG bulk dataset releases and download links")
    p.add_argument("--release", help="Release id, or 'latest' (default: list all releases)")
    p.add_argument("--name", help="Dataset name, e.g. abstracts, papers, authors, citations, embeddings, s2orc")
    p.add_argument("--diffs-from", help="Show incremental diffs from this release id instead of full files")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = load_s2_client(args.config)
    handlers = {
        "paper": cmd_paper,
        "match": cmd_match,
        "author": cmd_author,
        "recommend": cmd_recommend,
        "datasets": cmd_datasets,
    }
    try:
        handlers[args.command](args, client)
    except S2Error as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print(f"        key status: {client.key_status()}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())