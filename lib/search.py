#!/usr/bin/env python3
"""
VeriRef — Literature Search Module (文献检索)

Implements a 6-layer search strategy across academic APIs, followed by
deduplication, relevance scoring, and metadata enrichment.

Layers:
    1. Semantic Scholar    — high-quality papers with abstracts
    2. OpenAlex            — primary metadata backbone (broad coverage)
    3. CORE                — open access supplement with full-text URLs
    4. Scopus              — premium academic quality
    5. Zenodo + DOAJ + arXiv + Crossref-free + EuropePMC — free backups
    6. Unpaywall + Crossref — DOI/OA enrichment + bibliographic completion

Output: corpus.json with deduplicated, scored, metadata-completed paper records.

This module is dependency-light: uses the bundled `http_client` (stdlib-first,
falls back to `requests` if installed). No external pipeline coupling.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Make sibling http_client importable when run as a script or imported
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from http_client import http_get  # noqa: E402
from reference_checker import _crossref_record_blocklisted  # noqa: E402

# Default config location: <skill_root>/config/api_keys.json
_DEFAULT_CONFIG_PATH = str(_THIS_DIR.parent / "config" / "api_keys.json")

BIBLIO_FIELDS = (
    "source",
    "journal",
    "container_title",
    "publisher",
    "volume",
    "issue",
    "pages",
    "article_number",
    "issn",
    "source_type",
)

# How many years back the default search window reaches. Computed at runtime
# rather than hardcoded as a literal range, so the default cannot silently
# drift out of date as the years pass.
_DEFAULT_WINDOW_YEARS = 3


def _current_year() -> int:
    return int(time.strftime("%Y"))


def default_time_range() -> str:
    """Default year range: the most recent `_DEFAULT_WINDOW_YEARS` years."""
    now = _current_year()
    return f"{now - _DEFAULT_WINDOW_YEARS}-{now}"


# ---------------------------------------------------------------------------
# Utility functions (text / metadata cleaning)
# ---------------------------------------------------------------------------

def clean_html(text: Any) -> str | None:
    """Remove HTML tags and normalize whitespace. Apply to ALL abstracts."""
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def ensure_authors(authors: Any) -> list[str]:
    """Clean and validate author list. Return empty list if none valid."""
    if not authors:
        return []
    if not isinstance(authors, list):
        return []
    cleaned = [a for a in authors if a and isinstance(a, str) and a.strip()]
    return cleaned if cleaned else []


def _clean_value(value: Any) -> str | None:
    cleaned = clean_html(value) if isinstance(value, str) else value
    if cleaned is None:
        cleaned = value
    text = str(cleaned or "").strip()
    return text if text else None


def _given_to_initials(given: str) -> str:
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", str(given or ""))
    if not tokens:
        return ""
    return " ".join(f"{token[0].upper()}." for token in tokens if token)


def _contains_comma_author_format(authors: list[str]) -> bool:
    valid = ensure_authors(authors)
    if not valid:
        return False
    comma_count = sum(1 for author in valid if "," in author)
    return comma_count >= max(1, len(valid) // 2)


def _normalize_page_span(first_page: str | None = None, last_page: str | None = None, page: str | None = None) -> str | None:
    direct_page = _clean_value(page)
    if direct_page:
        direct_page = re.sub(r"(?i)^pp?\.\s*", "", direct_page).strip()
        return direct_page.replace("–", "-").replace("—", "-")
    start = _clean_value(first_page)
    end = _clean_value(last_page)
    if start and end:
        return f"{start}-{end}"
    return start or end


def extract_openalex_authors(authorships: Any) -> list[str]:
    """Extract author display names from OpenAlex authorship records."""
    names: list[str] = []
    if not isinstance(authorships, list):
        return []
    for entry in authorships:
        if not isinstance(entry, dict):
            continue
        name = ""
        author_obj = entry.get("author")
        if isinstance(author_obj, dict):
            name = str(author_obj.get("display_name") or "").strip()
        if not name:
            name = str(entry.get("display_name") or "").strip()
        if not name:
            name = str(entry.get("raw_author_name") or "").strip()
        if name:
            names.append(name)
    return ensure_authors(names)


def merge_author_lists(primary: list[str] | None, fallback: list[str] | None) -> list[str]:
    """Prefer richer author metadata while preserving source-priority ordering."""
    primary_authors = ensure_authors(primary)
    fallback_authors = ensure_authors(fallback)
    if not primary_authors:
        return fallback_authors
    if not fallback_authors:
        return primary_authors
    primary_comma = _contains_comma_author_format(primary_authors)
    fallback_comma = _contains_comma_author_format(fallback_authors)
    if primary_comma and not fallback_comma:
        return primary_authors
    if fallback_comma and not primary_comma:
        return fallback_authors
    return primary_authors if len(primary_authors) >= len(fallback_authors) else fallback_authors


# ---------------------------------------------------------------------------
# Crossref metadata extraction + merging (shared with reference_checker)
# ---------------------------------------------------------------------------

def _crossref_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in item.get("author", []) or []:
        if not isinstance(author, dict):
            continue
        given = _clean_value(author.get("given")) or ""
        family = _clean_value(author.get("family")) or ""
        initials = _given_to_initials(given)
        if family and initials:
            full_name = f"{family}, {initials}"
        else:
            full_name = family or given
        if full_name:
            authors.append(full_name)
    return ensure_authors(authors)


def _extract_crossref_metadata(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    if _crossref_record_blocklisted(item):
        return {}
    container_titles = item.get("container-title") or []
    container_title = _clean_value(container_titles[0]) if container_titles else None
    year: int | None = None
    issued = (
        item.get("issued")
        or item.get("published-print")
        or item.get("published-online")
        or {}
    )
    if isinstance(issued, dict):
        date_parts = issued.get("date-parts") or []
        if date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            year = date_parts[0][0]
    page = _normalize_page_span(page=item.get("page"))
    article_number = _clean_value(item.get("article-number"))
    if not article_number:
        publisher_item = item.get("publisher-item")
        if isinstance(publisher_item, dict):
            article_number = _clean_value(publisher_item.get("item-number"))
    issn_values = item.get("ISSN") if isinstance(item.get("ISSN"), list) else []
    metadata = {
        "doi": _clean_value(item.get("DOI")),
        "authors": _crossref_authors(item),
        "year": year,
        "source": container_title,
        "journal": container_title,
        "container_title": container_title,
        "publisher": _clean_value(item.get("publisher")),
        "volume": _clean_value(item.get("volume")),
        "issue": _clean_value(item.get("issue")),
        "pages": page,
        "article_number": article_number,
        "issn": ", ".join(v for v in issn_values if _clean_value(v)) if issn_values else None,
        "source_type": _clean_value(item.get("type")),
    }
    return {k: v for k, v in metadata.items() if v not in (None, "", [], {})}


def _merge_bibliographic_metadata(paper: dict[str, Any], metadata: dict[str, Any]) -> None:
    if not isinstance(paper, dict) or not isinstance(metadata, dict):
        return
    for key in BIBLIO_FIELDS:
        incoming = metadata.get(key)
        if not incoming:
            continue
        if not _clean_value(paper.get(key)):
            paper[key] = incoming

    incoming_source = _clean_value(metadata.get("source"))
    existing_source = _clean_value(paper.get("source"))
    if incoming_source and (
        not existing_source
        or existing_source.lower() in {"crossref", "indexed journal source", "europe pmc"}
    ):
        paper["source"] = incoming_source

    incoming_doi = _clean_value(metadata.get("doi"))
    if incoming_doi and not _clean_value(paper.get("doi")):
        paper["doi"] = incoming_doi

    incoming_year = metadata.get("year")
    if incoming_year and not paper.get("year"):
        paper["year"] = incoming_year

    incoming_authors = metadata.get("authors")
    if incoming_authors:
        paper["authors"] = merge_author_lists(incoming_authors, paper.get("authors"))


# ---------------------------------------------------------------------------
# Search configuration
# ---------------------------------------------------------------------------

class SearchConfig:
    """Holds API keys + dynamic search boundaries.

    Credentials resolve in this order:
        1. Environment variable (highest priority — keeps secrets off disk)
        2. config/api_keys.json
        3. Empty string — the corresponding layer degrades or is skipped

    No credential is required. Layers without a usable key either fall back to
    unauthenticated access (OpenAlex, Crossref, arXiv, Zenodo, DOAJ) or are
    skipped entirely (Scopus, Unpaywall).
    """

    def __init__(self, config: dict[str, Any], time_range: str, search_keywords: list[str], key_concepts: list[str]):
        def resolve(env_var: str, section: str, field: str) -> str:
            """Environment variable wins; fall back to the config file."""
            env_value = os.environ.get(env_var, "").strip()
            if env_value:
                return env_value
            section_data = config.get(section) or {}
            if not isinstance(section_data, dict):
                return ""
            return str(section_data.get(field) or "").strip()

        self.openalex_key = resolve("OPENALEX_API_KEY", "openalex", "api_key")
        self.openalex_mailto = resolve("OPENALEX_MAILTO", "openalex", "mailto")
        self.core_key = resolve("CORE_API_KEY", "core", "api_key")
        self.scopus_key = resolve("SCOPUS_API_KEY", "scopus", "api_key")
        self.s2_key = resolve("SEMANTIC_SCHOLAR_API_KEY", "semantic_scholar", "api_key")
        self.unpaywall_email = resolve("UNPAYWALL_EMAIL", "unpaywall", "email")
        self.crossref_mailto = resolve("CROSSREF_MAILTO", "crossref", "mailto")
        self.zenodo_token = resolve("ZENODO_ACCESS_TOKEN", "zenodo", "access_token")
        self.doaj_key = resolve("DOAJ_API_KEY", "doaj", "api_key")
        self.time_range = time_range
        self.search_keywords = search_keywords
        self.key_concepts = key_concepts

    @property
    def crossref_user_agent(self) -> str:
        """Polite-pool User-Agent. Omits the mailto clause when no email is set,
        rather than sending a placeholder address to the API."""
        if self.crossref_mailto:
            return f"VeriRef/1.0 (mailto:{self.crossref_mailto})"
        return "VeriRef/1.0"


def _load_rq_boundaries(rq_path: str) -> tuple[str, list[str], list[str]]:
    """Read search_keywords / time_range / key_concepts from an rq_final.json file."""
    with open(rq_path) as f:
        rq = json.load(f)
    time_range = rq.get("search_boundaries", {}).get("time_range") or default_time_range()
    search_keywords = rq.get("search_keywords", []) or []
    key_concepts = rq.get("key_concepts", []) or []
    if not search_keywords:
        raise ValueError(
            f"{rq_path}: 'search_keywords' is empty. Provide at least one keyword."
        )
    return time_range, search_keywords, key_concepts


# ---------------------------------------------------------------------------
# Layer implementations
# ---------------------------------------------------------------------------

def _layer1_semantic_scholar(cfg: SearchConfig) -> list[dict[str, Any]]:
    print("\n[Layer 1] Semantic Scholar search...")

    def s2_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params = {
            "query": query,
            "limit": limit,
            "year": cfg.time_range,
            "fields": "title,authors,year,abstract,externalIds,venue,citationCount,publicationTypes",
        }
        headers: dict[str, str] = {}
        if cfg.s2_key:
            headers["x-api-key"] = cfg.s2_key
        for attempt in range(3):
            try:
                resp = http_get(url, params=params, headers=headers, timeout=20)
                if resp.status_code == 200:
                    return resp.json().get("data", [])
                if resp.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
                return []
            except Exception:
                return []
        return []

    papers: list[dict[str, Any]] = []
    for q in cfg.search_keywords[:3]:
        for r in s2_search(q, limit=10):
            authors = ensure_authors([a.get("name", "") for a in r.get("authors", [])])
            papers.append({
                "title": r.get("title"),
                "authors": authors,
                "year": r.get("year"),
                "abstract": clean_html(r.get("abstract")),
                "doi": r.get("externalIds", {}).get("DOI") if r.get("externalIds") else None,
                "source": r.get("venue"),
                "citation_count": r.get("citationCount", 0),
                "is_oa": False,
                "search_source": "semantic_scholar",
                "search_query_used": q,
            })
        time.sleep(1)
    print(f"  -> Found {len(papers)} papers from Semantic Scholar")
    return papers


def _layer2_openalex(cfg: SearchConfig) -> list[dict[str, Any]]:
    print("\n[Layer 2] OpenAlex search...")

    def openalex_search(query: str, per_page: int = 20, page: int = 1) -> list[dict[str, Any]]:
        url = "https://api.openalex.org/works"
        params = {
            "search": query,
            "per_page": per_page,
            "page": page,
            "sort": "cited_by_count:desc",
            "filter": f"publication_year:{cfg.time_range}",
        }
        # Only advertise a contact address when one is actually configured —
        # sending an empty mailto is worse than sending none.
        if cfg.openalex_mailto:
            params["mailto"] = cfg.openalex_mailto
        headers = {"Authorization": f"Bearer {cfg.openalex_key}"} if cfg.openalex_key else {}
        try:
            resp = http_get(url, params=params, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp.json().get("results", [])
        except Exception:
            pass
        return []

    papers: list[dict[str, Any]] = []
    for q in cfg.search_keywords[:5]:
        for page in (1, 2):
            results = openalex_search(q, per_page=20, page=page)
            if not results:
                break
            for r in results:
                abstract = None
                inv_idx = r.get("abstract_inverted_index")
                if inv_idx:
                    words: dict[int, str] = {}
                    for word, positions in inv_idx.items():
                        for pos in positions:
                            words[pos] = word
                    abstract = clean_html(" ".join(words[i] for i in sorted(words.keys())))
                authors = extract_openalex_authors(r.get("authorships", []))
                loc = r.get("primary_location") or {}
                source = loc.get("source") or {}
                biblio = r.get("biblio") or {}
                pages = _normalize_page_span(first_page=biblio.get("first_page"), last_page=biblio.get("last_page"))
                papers.append({
                    "title": r.get("title"),
                    "authors": authors,
                    "year": r.get("publication_year"),
                    "abstract": abstract,
                    "doi": r.get("doi"),
                    "source": source.get("display_name"),
                    "journal": source.get("display_name"),
                    "container_title": source.get("display_name"),
                    "volume": _clean_value(biblio.get("volume")),
                    "issue": _clean_value(biblio.get("issue")),
                    "pages": pages,
                    "publisher": _clean_value(source.get("host_organization_name")),
                    "issn": ", ".join(source.get("issn_l") or []) if isinstance(source.get("issn_l"), list) else _clean_value(source.get("issn_l")),
                    "source_type": _clean_value(r.get("type")),
                    "citation_count": r.get("cited_by_count", 0),
                    "is_oa": r.get("open_access", {}).get("is_oa", False),
                    "search_source": "openalex",
                    "search_query_used": q,
                })
            if len(results) < 20:
                break
            time.sleep(0.5)
        time.sleep(0.5)
    print(f"  -> Found {len(papers)} papers from OpenAlex")
    return papers


def _layer3_core(cfg: SearchConfig) -> list[dict[str, Any]]:
    print("\n[Layer 3] CORE search...")

    def core_search(query: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        url = "https://api.core.ac.uk/v3/search/works"
        headers = {"Authorization": f"Bearer {cfg.core_key}"} if cfg.core_key else {}
        try:
            resp = http_get(url, params={"q": query, "limit": limit, "offset": offset}, headers=headers, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("results") or data.get("hits", [])
        except Exception:
            pass
        return []

    papers: list[dict[str, Any]] = []
    for q in cfg.search_keywords[:5]:
        for offset in (0, 20):
            results = core_search(q, limit=20, offset=offset)
            if not results:
                break
            for r in results:
                authors = ensure_authors([a.get("name", "") for a in r.get("authors", [])])
                papers.append({
                    "title": r.get("title"),
                    "authors": authors,
                    "year": r.get("yearPublished"),
                    "abstract": clean_html(r.get("abstract")),
                    "doi": r.get("doi"),
                    "source": r.get("publisher"),
                    "full_text_url": r.get("downloadUrl") or r.get("sourceFullTextUrl"),
                    "citation_count": 0,
                    "is_oa": True,
                    "search_source": "core",
                    "search_query_used": q,
                })
            if len(results) < 20:
                break
            time.sleep(0.5)
        time.sleep(0.5)
    print(f"  -> Found {len(papers)} papers from CORE")
    return papers


def _layer4_scopus(cfg: SearchConfig) -> list[dict[str, Any]]:
    print("\n[Layer 4] Scopus search...")

    def scopus_search(query: str, count: int = 15) -> list[dict[str, Any]]:
        if not cfg.scopus_key:
            return []
        url = "https://api.elsevier.com/content/search/scopus"
        headers = {"Accept": "application/json", "X-ELS-APIKey": cfg.scopus_key}
        try:
            resp = http_get(url, params={"query": f"TITLE-ABS-KEY({query})", "count": count, "view": "STANDARD"}, headers=headers, timeout=20)
            if resp.status_code == 200:
                return resp.json().get("search-results", {}).get("entry", [])
        except Exception:
            pass
        return []

    # Build Scopus queries from the caller's own concepts only. Nothing is
    # injected here: whatever domain the caller is researching is the domain
    # that gets searched. The first concept acts as the anchor and is paired
    # with each of the others, which is how TITLE-ABS-KEY narrowing is meant
    # to be used.
    concepts = [str(c).strip() for c in (cfg.key_concepts or cfg.search_keywords) if str(c).strip()][:3]
    if not concepts:
        print("  -> No concepts supplied; skipping Scopus layer")
        return []
    if len(concepts) == 1:
        scopus_queries = [concepts[0]]
    else:
        scopus_queries = [f"{concepts[0]} AND {other}" for other in concepts[1:]]

    papers: list[dict[str, Any]] = []
    for q in scopus_queries:
        for r in scopus_search(q, count=15):
            authors: list[str] = []
            if "author" in r:
                for a in r["author"][:5]:
                    name = f"{a.get('given-name', '')} {a.get('surname', '')}".strip()
                    if name:
                        authors.append(name)
            authors = ensure_authors(authors)
            year_str = r.get("prism:coverDate", "0000")
            year = int(year_str[:4]) if year_str and year_str != "0000" else None
            papers.append({
                "title": r.get("dc:title"),
                "authors": authors,
                "year": year,
                "abstract": clean_html(r.get("dc:description")),
                "doi": r.get("prism:doi"),
                "source": r.get("prism:publicationName"),
                "journal": r.get("prism:publicationName"),
                "container_title": r.get("prism:publicationName"),
                "volume": _clean_value(r.get("prism:volume")),
                "issue": _clean_value(r.get("prism:issueIdentifier")),
                "pages": _normalize_page_span(page=r.get("prism:pageRange")),
                "publisher": _clean_value(r.get("dc:publisher")),
                "source_type": _clean_value(r.get("subtypeDescription") or r.get("prism:aggregationType")),
                "citation_count": int(r.get("citedby-count", 0)),
                "is_oa": False,
                "search_source": "scopus",
                "search_query_used": q,
            })
        time.sleep(1)
    print(f"  -> Found {len(papers)} papers from Scopus")
    return papers


def _layer5_free_backups(cfg: SearchConfig) -> list[dict[str, Any]]:
    print("\n[Layer 5] Zenodo + DOAJ + arXiv + Crossref-free + EuropePMC search...")

    def zenodo_search(query: str, size: int = 15, page: int = 1) -> list[dict[str, Any]]:
        url = "https://zenodo.org/api/records"
        params: dict[str, Any] = {"q": query, "size": size, "page": page, "type": "publication"}
        if cfg.zenodo_token:
            params["access_token"] = cfg.zenodo_token
        try:
            resp = http_get(url, params=params, timeout=20)
            if resp.status_code == 200:
                hits = resp.json().get("hits", {}).get("hits", [])
                out: list[dict[str, Any]] = []
                for r in hits:
                    meta = r.get("metadata", {})
                    doi = meta.get("doi")
                    if doi and not doi.startswith("10."):
                        doi = f"10.{doi.split('10.')[-1]}"
                    authors = ensure_authors([a.get("name", "") for a in meta.get("creators", [])])
                    files = r.get("files", [])
                    full_url = files[0].get("links", {}).get("self") if files else None
                    out.append({
                        "title": meta.get("title"),
                        "authors": authors,
                        "year": meta.get("publication_date", "")[:4] if meta.get("publication_date") else None,
                        "abstract": clean_html(meta.get("description")),
                        "doi": doi,
                        "source": meta.get("journal", {}).get("title") if meta.get("journal") else "Zenodo",
                        "full_text_url": full_url,
                        "citation_count": 0,
                        "is_oa": True,
                        "search_source": "zenodo",
                        "search_query_used": query,
                    })
                return out
        except Exception:
            pass
        return []

    def doaj_search(query: str, page_size: int = 10) -> list[dict[str, Any]]:
        url = f"https://doaj.org/api/search/articles/{quote(query)}"
        params: dict[str, Any] = {"pageSize": page_size, "page": 1}
        if cfg.doaj_key:
            params["api_key"] = cfg.doaj_key
        try:
            resp = http_get(url, params=params, timeout=20)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                out: list[dict[str, Any]] = []
                for r in results:
                    bib = r.get("bibjson", {})
                    authors = ensure_authors([a.get("name", "") for a in bib.get("author", [])])
                    out.append({
                        "title": bib.get("title"),
                        "authors": authors,
                        "year": bib.get("year"),
                        "abstract": clean_html(bib.get("abstract")),
                        "doi": bib.get("doi"),
                        "source": bib.get("journal", {}).get("title") if bib.get("journal") else "DOAJ",
                        "full_text_url": bib.get("link", [{}])[0].get("url") if bib.get("link") else None,
                        "citation_count": 0,
                        "is_oa": True,
                        "search_source": "doaj",
                        "search_query_used": query,
                    })
                return out
        except Exception:
            pass
        return []

    def arxiv_search(query: str, max_results: int = 12, start: int = 0) -> list[dict[str, Any]]:
        url = "http://export.arxiv.org/api/query"
        params = {
            "search_query": f"all:{query}",
            "start": start,
            "max_results": max_results,
            "sortBy": "lastUpdatedDate",
            "sortOrder": "descending",
        }
        try:
            resp = http_get(url, params=params, timeout=20)
            if resp.status_code != 200:
                return []
            root = ET.fromstring(resp.text)
            ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
            out: list[dict[str, Any]] = []
            for entry in root.findall("atom:entry", ns):
                title = clean_html(entry.findtext("atom:title", default="", namespaces=ns))
                abstract = clean_html(entry.findtext("atom:summary", default="", namespaces=ns))
                published = entry.findtext("atom:published", default="", namespaces=ns)
                year = int(published[:4]) if published and published[:4].isdigit() else None
                authors = ensure_authors([
                    a.findtext("atom:name", default="", namespaces=ns)
                    for a in entry.findall("atom:author", ns)
                ])
                doi = entry.findtext("arxiv:doi", default=None, namespaces=ns)
                arxiv_id = entry.findtext("atom:id", default="", namespaces=ns)
                pdf_link = None
                for link in entry.findall("atom:link", ns):
                    if link.attrib.get("title") == "pdf":
                        pdf_link = link.attrib.get("href")
                        break
                out.append({
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "abstract": abstract,
                    "doi": doi,
                    "source": "arXiv",
                    "full_text_url": pdf_link or arxiv_id,
                    "citation_count": 0,
                    "is_oa": True,
                    "search_source": "arxiv",
                    "search_query_used": query,
                })
            return out
        except Exception:
            return []

    def crossref_search_free(query: str, rows: int = 12) -> list[dict[str, Any]]:
        url = "https://api.crossref.org/works"
        headers = {"User-Agent": cfg.crossref_user_agent}
        try:
            resp = http_get(url, params={"query": query, "rows": rows, "sort": "published", "order": "desc"}, headers=headers, timeout=20)
            if resp.status_code != 200:
                return []
            items = resp.json().get("message", {}).get("items", [])
            out: list[dict[str, Any]] = []
            for item in items:
                title_list = item.get("title", [])
                title = clean_html(title_list[0]) if title_list else None
                authors: list[str] = []
                for a in item.get("author", []):
                    name = f"{a.get('given', '')} {a.get('family', '')}".strip()
                    if name:
                        authors.append(name)
                year = None
                issued = item.get("issued", {})
                if issued.get("date-parts") and issued["date-parts"][0]:
                    year = issued["date-parts"][0][0]
                out.append({
                    "title": title,
                    "authors": ensure_authors(authors),
                    "year": year,
                    "abstract": clean_html(item.get("abstract")),
                    "doi": item.get("DOI"),
                    "source": (item.get("container-title") or ["Crossref"])[0],
                    "journal": (item.get("container-title") or [None])[0],
                    "container_title": (item.get("container-title") or [None])[0],
                    "publisher": _clean_value(item.get("publisher")),
                    "volume": _clean_value(item.get("volume")),
                    "issue": _clean_value(item.get("issue")),
                    "pages": _normalize_page_span(page=item.get("page")),
                    "article_number": _clean_value(item.get("article-number")),
                    "issn": ", ".join(item.get("ISSN", [])) if isinstance(item.get("ISSN"), list) else _clean_value(item.get("ISSN")),
                    "source_type": _clean_value(item.get("type")),
                    "citation_count": int(item.get("is-referenced-by-count", 0) or 0),
                    "is_oa": False,
                    "search_source": "crossref_free",
                    "search_query_used": query,
                })
            return out
        except Exception:
            return []

    def europe_pmc_search(query: str, page_size: int = 25) -> list[dict[str, Any]]:
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        params = {"query": query, "format": "json", "pageSize": page_size, "resultType": "core"}
        try:
            resp = http_get(url, params=params, timeout=20)
            if resp.status_code != 200:
                return []
            results = resp.json().get("resultList", {}).get("result", [])
            out: list[dict[str, Any]] = []
            for item in results:
                title = clean_html(item.get("title"))
                if not title:
                    continue
                author_str = item.get("authorString") or ""
                if ";" in author_str:
                    authors = [a.strip() for a in author_str.split(";")]
                elif "," in author_str:
                    authors = [a.strip() for a in author_str.split(",")]
                else:
                    authors = [author_str.strip()] if author_str.strip() else []
                year = item.get("pubYear")
                try:
                    year_val = int(year) if year is not None else None
                except Exception:
                    year_val = None
                source = item.get("journalTitle") or "Europe PMC"
                full_url = None
                pmcid = item.get("pmcid")
                if pmcid:
                    full_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
                elif item.get("fullTextUrlList", {}).get("fullTextUrl"):
                    urls = item["fullTextUrlList"]["fullTextUrl"]
                    if isinstance(urls, list) and urls:
                        full_url = urls[0].get("url")
                out.append({
                    "title": title,
                    "authors": ensure_authors(authors),
                    "year": year_val,
                    "abstract": clean_html(item.get("abstractText")),
                    "doi": item.get("doi"),
                    "source": source,
                    "journal": source,
                    "container_title": source,
                    "volume": _clean_value(item.get("journalVolume")),
                    "issue": _clean_value(item.get("issue")),
                    "pages": _normalize_page_span(page=item.get("pageInfo")),
                    "source_type": _clean_value(item.get("pubType")),
                    "full_text_url": full_url,
                    "citation_count": 0,
                    "is_oa": bool(item.get("isOpenAccess", "N") == "Y"),
                    "search_source": "europe_pmc",
                    "search_query_used": query,
                })
            return out
        except Exception:
            return []

    papers: list[dict[str, Any]] = []
    # Short keyword lists still feed every layer: fall back to the first
    # keywords when the per-layer slice would otherwise come up empty.
    for q in (cfg.search_keywords[4:6] or cfg.search_keywords[:2]):
        papers.extend(zenodo_search(q, size=15))
        time.sleep(1)
    for q in (cfg.search_keywords[6:8] or cfg.search_keywords[:2]):
        papers.extend(doaj_search(q, page_size=10))
        time.sleep(1)
    arxiv_queries = cfg.search_keywords[8:10] if len(cfg.search_keywords) > 8 else cfg.search_keywords[:2]
    for q in arxiv_queries:
        papers.extend(arxiv_search(q, max_results=12, start=0))
        time.sleep(1)
    for q in cfg.search_keywords[:3]:
        papers.extend(crossref_search_free(q, rows=12))
        time.sleep(1)
    epmc_queries = cfg.search_keywords[2:4] if len(cfg.search_keywords) > 3 else cfg.search_keywords[:2]
    for q in epmc_queries:
        papers.extend(europe_pmc_search(q, page_size=20))
        time.sleep(1)

    backup_sources = {"zenodo", "doaj", "arxiv", "crossref_free", "europe_pmc"}
    count = sum(1 for p in papers if p.get("search_source") in backup_sources)
    print(f"  -> Found {count} papers from Zenodo/DOAJ/arXiv/Crossref-free/EuropePMC")
    return papers


def _layer6_enrichment(cfg: SearchConfig, all_papers: list[dict[str, Any]]) -> None:
    """Unpaywall + Crossref enrichment (in-place). Completes DOI / OA URL / biblio metadata."""
    print("\n[Layer 6] Unpaywall + Crossref enrichment...")

    def unpaywall_lookup(doi: str) -> dict[str, Any] | None:
        if not cfg.unpaywall_email:
            return None
        doi_clean = doi.lower().replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
        try:
            resp = http_get(f"https://api.unpaywall.org/v2/{doi_clean}?email={cfg.unpaywall_email}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "is_oa": data.get("is_oa", False),
                    "oa_url": data.get("best_oa_location", {}).get("url_for_pdf"),
                }
        except Exception:
            pass
        return None

    def crossref_enrich_by_doi(doi: str) -> dict[str, Any] | None:
        doi_clean = _clean_value(doi)
        if not doi_clean:
            return None
        doi_clean = doi_clean.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").replace("doi:", "").strip()
        if not doi_clean:
            return None
        url = f"https://api.crossref.org/works/{quote(doi_clean, safe='')}"
        headers = {"User-Agent": cfg.crossref_user_agent}
        try:
            resp = http_get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                item = resp.json().get("message", {})
                return _extract_crossref_metadata(item)
        except Exception:
            pass
        return None

    def crossref_enrich_by_title(title: str) -> dict[str, Any] | None:
        url = "https://api.crossref.org/works"
        headers = {"User-Agent": cfg.crossref_user_agent}
        try:
            resp = http_get(url, params={"query.title": title, "rows": 1}, headers=headers, timeout=10)
            if resp.status_code == 200:
                items = resp.json().get("message", {}).get("items", [])
                if items:
                    return _extract_crossref_metadata(items[0])
        except Exception:
            pass
        return None

    # Unpaywall: fill OA URLs for papers that have a DOI but no full_text_url
    for paper in [p for p in all_papers if p.get("doi") and not p.get("full_text_url")][:50]:
        time.sleep(0.1)
        oa = unpaywall_lookup(paper["doi"])
        if oa:
            paper["is_oa"] = oa["is_oa"]
            if oa["oa_url"]:
                paper["full_text_url"] = oa["oa_url"]

    # Crossref by DOI: backfill complete biblio metadata
    for paper in [p for p in all_papers if p.get("doi")][:80]:
        time.sleep(0.12)
        enriched = crossref_enrich_by_doi(paper.get("doi"))
        if enriched:
            _merge_bibliographic_metadata(paper, enriched)

    # Crossref by title: rescue papers missing DOI/source/authors/volume/etc.
    for paper in [
        p for p in all_papers
        if (not p.get("doi") or not p.get("source") or not p.get("authors")
            or not (p.get("volume") or p.get("issue") or p.get("pages") or p.get("article_number")))
    ][:60]:
        if not paper.get("title"):
            continue
        time.sleep(0.3)
        enriched = crossref_enrich_by_title(paper["title"])
        if enriched:
            _merge_bibliographic_metadata(paper, enriched)
    print("  -> Unpaywall/Crossref enrichment complete")


# ---------------------------------------------------------------------------
# Deduplication + scoring
# ---------------------------------------------------------------------------

_SOURCE_PRIORITY = {
    "scopus": 0,
    "openalex": 1,
    "core": 2,
    "semantic_scholar": 3,
    "crossref_free": 4,
    "europe_pmc": 5,
    "arxiv": 6,
    "zenodo": 7,
    "doaj": 8,
}


def _normalize_title(title: str) -> str:
    return re.sub(r"[^\w\s]", "", str(title).lower().strip())


def deduplicate(all_papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Source-priority deduplication with metadata backfill from lower-priority duplicates."""
    print("\n[Deduplication] Processing papers...")
    seen: dict[str, dict[str, Any]] = {}
    total_before = len(all_papers)
    for paper in all_papers:
        if not paper.get("title") or len(_normalize_title(paper["title"])) < 10:
            continue
        key = _normalize_title(paper["title"])
        if key not in seen:
            seen[key] = paper
            continue
        existing = seen[key]
        existing_priority = _SOURCE_PRIORITY.get(existing.get("search_source", ""), 99)
        new_priority = _SOURCE_PRIORITY.get(paper.get("search_source", ""), 99)
        if new_priority < existing_priority:
            merged = dict(paper)
            for field in ["abstract", "doi", "source", "full_text_url", "citation_count", "year", *BIBLIO_FIELDS]:
                if not merged.get(field) and existing.get(field):
                    merged[field] = existing[field]
            merged["authors"] = merge_author_lists(merged.get("authors"), existing.get("authors"))
            seen[key] = merged
        else:
            for field in ["abstract", "doi", "source", "full_text_url", "citation_count", "year", *BIBLIO_FIELDS]:
                if not existing.get(field) and paper.get(field):
                    existing[field] = paper[field]
            existing["authors"] = merge_author_lists(existing.get("authors"), paper.get("authors"))

    out = list(seen.values())
    print(f"  -> {len(out)} unique papers after deduplication (from {total_before})")
    return out


