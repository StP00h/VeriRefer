from __future__ import annotations

import hashlib
import json
import html
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib import error, parse, request


_CROSSREF_API = "https://api.crossref.org/works/"
_OPENALEX_API = "https://api.openalex.org/works/doi:"
_DOI_RESOLVE_URL = "https://doi.org/"
logger = logging.getLogger(__name__)


def _load_known_hallucinations() -> set[tuple[str, str]]:
    """Load (surname, year) blocklist entries from config/hallucinations.json.

    The blocklist is a generic guard against known-hallucinated records; the
    entries themselves are site-specific, so the shipped default is empty.
    File format: {"entries": [["surname", "20YY"], ...]} — each pair blocks
    a Crossref record whose year matches and whose authors/title contain the
    surname. A missing or unparseable file simply means "no entries".
    """
    path = Path(__file__).resolve().parent.parent / "config" / "hallucinations.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    entries = data.get("entries") if isinstance(data, dict) else data
    blocked: set[tuple[str, str]] = set()
    if isinstance(entries, list):
        for entry in entries:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and str(entry[0]).strip()
                and str(entry[1]).strip()
            ):
                blocked.add((str(entry[0]).strip().lower(), str(entry[1]).strip()))
    return blocked


_KNOWN_HALLUCINATIONS = _load_known_hallucinations()

# Crossref and OpenAlex grant faster, more reliable service to clients that
# identify themselves ("polite pool"). Set VERIREF_MAILTO to your email to
# opt in; otherwise we send an anonymous but still well-formed User-Agent.
_CONTACT_EMAIL = os.environ.get("VERIREF_MAILTO", "").strip()
_USER_AGENT = (
    f"VeriRef/1.0 (mailto:{_CONTACT_EMAIL})"
    if _CONTACT_EMAIL
    else "VeriRef/1.0"
)


def _api_headers() -> dict[str, str]:
    return {"User-Agent": _USER_AGENT}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _normalize_doi(value: Any) -> str:
    raw = _clean_text(value).lower().strip()
    raw = raw.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    raw = raw.removeprefix("doi:")
    return raw.strip(" /")


def _given_to_initials(given: str) -> str:
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", _clean_text(given))
    if not tokens:
        return ""
    return " ".join(f"{token[0].upper()}." for token in tokens if token)


def _is_initial_like(token: str) -> bool:
    stripped = token.strip()
    if "." in stripped:
        cleaned = stripped.rstrip(".")
        return len(cleaned) <= 2 and cleaned.isalpha()
    return len(stripped) == 1 and stripped.isalpha()


