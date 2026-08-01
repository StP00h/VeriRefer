from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

try:
    import requests as _requests
except Exception:  # pragma: no cover - exercised only when requests is unavailable
    _requests = None


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class CompatResponse:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        stream_handle: Any = None,
    ) -> None:
        self.status_code = int(status_code)
        self.headers = dict(headers or {})
        self._body = body if body is not None else b""
        self._stream_handle = stream_handle

    @property
    def text(self) -> str:
        if self._stream_handle is not None and not self._body:
            self._body = self._stream_handle.read()
            self._stream_handle.close()
            self._stream_handle = None
        return self._body.decode("utf-8", errors="ignore")

    def json(self) -> Any:
        return json.loads(self.text or "{}")

    def iter_content(self, chunk_size: int = 8192):
        if self._stream_handle is not None:
            try:
                while True:
                    chunk = self._stream_handle.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                self._stream_handle.close()
                self._stream_handle = None
            return

        if not self._body:
            return
        for idx in range(0, len(self._body), max(chunk_size, 1)):
            yield self._body[idx : idx + chunk_size]


def _build_url(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    query = urlencode(params, doseq=True)
    if not query:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{query}"


def http_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    stream: bool = False,
    allow_redirects: bool = True,
):
    if _requests is not None:
        return _requests.get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=allow_redirects,
        )

    resolved_url = _build_url(url, params)
    request = Request(resolved_url, headers=headers or {}, method="GET")
    opener = (
        build_opener()
        if allow_redirects
        else build_opener(_NoRedirectHandler())
    )
    try:
        response = opener.open(request, timeout=timeout)
        if stream:
            return CompatResponse(
                status_code=getattr(response, "status", response.getcode()),
                headers=dict(response.headers.items()),
                stream_handle=response,
            )
        body = response.read()
        response.close()
        return CompatResponse(
            status_code=getattr(response, "status", response.getcode()),
            headers=dict(response.headers.items()),
            body=body,
        )
    except HTTPError as error:
        body = b""
        try:
            body = error.read()
        except Exception:
            body = b""
        return CompatResponse(
            status_code=error.code,
            headers=dict(error.headers.items()) if error.headers else {},
            body=body,
        )
    except URLError as error:
        raise RuntimeError(str(error)) from error