def score_and_finalize(papers: list[dict[str, Any]], search_keywords: list[str], top_n: int = 100) -> list[dict[str, Any]]:
    """Assign paper IDs, compute relevance scores, sort, classify methodology, derive key_findings."""
    # Recency bonus is relative to the current year, not a fixed cutoff, so the
    # heuristic stays meaningful over time.
    recent_since = _current_year() - _DEFAULT_WINDOW_YEARS

    for i, paper in enumerate(papers):
        paper["paper_id"] = f"src-{i + 1:03d}"

    for paper in papers:
        score = 0.0
        title_lower = paper.get("title", "").lower()
        abstract_lower = (paper.get("abstract") or "").lower()
        text_lower = title_lower + " " + abstract_lower
        for kw in search_keywords:
            kw_lower = kw.lower()
            if kw_lower in text_lower:
                score += 0.1
                if kw_lower in title_lower:
                    score += 0.1
        if paper.get("abstract"):
            score += 0.1
        if paper.get("citation_count", 0) > 10:
            score += 0.1
        try:
            year_val = int(paper.get("year") or 0)
            if year_val >= recent_since:
                score += 0.1
        except (ValueError, TypeError):
            pass
        paper["relevance_score"] = min(score, 1.0)

    papers.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    papers = papers[:top_n]

    for paper in papers:
        abstract = paper.get("abstract", "") or ""
        key_findings = ""
        if abstract and len(abstract) > 100:
            sentences = re.split(r"(?<=[.!?]) +", abstract)
            key_findings = " ".join(sentences[:3])
        paper["key_findings"] = key_findings

        methodology = "unknown"
        abstract_lower = abstract.lower()
        if any(x in abstract_lower for x in ["qualitative", "interview", "focus group", "ethnography", "case study", "thematic analysis", "grounded theory"]):
            methodology = "qualitative"
        elif any(x in abstract_lower for x in ["quantitative", "survey", "experimental", "randomized", "statistical"]):
            methodology = "quantitative"
        elif any(x in abstract_lower for x in ["mixed method", "mixed-method", "qualitative and quantitative"]):
            methodology = "mixed"
        elif any(x in abstract_lower for x in ["review", "systematic review", "meta-analysis", "literature"]):
            methodology = "review"
        paper["methodology"] = methodology

    return papers


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def run_search(
    run_dir: str,
    config_path: str | None = None,
    rq_path: str | None = None,
    corpus_path: str | None = None,
    keywords: list[str] | None = None,
    key_concepts: list[str] | None = None,
    time_range: str | None = None,
    top_n: int = 100,
) -> dict[str, Any]:
    """Run the full 6-layer literature search pipeline.

    Inputs (one of):
        - rq_path:  path to an rq_final.json with search_keywords/time_range/key_concepts
        - keywords: explicit keyword list (bypasses rq_path)

    Outputs:
        - Writes corpus.json into run_dir (path overridable via corpus_path)
        - Returns the corpus dict

    Args:
        run_dir:       output directory for corpus.json
        config_path:   path to api_keys.json (default: <skill>/config/api_keys.json)
        rq_path:       optional rq_final.json path for keyword/time boundaries
        corpus_path:   optional explicit corpus.json output path
        keywords:      optional explicit keyword list (used if rq_path absent)
        key_concepts:  optional concept list (defaults to keywords if absent)
        time_range:    year range string, e.g. "20YY-20YY". Defaults to the
                       last few years when omitted and no rq_path is given.
        top_n:         max papers to retain after scoring (default 100)
    """
    config_path = config_path or _DEFAULT_CONFIG_PATH
    corpus_path = corpus_path or os.path.join(run_dir, "corpus.json")

    # The config file is optional: every credential can come from the
    # environment instead, and every layer degrades gracefully without one.
    config: dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    else:
        print(
            f"[INFO] No config file at {config_path} — reading credentials from "
            "environment variables only. Unauthenticated layers still run."
        )

    # Resolve search boundaries
    if rq_path:
        if not os.path.exists(rq_path):
            raise FileNotFoundError(f"Missing rq file: {rq_path}")
        rq_time_range, rq_keywords, rq_concepts = _load_rq_boundaries(rq_path)
        time_range = rq_time_range
        search_keywords = rq_keywords
        key_concepts = rq_concepts
    else:
        if not keywords:
            raise ValueError(
                "No search input: provide either `rq_path` (rq_final.json) or `keywords` list."
            )
        search_keywords = list(keywords)
        key_concepts = key_concepts or list(search_keywords)
        # No frozen window: when the caller omits a range, use the runtime
        # default (the most recent few years) instead of a hardcoded literal.
        time_range = time_range or default_time_range()

    os.makedirs(run_dir, exist_ok=True)
    cfg = SearchConfig(config, time_range, search_keywords, key_concepts)

    print(f"Starting 6-layer academic search with time_range={time_range}")
    print(f"Keywords: {search_keywords}")

    all_papers: list[dict[str, Any]] = []
    all_papers.extend(_layer1_semantic_scholar(cfg))
    all_papers.extend(_layer2_openalex(cfg))
    all_papers.extend(_layer3_core(cfg))
    all_papers.extend(_layer4_scopus(cfg))
    all_papers.extend(_layer5_free_backups(cfg))
    _layer6_enrichment(cfg, all_papers)

    all_papers = deduplicate(all_papers)
    all_papers = score_and_finalize(all_papers, search_keywords, top_n=top_n)

    search_metadata = {
        "layer1_semantic_scholar": {"papers_found": sum(1 for p in all_papers if p["search_source"] == "semantic_scholar")},
        "layer2_openalex": {"papers_found": sum(1 for p in all_papers if p["search_source"] == "openalex")},
        "layer3_core": {"papers_found": sum(1 for p in all_papers if p["search_source"] == "core")},
        "layer4_scopus": {"papers_found": sum(1 for p in all_papers if p["search_source"] == "scopus")},
        "layer5_free_backups": {
            "zenodo": sum(1 for p in all_papers if p["search_source"] == "zenodo"),
            "doaj": sum(1 for p in all_papers if p["search_source"] == "doaj"),
            "arxiv": sum(1 for p in all_papers if p["search_source"] == "arxiv"),
            "europe_pmc": sum(1 for p in all_papers if p["search_source"] == "europe_pmc"),
            "crossref_free": sum(1 for p in all_papers if p["search_source"] == "crossref_free"),
        },
        "layer6_unpaywall_crossref": {"enriched": "complete"},
        "total_after_dedup": len(all_papers),
        "search_date": time.strftime("%Y-%m-%d"),
        "time_range": time_range,
        "search_keywords": search_keywords,
    }

    corpus = {"papers": all_papers, "search_metadata": search_metadata}
    with open(corpus_path, "w") as f:
        json.dump(corpus, f, indent=2, ensure_ascii=False)

    print(f"\n[OK] Saved corpus.json with {len(all_papers)} papers at {corpus_path}")
    return corpus


if __name__ == "__main__":
    # Minimal CLI when invoked directly: python -m lib.search --run-dir RUN --rq RQ
    import argparse

    parser = argparse.ArgumentParser(description="VeriRef literature search")
    parser.add_argument("--run-dir", required=True, help="Output directory for corpus.json")
    parser.add_argument("--config", default=_DEFAULT_CONFIG_PATH, help="Path to api_keys.json")
    parser.add_argument("--rq", help="Path to rq_final.json (provides keywords/time range)")
    parser.add_argument("--keywords", nargs="*", help="Explicit keyword list (if no --rq)")
    parser.add_argument("--time-range", default=None, help=f"Year range, e.g. 20YY-20YY (default: {default_time_range()})")
    parser.add_argument("--top-n", type=int, default=100)
    args = parser.parse_args()

    run_search(
        run_dir=args.run_dir,
        config_path=args.config,
        rq_path=args.rq,
        keywords=args.keywords,
        time_range=args.time_range,
        top_n=args.top_n,
    )