def _has_non_ascii(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def _canonicalize_author_name(name: str) -> str:
    cleaned = unicodedata.normalize("NFC", _clean_text(name))
    if not cleaned:
        return ""
    # Strip parenthesized CJK annotations from author names
    # (e.g. "Su (苏嘉红)" → "Su") so pandoc renders clean citations.
    # Without this, CJK parens break regex-based citation extraction in validators.
    cleaned = re.sub(
        r"\s*\([\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]+\)",
        "",
        cleaned,
    ).strip()
    if not cleaned:
        return unicodedata.normalize("NFC", _clean_text(name))
    if "," in cleaned:
        head, tail = cleaned.split(",", 1)
        head = _clean_text(head)
        tail = _clean_text(tail)
        if head and tail:
            return f"{head}, {tail}"
        return head or tail

    tokens = [tok for tok in cleaned.split() if tok]
    if len(tokens) < 2 or len(tokens) > 3:
        return cleaned
    if _has_non_ascii(cleaned):
        return cleaned
    particles = {"de", "del", "da", "di", "van", "von", "der", "den", "bin", "al"}
    lowered = {token.lower().strip(".") for token in tokens}
    if lowered & particles:
        return cleaned

    if _is_initial_like(tokens[-1]):
        family = tokens[0]
        given_tokens = tokens[1:]
    else:
        family = tokens[-1]
        given_tokens = tokens[:-1]
    initials: list[str] = []
    for token in given_tokens:
        normalized = token.strip()
        if not normalized:
            continue
        if len(normalized) == 2 and normalized.endswith("."):
            initials.append(normalized[0].upper() + ".")
            continue
        initials.append(normalized[0].upper() + ".")
    if not initials:
        return cleaned
    return f"{family}, {' '.join(initials)}"


def _normalize_authors(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            family = _clean_text(item.get("family"))
            given = _clean_text(item.get("given"))
            name = _clean_text(item.get("name"))
            if family and given:
                initials = _given_to_initials(given)
                formatted = f"{family}, {initials}" if initials else f"{family}, {given}"
                names.append(_canonicalize_author_name(formatted))
            elif family:
                names.append(_canonicalize_author_name(family))
            elif name:
                names.append(_canonicalize_author_name(name))
            continue
        text = _clean_text(item)
        if text:
            names.append(_canonicalize_author_name(text))
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized = unicodedata.normalize("NFC", _clean_text(name))
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _paper_year(paper: dict[str, Any]) -> str:
    year = paper.get("year")
    if isinstance(year, int):
        return str(year)
    match = re.search(r"(19|20)\d{2}", _clean_text(year))
    return match.group(0) if match else "n.d."


def _title_case_if_all_caps(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return text
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    return text.title() if upper_ratio >= 0.9 else text


def _slug_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "", value.lower())
    return token or hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]")


def _first_author_token(authors: list[str]) -> str:
    if not authors:
        return "unknown"
    head = authors[0]
    if "," in head:
        head = head.split(",", 1)[0]
    if _CJK_RE.search(head):
        first_token = head.split()[0] if head.split() else head[:2]
        return _slug_token(first_token)
    parts = [p for p in re.split(r"\s+", head) if p]
    for part in reversed(parts):
        if re.search(r"[A-Za-z]", part) and not _is_initial_like(part):
            return _slug_token(part)
    for part in parts:
        if re.search(r"[A-Za-z]", part):
            return _slug_token(part)
    return _slug_token(parts[-1] if parts else head)


def _first_author_initial_token(authors: list[str]) -> str:
    if not authors:
        return "x"
    head = _clean_text(authors[0])
    if not head:
        return "x"
    if "," in head:
        _, tail = head.split(",", 1)
        match = re.search(r"[A-Za-z]", tail)
        if match:
            return _slug_token(match.group(0))
    parts = [part for part in re.split(r"\s+", head) if part]
    if len(parts) >= 2:
        match = re.search(r"[A-Za-z]", parts[0])
        if match:
            return _slug_token(match.group(0))
    match = re.search(r"[A-Za-z]", head)
    if match:
        return _slug_token(match.group(0))
    return "x"


def _title_hash_token(title: str) -> str:
    digest = hashlib.sha1(_clean_text(title).lower().encode("utf-8")).hexdigest()[:4]
    return f"title{digest}"


def _claim_unique_citekey(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    idx = 2
    while f"{base}_{idx}" in used:
        idx += 1
    key = f"{base}_{idx}"
    used.add(key)
    return key


def _build_unique_citekeys(paper_records: list[dict[str, Any]]) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in paper_records:
        base = f"{_first_author_token(record['authors'])}{_slug_token(record['year'])}"
        grouped.setdefault(base, []).append(record)

    used: set[str] = set()
    key_by_paper_id: dict[str, str] = {}
    for base in sorted(grouped.keys()):
        records = sorted(grouped[base], key=lambda item: str(item.get("paper_id", "")))
        if len(records) == 1:
            only = records[0]
            key_by_paper_id[only["paper_id"]] = _claim_unique_citekey(base, used)
            continue

        resolved_keys: list[str] = []
        by_initial: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            initial = _first_author_initial_token(record["authors"])
            candidate = (
                f"{_first_author_token(record['authors'])}_{initial}"
                f"{_slug_token(record['year'])}"
            )
            by_initial.setdefault(candidate, []).append(record)

        for candidate in sorted(by_initial.keys()):
            candidate_records = sorted(
                by_initial[candidate], key=lambda item: str(item.get("paper_id", ""))
            )
            if len(candidate_records) == 1:
                only = candidate_records[0]
                unique = _claim_unique_citekey(candidate, used)
                key_by_paper_id[only["paper_id"]] = unique
                resolved_keys.append(unique)
                continue

            for record in candidate_records:
                titled = f"{candidate}_{_title_hash_token(str(record.get('title') or ''))}"
                unique = _claim_unique_citekey(titled, used)
                key_by_paper_id[record["paper_id"]] = unique
                resolved_keys.append(unique)

        logger.warning(
            "citekey collision resolved: %s -> %s",
            base,
            ", ".join(resolved_keys),
        )
    return key_by_paper_id


def _crossref_lookup(doi: str, timeout_sec: int = 8) -> dict[str, Any]:
    if not doi:
        return {}
    url = _CROSSREF_API + parse.quote(doi, safe="")
    req = request.Request(url, headers=_api_headers())
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    message = payload.get("message")
    return message if isinstance(message, dict) else {}


def _crossref_title_author_lookup(
    title: str, authors: list[str], timeout_sec: int = 8
) -> dict[str, Any]:
    title_norm = _clean_text(title)
    if not title_norm:
        return {}
    author_hint = ""
    if authors:
        head = _clean_text(authors[0])
        author_hint = head.split(",", 1)[0].strip()
    params = {"query.title": title_norm, "rows": "1"}
    if author_hint:
        params["query.author"] = author_hint
    url = "https://api.crossref.org/works?" + parse.urlencode(params)
    req = request.Request(url, headers=_api_headers())
    try:
        time.sleep(0.34)
        with request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    message = payload.get("message")
    if not isinstance(message, dict):
        return {}
    items = message.get("items")
    if not isinstance(items, list) or not items:
        return {}
    first = items[0]
    return first if isinstance(first, dict) else {}


def validate_doi(doi: str, timeout_sec: int = 6) -> bool:
    doi_norm = _normalize_doi(doi)
    if not doi_norm:
        return False
    url = _DOI_RESOLVE_URL + parse.quote(doi_norm, safe="/")
    req = request.Request(url, method="HEAD", headers=_api_headers())
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            status = getattr(resp, "status", None)
            return status in (200, 301, 302, 303)
    except error.HTTPError as exc:
        return exc.code in (200, 301, 302, 303)
    except (error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def _openalex_lookup(doi: str, timeout_sec: int = 8) -> dict[str, Any]:
    doi_norm = _normalize_doi(doi)
    if not doi_norm:
        return {}
    url = _OPENALEX_API + parse.quote(doi_norm, safe="/")
    req = request.Request(url, headers=_api_headers())
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _verify_paper_authenticity(
    paper: dict[str, Any], doi: str, crossref: dict[str, Any], timeout_sec: int = 8
) -> bool:
    if doi and validate_doi(doi, timeout_sec=timeout_sec):
        return True
    if crossref:
        return True
    title = _clean_text(paper.get("title"))
    authors = _normalize_authors(paper.get("authors"))
    return bool(_crossref_title_author_lookup(title, authors, timeout_sec=timeout_sec))


def _pick_year(metadata: dict[str, Any], fallback: str) -> str:
    for key in ("published-print", "published-online", "issued", "created"):
        block = metadata.get(key)
        if not isinstance(block, dict):
            continue
        parts = block.get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if isinstance(first, list) and first:
            year = str(first[0])
            if re.fullmatch(r"(19|20)\d{2}", year):
                return year
    return fallback


def _crossref_record_blocklisted(metadata: dict[str, Any]) -> bool:
    if not isinstance(metadata, dict):
        return False
    year = _pick_year(metadata, "")
    authors = _normalize_authors(metadata.get("author"))
    title_list = metadata.get("title")
    title = (
        _clean_text(title_list[0])
        if isinstance(title_list, list) and title_list
        else _clean_text(metadata.get("title"))
    )
    lowered_authors = " ".join(authors).lower()
    lowered_title = title.lower()
    for surname, blocked_year in _KNOWN_HALLUCINATIONS:
        if year == blocked_year and (
            surname in lowered_authors or surname in lowered_title
        ):
            return True
    return False


def _pick_title(metadata: dict[str, Any], fallback: str) -> str:
    title = metadata.get("title")
    if isinstance(title, list) and title:
        return _title_case_if_all_caps(_clean_text(title[0])) or fallback
    return _title_case_if_all_caps(fallback)


def _pick_journal(metadata: dict[str, Any], fallback: str) -> str:
    container = metadata.get("container-title")
    if isinstance(container, list) and container:
        return _clean_text(container[0]) or fallback
    return fallback


def _bib_escape(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("&amp;", "&")
    escaped = value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    escaped = escaped.replace("&", r"\&").replace("#", r"\#")
    escaped = escaped.replace("~", r"\~").replace("%", r"\%").replace("_", r"\_")
    return escaped


def _bib_entry(citekey: str, fields: dict[str, str], entry_type: str = "article") -> str:
    ordered = [
        ("author", fields.get("author", "")),
        ("title", fields.get("title", "")),
        ("journal", fields.get("journal", "")),
        ("year", fields.get("year", "")),
        ("volume", fields.get("volume", "")),
        ("number", fields.get("number", "")),
        ("pages", fields.get("pages", "")),
        ("doi", fields.get("doi", "")),
    ]
    lines = [f"@{entry_type}{{{citekey},"]
    for key, value in ordered:
        if not value:
            continue
        lines.append(f"  {key} = {{{_bib_escape(value)}}},")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def build_references_bib(
    run_dir: str,
    papers: list[dict[str, Any]],
) -> dict[str, Any]:
    entries: list[str] = []
    required_ok = 0
    vip_coverage_hits = 0
    title_case_hits = 0
    verified_by_crossref = 0
    resolvable_doi = 0
    doi_total = 0
    verification_status: dict[str, str] = {}
    metadata_incomplete_ids: list[str] = []
    article_total = 0
    article_with_volume = 0
    paper_records: list[dict[str, Any]] = []

    for paper in papers:
        paper_id = _clean_text(paper.get("paper_id"))
        if not paper_id:
            continue
        doi = _normalize_doi(paper.get("doi"))
        crossref = _crossref_lookup(doi)
        if not crossref:
            crossref = _crossref_title_author_lookup(
                _clean_text(paper.get("title")),
                _normalize_authors(paper.get("authors")),
            )
        if not isinstance(crossref, dict):
            crossref = {}
        if _crossref_record_blocklisted(crossref):
            crossref = {}
        if crossref:
            verified_by_crossref += 1
        if doi:
            doi_total += 1
            if validate_doi(doi):
                resolvable_doi += 1
        # Prefer corpus authors when they are cleaner (Latin-only) than crossref
        _corpus_authors = _normalize_authors(paper.get("authors"))
        _crossref_authors = _normalize_authors(crossref.get("author"))
        if _corpus_authors and all(
            re.match(r'^[A-Za-z,\s.]+$', a.strip()) for a in _corpus_authors
        ):
            authors = _corpus_authors
        else:
            authors = _crossref_authors or _corpus_authors
        title = _pick_title(crossref, _clean_text(paper.get("title") or "Untitled study"))
        journal = _pick_journal(crossref, _clean_text(paper.get("source") or paper.get("venue") or "Scholarly source"))
        year = _pick_year(crossref, _paper_year(paper))
        volume = _clean_text(crossref.get("volume"))
        number = _clean_text(crossref.get("issue"))
        pages = _clean_text(crossref.get("page"))
        if doi and (not volume or not number or not pages):
            openalex = _openalex_lookup(doi)
            biblio = openalex.get("biblio") if isinstance(openalex, dict) else {}
            if isinstance(biblio, dict):
                if not volume:
                    volume = _clean_text(biblio.get("volume"))
                if not number:
                    number = _clean_text(biblio.get("issue"))
                if not pages:
                    first_page = _clean_text(biblio.get("first_page"))
                    last_page = _clean_text(biblio.get("last_page"))
                    if first_page and last_page:
                        pages = f"{first_page}-{last_page}"
                    elif first_page:
                        pages = first_page

        verified = _verify_paper_authenticity(paper, doi, crossref)
        verification_status[paper_id] = "verified" if verified else "unverified"

        fields = {
            "author": " and ".join(authors),
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "number": number,
            "pages": pages,
            "doi": doi,
        }
        if fields["author"] and fields["title"] and fields["journal"] and fields["year"] != "n.d.":
            required_ok += 1
        if volume:
            vip_coverage_hits += 1
        elif doi:
            metadata_incomplete_ids.append(paper_id)
        if title and title != title.upper():
            title_case_hits += 1
        if fields["journal"] and fields["year"] != "n.d.":
            article_total += 1
            if volume:
                article_with_volume += 1
        # Detect conference papers by container-title keywords
        _container = str(fields.get("journal", "") or "").lower()
        _is_conference = any(kw in _container for kw in ("conference", "proceedings", "symposium", "workshop", "congress"))
        paper_records.append(
            {
                "paper_id": paper_id,
                "authors": authors,
                "title": title,
                "year": year,
                "fields": fields,
                "entry_type": "inproceedings" if _is_conference else ("article" if volume else "misc"),
            }
        )

    key_by_paper_id = _build_unique_citekeys(paper_records)
    for record in paper_records:
        paper_id = str(record["paper_id"])
        citekey = key_by_paper_id.get(paper_id)
        if not citekey:
            continue
        entries.append(
            _bib_entry(
                citekey,
                record["fields"],
                entry_type=str(record.get("entry_type") or "misc"),
            )
        )

    bib_path = Path(run_dir) / "references.bib"
    _atomic_write_text(
        bib_path, "\n\n".join(entries).strip() + ("\n" if entries else "")
    )

    unverified_ids = sorted(
        [paper_id for paper_id, status in verification_status.items() if status == "unverified"]
    )
    key_map_path = Path(run_dir) / "citation_keys.json"
    _atomic_write_text(
        key_map_path,
        json.dumps(
            {
                "paper_id_to_citekey": key_by_paper_id,
                "verification_status": verification_status,
                "unverified_paper_ids": unverified_ids,
                "metadata_incomplete_paper_ids": sorted(set(metadata_incomplete_ids)),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )

    total = max(len(key_by_paper_id), 1)
    doi_base = max(doi_total, 1)
    crossref_ratio = round(verified_by_crossref / total, 3)
    unverified_ratio = round(len(unverified_ids) / total, 3)
    vip_base = max(total - len(set(metadata_incomplete_ids)), 1)
    if article_total > 0:
        vip_ratio = round(article_with_volume / article_total, 3)
    else:
        vip_ratio = round(vip_coverage_hits / vip_base, 3)
    warnings: list[str] = []
    if unverified_ratio > 0.20:
        warnings.append(f"unverified ratio exceeded threshold: {unverified_ratio:.1%}")
    if vip_ratio < 0.70 and vip_base > 0:
        warnings.append(f"volume coverage below threshold: {vip_ratio:.1%}")
    return {
        "references_bib": str(bib_path),
        "citation_keys": str(key_map_path),
        "paper_id_to_citekey": key_by_paper_id,
        "required_field_ratio": round(required_ok / total, 3),
        "vip_coverage_ratio": vip_ratio,
        "title_case_ratio": round(title_case_hits / total, 3),
        "doi_resolvable_ratio": round(resolvable_doi / doi_base, 3),
        "crossref_confirm_ratio": crossref_ratio,
        "unverified_ratio": unverified_ratio,
        "unverified_paper_ids": unverified_ids,
        "verification_status": verification_status,
        "warnings": warnings,
        "entry_count": len(key_by_paper_id),
    }
