"""Small HTTPS-only JSON client with bounded responses and no redirects."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("API redirect refused")


def request_raw(base: str, route: str, token: str, *, method: str = "GET",
                query: dict[str, Any] | None = None, payload: dict | None = None,
                auth_scheme: str = "Bearer", accept: str = "application/json") -> bytes:
    if not route.startswith("/") or route.startswith("//"):
        raise ValueError("API route must be an absolute path")
    url = base.rstrip("/") + route
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"{auth_scheme} {token}", "Accept": accept,
        "Content-Type": "application/json", "User-Agent": "hbf-api-tools/0.1",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=20) as response:
            raw = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        raise ApiError(f"API returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise ApiError("API connection failed") from None
    if len(raw) > 2_000_000:
        raise ApiError("API response too large")
    return raw


def request_json(base: str, route: str, token: str, *, method: str = "GET",
                 query: dict[str, Any] | None = None, payload: dict | None = None,
                 auth_scheme: str = "Bearer") -> Any:
    raw = request_raw(base, route, token, method=method, query=query,
                      payload=payload, auth_scheme=auth_scheme)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeError, ValueError):
        raise ApiError("API returned invalid JSON") from None
