"""Bounded read-only Emby MCP tools supporting multiple configured instances.

Instances come only from ~/.credentials/emby*.yml on the MCP host.
Credentials and arbitrary URLs are never accepted as tool parameters.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

CREDENTIAL_DIR = Path(os.getenv("HBF_MCP_AGENT_HOME", "/home/gptagent")) / ".credentials"
MAX_BODY = 256 * 1024
ITEM_FIELDS = (
    "Id", "Name", "Type", "ProductionYear", "Path", "ProviderIds",
    "CommunityRating", "CriticRating", "OfficialRating", "Genres",
    "SortName", "LockData", "LockedFields",
)
ID_RE = re.compile(r"[a-zA-Z0-9-]{1,64}\Z")
INSTANCE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward server credentials across HTTP redirects."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _instance_name(path: Path) -> str:
    stem = path.stem
    if stem == "emby":
        return "default"
    suffix = stem[4:]
    return suffix[1:] if suffix.startswith(("-", "_")) else suffix


def _instances() -> dict[str, Path]:
    """Discover regular files only; aliases cannot be used for path traversal."""
    found: dict[str, Path] = {}
    for path in sorted(CREDENTIAL_DIR.glob("emby*.yml")):
        if path.is_symlink() or not path.is_file():
            continue
        name = _instance_name(path)
        if not INSTANCE_RE.fullmatch(name):
            continue
        if name in found:
            raise RuntimeError("Duplicate Emby instance names in credentials directory")
        found[name] = path
    return found


def list_instances() -> dict[str, Any]:
    """Expose only instance aliases, never URLs, tokens or credential contents."""
    instances = _instances()
    return {"instances": sorted(instances), "count": len(instances)}


def _choose_instance(instance: str | None) -> tuple[str, Path]:
    paths = _instances()
    if instance is not None:
        if not isinstance(instance, str) or not INSTANCE_RE.fullmatch(instance):
            raise ValueError("Invalid Emby instance name")
        if instance not in paths:
            raise ValueError("Unknown Emby instance; use emby_instances to list names")
        return instance, paths[instance]
    if not paths:
        raise RuntimeError("No Emby instances configured")
    if len(paths) != 1:
        raise ValueError("Multiple Emby instances configured; specify instance")
    return next(iter(paths.items()))


def _settings(instance: str | None) -> tuple[str, dict[str, str]]:
    name, path = _choose_instance(instance)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise RuntimeError("Emby credentials are unavailable") from None

    parsed: dict[str, str] = {}
    expected = {"emby_internal", "emby_external", "emby_token"}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise RuntimeError("Emby credentials have an invalid format")
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in expected or key in parsed:
            raise RuntimeError("Emby credentials have an invalid format")
        parsed[key] = value.strip().strip("\"'")
    if not expected.issubset(parsed):
        raise RuntimeError("Emby credentials are incomplete")
    if not re.fullmatch(r"[a-zA-Z0-9._-]{16,256}", parsed["emby_token"]):
        raise RuntimeError("Emby API token has an invalid format")
    return name, parsed


def _base_url(target: str, config: dict[str, str]) -> str:
    if target not in ("internal", "external"):
        raise ValueError("target must be internal or external")
    raw_url = config["emby_" + target]
    parts = urllib.parse.urlsplit(raw_url)
    host = parts.hostname
    try:
        port = parts.port
    except ValueError:
        raise RuntimeError("Emby URL is not permitted") from None
    if (not host or parts.username or parts.password or parts.query or parts.fragment
            or (parts.netloc.endswith(":") and port is None)
            or parts.path not in ("", "/", "/emby", "/emby/")):
        raise RuntimeError("Emby URL is not permitted")
    if target == "internal":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise RuntimeError("Emby internal URL must use a private IP") from None
        if (parts.scheme not in ("http", "https") or not ip.is_private
                or ip.is_link_local or ip.is_loopback or ip.is_unspecified
                or ip.is_multicast):
            raise RuntimeError("Emby internal URL is not permitted")
    else:
        # Each locally provisioned file owns its external hostname.
        # Do not limit all instances to one hard-coded Emby DNS name.
        if parts.scheme != "https" or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
            raise RuntimeError("Emby external URL is not permitted")
    return raw_url.rstrip("/")


def _api(endpoint: str, *, params: dict[str, str] | None = None,
         target: str = "internal", instance: str | None = None) -> tuple[Any, str]:
    """GET fixed API endpoints only; returns the selected instance alias."""
    name, config = _settings(instance)
    base = _base_url(target, config)
    query = urllib.parse.urlencode(params or {})
    suffix = "?" + query if query else ""
    paths = [endpoint] if base.endswith("/emby") else [endpoint, "/emby" + endpoint]
    for idx, path in enumerate(paths):
        request = urllib.request.Request(
            base + path + suffix, method="GET",
            headers={"X-Emby-Token": config["emby_token"], "Accept": "application/json"},
        )
        try:
            with _OPENER.open(request, timeout=8) as response:
                if response.status != 200:
                    raise RuntimeError("Emby API returned a non-success status")
                body = response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and idx < len(paths) - 1:
                continue
            raise RuntimeError("Emby API returned HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Emby API is unavailable") from None
        if len(body) > MAX_BODY:
            raise RuntimeError("Emby API response exceeded size limit")
        try:
            return json.loads(body), name
        except (ValueError, UnicodeError):
            raise RuntimeError("Emby API returned invalid JSON") from None
    raise RuntimeError("Emby API endpoint not found")


def _movie(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise RuntimeError("Emby API returned invalid movie metadata")
    movie = {key: item[key] for key in ITEM_FIELDS if key in item}
    providers = item.get("ProviderIds")
    movie["has_imdb_id"] = (
        isinstance(providers, dict)
        and any(str(k).lower() == "imdb" and bool(v) for k, v in providers.items())
    )
    return movie


def server_status(target: str = "internal", instance: str | None = None) -> dict[str, Any]:
    """Verify authenticated API access for the chosen instance."""
    info, name = _api("/System/Info", target=target, instance=instance)
    if not isinstance(info, dict):
        raise RuntimeError("Emby returned invalid server information")
    return {
        "connected": True, "instance": name, "target": target,
        "name": info.get("ServerName"), "version": info.get("Version"),
        "id": info.get("Id"),
    }


def library_counts(target: str = "internal", instance: str | None = None) -> dict[str, Any]:
    counts, name = _api("/Items/Counts", target=target, instance=instance)
    if not isinstance(counts, dict):
        raise RuntimeError("Emby returned invalid library counts")
    keys = ("MovieCount", "SeriesCount", "EpisodeCount", "ItemCount")
    return {"instance": name, **{key: counts.get(key) for key in keys}}


def movies(start_index: int = 0, limit: int = 50, search_term: str = "",
           year: int = 0, missing_imdb_only: bool = False,
           target: str = "internal", instance: str | None = None) -> dict[str, Any]:
    if type(start_index) is not int or not 0 <= start_index <= 10_000_000:
        raise ValueError("start_index must be between 0 and 10000000")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(year) is not int or (year != 0 and not 1880 <= year <= 2100):
        raise ValueError("year must be zero or between 1880 and 2100")
    if (not isinstance(search_term, str) or len(search_term) > 120
            or any(ord(ch) < 32 for ch in search_term)):
        raise ValueError("search_term must be at most 120 printable characters")
    if type(missing_imdb_only) is not bool:
        raise ValueError("missing_imdb_only must be a boolean")
    params = {
        "IncludeItemTypes": "Movie", "Recursive": "true",
        "Fields": "Path,ProviderIds,Genres", "StartIndex": str(start_index),
        "Limit": str(limit), "SortBy": "SortName",
    }
    if search_term.strip():
        params["SearchTerm"] = search_term.strip()
    if year:
        params["Years"] = str(year)
    if missing_imdb_only:
        params["HasImdbId"] = "false"
    result, name = _api("/Items", params=params, target=target, instance=instance)
    if not isinstance(result, dict) or not isinstance(result.get("Items"), list):
        raise RuntimeError("Emby returned an invalid movie list")
    items = result["Items"]
    if len(items) > limit:
        raise RuntimeError("Emby returned more items than requested")
    return {
        "instance": name, "start_index": start_index, "limit": limit,
        "total": result.get("TotalRecordCount"),
        "items": [_movie(item) for item in items],
    }


def movie_details(item_id: str, target: str = "internal",
                  instance: str | None = None) -> dict[str, Any]:
    if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
        raise ValueError("item_id must be an Emby movie ID")
    result, name = _api(
        "/Items",
        params={"Ids": item_id, "IncludeItemTypes": "Movie",
                "Fields": "Path,ProviderIds,Genres", "Limit": "1"},
        target=target, instance=instance,
    )
    if not isinstance(result, dict) or not isinstance(result.get("Items"), list):
        raise RuntimeError("Emby returned invalid item metadata")
    items = result["Items"]
    if not items:
        raise ValueError("Emby movie not found")
    movie = _movie(items[0])
    if str(movie.get("Id")) != item_id:
        raise RuntimeError("Emby returned an unexpected movie ID")
    return {"instance": name, **movie}


def register_tools(mcp: Any) -> None:
    readonly = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    )

    @mcp.tool(annotations=readonly)
    def emby_instances() -> dict:
        """List configured Emby instance names from .credentials/emby*.yml (no secrets)."""
        return list_instances()

    @mcp.tool(annotations=readonly)
    def emby_status(target: str = "internal", instance: str | None = None) -> dict:
        """Check one configured Emby server. Select instance by file suffix."""
        return server_status(target, instance)

    @mcp.tool(annotations=readonly)
    def emby_library_counts(target: str = "internal", instance: str | None = None) -> dict:
        """Get read-only Emby library counts from one configured instance."""
        return library_counts(target, instance)

    @mcp.tool(annotations=readonly)
    def emby_movies(start_index: int = 0, limit: int = 50, search_term: str = "",
                    year: int = 0, missing_imdb_only: bool = False,
                    target: str = "internal", instance: str | None = None) -> dict:
        """List a bounded page of Emby movies, including paths and provider IDs."""
        return movies(start_index, limit, search_term, year,
                      missing_imdb_only, target, instance)

    @mcp.tool(annotations=readonly)
    def emby_movie_details(item_id: str, target: str = "internal",
                           instance: str | None = None) -> dict:
        """Read metadata of one Emby movie by ID. Never modify library metadata."""
        return movie_details(item_id, target, instance)
