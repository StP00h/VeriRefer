#!/usr/bin/env python3
"""
VeriRefer — Semantic Scholar API client (Graph + Recommendations + Datasets)

Covers the endpoints documented in the official tutorial:
    https://www.semanticscholar.org/product/api/tutorial

Graph API (api.semanticscholar.org/graph/v1):
    paper search / details / title match / citations / references
    author search / details / author papers

Recommendations API (api.semanticscholar.org/recommendations/v1):
    GET  /papers/forpaper/{paper_id}      single-paper recommendations
    POST /papers/                         list-based recommendations (positive/negative)
    NOTE: the historical "/papers/forlist" route is DEPRECATED and answers 405;
    the current route is POST /papers/ (verified against the live swagger.json).

Datasets API (api.semanticscholar.org/datasets/v1):
    GET /release/                                  list release ids
    GET /release/{release_id}                      datasets in a release
    GET /release/{release_id}/dataset/{name}       download links for a dataset
    GET /diffs/{start}/to/{end}/{dataset_name}     incremental diff download links

Client behaviour:
    - Sends `x-api-key` when configured; a 403 (revoked/expired key) drops the
      key for the rest of the run and retries keyless (public pool, throttled).
    - Enforces the documented 1 req/s limit (cumulative across endpoints) with a
      monotonic-clock spacer, plus exponential backoff with jitter on 429/5xx.
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from http_client import http_get, http_post_json  # noqa: E402

_BASE = "https://api.semanticscholar.org"
_GRAPH = f"{_BASE}/graph/v1"
_RECOMMENDATIONS = f"{_BASE}/recommendations/v1"
_DATASETS = f"{_BASE}/datasets/v1"

DEFAULT_PAPER_FIELDS = (
    "title,authors,year,abstract,externalIds,venue,citationCount,"
    "publicationTypes,isOpenAccess"
)
DEFAULT_AUTHOR_FIELDS = "name,affiliations,paperCount,citationCount,hIndex"

_MAX_ATTEMPTS = 6


class S2Error(RuntimeError):
    """Raised when an S2 endpoint cannot be reached or returns a hard error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class S2Client:
    """Thin authenticated client for the Semantic Scholar API family."""

    def __init__(self, api_key: str = "", *, min_interval: float = 1.05) -> None:
        self.api_key = (api_key or "").strip()
        self.min_interval = float(min_interval)
        self._last_request_at = 0.0
        self.key_disabled = not self.api_key
        self.key_drop_reason = ""

    # ------------------------------------------------------------------ core

    def _throttle(self) -> None:
        """Space requests at least `min_interval` apart (1 req/s documented)."""
        now = time.monotonic()
        wait = self._last_request_at + self.min_interval - now
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _headers(self) -> dict[str, str]:
        if self.api_key and not self.key_disabled:
            return {"x-api-key": self.api_key}
        return {}

    def _drop_key(self, status: int) -> None:
        self.key_disabled = True
        self.key_drop_reason = f"HTTP {status}"
        print(
            f"  !! Semantic Scholar returned {status} for the configured API key "
            "(key appears revoked or expired).\n"
            "     Falling back to the keyless public pool for this run. "
            "Renew the key at https://www.semanticscholar.org/product/api"
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        timeout: int = 30,
        retries_429: bool = True,
    ) -> Any:
        attempt = 0
        while True:
            self._throttle()
            try:
                if method == "POST":
                    resp = http_post_json(url, body=body or {}, params=params, headers=self._headers(), timeout=timeout)
                else:
                    resp = http_get(url, params=params, headers=self._headers(), timeout=timeout)
                status = resp.status_code
                if status == 200:
                    return resp.json()
                if status == 403 and self.api_key and not self.key_disabled:
                    self._drop_key(status)
                    continue  # retry keyless immediately, no attempt spent
                if retries_429 and (status == 429 or status >= 500):
                    if attempt + 1 >= _MAX_ATTEMPTS:
                        break
                    # Anonymous pool is 1 req/s shared globally; back off with
                    # jitter so concurrent clients don't retry in lockstep.
                    time.sleep(2 ** (attempt + 1) + random.uniform(0, 1.5))
                    attempt += 1
                    continue
                raise S2Error(f"Semantic Scholar HTTP {status} for {url}", status_code=status)
            except S2Error:
                raise
            except Exception as exc:
                raise S2Error(f"Semantic Scholar request failed for {url}: {exc}") from exc
        raise S2Error(
            f"Semantic Scholar exhausted retries for {url} (pool congested or upstream error)"
        )

    def key_status(self) -> str:
        if self.api_key and not self.key_disabled:
            return "key"
        if self.api_key:
            return f"keyless (key dropped: {self.key_drop_reason})"
        return "keyless (no key configured)"

    # ---------------------------------------------------------------- graph

    def paper_search(
        self,
        query: str,
        *,
        limit: int = 10,
        year: str | None = None,
        fields: str = DEFAULT_PAPER_FIELDS,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "limit": limit, "fields": fields}
        if year:
            params["year"] = year
        data = self._request("GET", f"{_GRAPH}/paper/search", params=params)
        return data.get("data", []) or []

    def paper_details(self, paper_id: str, *, fields: str = DEFAULT_PAPER_FIELDS) -> dict[str, Any]:
        return self._request("GET", f"{_GRAPH}/paper/{paper_id}", params={"fields": fields})

    def paper_match(self, title: str, *, fields: str = "title,year,externalIds") -> dict[str, Any] | None:
        """Single best title match (paper/search/match)."""
        data = self._request("GET", f"{_GRAPH}/paper/search/match", params={"query": title, "fields": fields})
        results = data.get("data", []) or []
        return results[0] if results else None

    def paper_citations(self, paper_id: str, *, limit: int = 20, fields: str = "title,year,externalIds") -> list[dict[str, Any]]:
        data = self._request("GET", f"{_GRAPH}/paper/{paper_id}/citations", params={"limit": limit, "fields": fields})
        return [row.get("citingPaper", {}) for row in (data.get("data", []) or [])]

    def paper_references(self, paper_id: str, *, limit: int = 20, fields: str = "title,year,externalIds") -> list[dict[str, Any]]:
        data = self._request("GET", f"{_GRAPH}/paper/{paper_id}/references", params={"limit": limit, "fields": fields})
        return [row.get("citedPaper", {}) for row in (data.get("data", []) or [])]

    def author_search(self, query: str, *, limit: int = 10, fields: str = DEFAULT_AUTHOR_FIELDS) -> list[dict[str, Any]]:
        data = self._request("GET", f"{_GRAPH}/author/search", params={"query": query, "limit": limit, "fields": fields})
        return data.get("data", []) or []

    def author_details(self, author_id: str, *, fields: str = DEFAULT_AUTHOR_FIELDS) -> dict[str, Any]:
        return self._request("GET", f"{_GRAPH}/author/{author_id}", params={"fields": fields})

    def author_papers(self, author_id: str, *, limit: int = 20, fields: str = DEFAULT_PAPER_FIELDS) -> list[dict[str, Any]]:
        data = self._request("GET", f"{_GRAPH}/author/{author_id}/papers", params={"limit": limit, "fields": fields})
        return data.get("data", []) or []

    # -------------------------------------------------------- recommendations

    def recommend_for_paper(
        self,
        paper_id: str,
        *,
        limit: int = 20,
        fields: str = DEFAULT_PAPER_FIELDS,
    ) -> list[dict[str, Any]]:
        """Recommendations from a single seed paper (GET /papers/forpaper/{id})."""
        data = self._request(
            "GET",
            f"{_RECOMMENDATIONS}/papers/forpaper/{paper_id}",
            params={"limit": limit, "fields": fields},
        )
        return data.get("recommendedPapers", []) or []

    def recommend_for_list(
        self,
        positive: list[str],
        *,
        negative: list[str] | None = None,
        limit: int = 20,
        fields: str = DEFAULT_PAPER_FIELDS,
    ) -> list[dict[str, Any]]:
        """Recommendations from positive/negative example lists (POST /papers/).

        Uses the CURRENT route POST /recommendations/v1/papers/ — the older
        /papers/forlist route was retired and answers 405 Method Not Allowed.
        """
        if not positive:
            raise ValueError("recommend_for_list requires at least one positive paper id")
        data = self._request(
            "POST",
            f"{_RECOMMENDATIONS}/papers/",
            params={"limit": limit, "fields": fields},
            body={"positivePaperIds": positive, "negativePaperIds": negative or []},
        )
        return data.get("recommendedPapers", []) or []

    # ---------------------------------------------------------------- datasets

    def dataset_releases(self) -> list[str]:
        """All available dataset release ids (chronological)."""
        data = self._request("GET", f"{_DATASETS}/release/", retries_429=False)
        return data if isinstance(data, list) else []

    def datasets_in_release(self, release_id: str = "latest") -> dict[str, Any]:
        """Datasets included in a release (README + dataset names)."""
        return self._request("GET", f"{_DATASETS}/release/{release_id}", retries_429=False)

    def dataset_download_links(self, dataset_name: str, release_id: str = "latest") -> dict[str, Any]:
        """File manifest + download instructions for one dataset in a release."""
        return self._request(
            "GET", f"{_DATASETS}/release/{release_id}/dataset/{dataset_name}", retries_429=False
        )

    def dataset_diffs(self, dataset_name: str, start_release: str, end_release: str = "latest") -> dict[str, Any]:
        """Incremental diffs to update a dataset between two releases."""
        return self._request(
            "GET",
            f"{_DATASETS}/diffs/{start_release}/to/{end_release}/{dataset_name}",
            retries_429=False,
        )


def load_s2_client(config_path: str | None = None) -> S2Client:
    """Build an S2Client from an api_keys.json config file."""
    path = Path(config_path) if config_path else _THIS_DIR.parent / "config" / "api_keys.json"
    try:
        with open(path) as f:
            config = json.load(f)
        entry = config.get("semantic_scholar") or {}
        return S2Client(str(entry.get("api_key") or ""))
    except Exception as exc:
        print(f"  !! Could not load S2 config from {path}: {exc}")
        return S2Client("")