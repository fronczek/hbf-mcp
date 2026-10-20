"""Narrow YouTrack tools for the HBF MCP service; no shell/SSH dependency.

Register with an existing FastMCP instance via register_tools(mcp).
Credentials remain on hbfplhmcpu01 and are never returned.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

CREDENTIALS = Path("/home/gptagent/credentials/youtrack.yml")
ISSUE_ID = re.compile(r"^ESE-[1-9][0-9]{0,7}$")


def _credentials() -> dict[str, str]:
    if not CREDENTIALS.is_file():
        raise RuntimeError("GPT YouTrack credential is not configured")
    result: dict[str, str] = {}
    for line in CREDENTIALS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("'\"")
    required = ("youtrack_url", "youtrack_token_value")
    if any(not result.get(k) for k in required):
        raise RuntimeError("Incomplete local GPT YouTrack credential")
    endpoint = urllib.parse.urlsplit(result["youtrack_url"])
    if (endpoint.scheme != "https" or endpoint.hostname != "youtrack.hbf.pl"
            or endpoint.port not in (None, 443) or endpoint.username or endpoint.password
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise RuntimeError("YouTrack URL must be a trusted HTTPS endpoint")
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("YouTrack redirect refused")


def _call(method: str, route: str, payload: dict | None = None) -> Any:
    if not route.startswith("/api/") or route.startswith("//"):
        raise ValueError("Only the YouTrack API is permitted")
    config = _credentials()
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    req = urllib.request.Request(
        config["youtrack_url"].rstrip("/") + route,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + config["youtrack_token_value"],
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(req, timeout=20) as response:
            body = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        # Never print response bodies: issue content may contain secrets.
        raise RuntimeError(f"YouTrack returned HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("YouTrack API connection failed") from None
    if len(body) > 2_000_000:
        raise RuntimeError("YouTrack response is unexpectedly large")
    return json.loads(body) if body else {}


def _issue(issue_id: str) -> str:
    issue_id = issue_id.strip()
    if not ISSUE_ID.fullmatch(issue_id):
        raise ValueError("Issue must be an ESE project issue, e.g. ESE-801")
    return "/api/issues/" + urllib.parse.quote(issue_id, safe="")


def _confirm_gpt_account() -> None:
    who = _call("GET", "/api/users/me?fields=login")
    if who.get("login") != "gpt":
        raise RuntimeError("Refusing to write to YouTrack: account is not GPT")


def register_tools(mcp: Any) -> None:
    """Attach narrowly scoped YouTrack methods to an existing MCP FastMCP app."""

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

    @mcp.tool(annotations=read)
    def youtrack_search_issues(query: str = "", limit: int = 15, skip: int = 0) -> dict:
        """Find ESE issues with bounded summaries; optional YouTrack search terms."""
        if not 1 <= limit <= 50 or not 0 <= skip <= 2000 or len(query) > 160:
            raise ValueError("Invalid search pagination or query")
        if any(ch in query for ch in "\r\n"):
            raise ValueError("Search query must be a single line")
        q = "project: ESE " + query.strip()
        params = urllib.parse.urlencode({"query":q,"$top":limit,"$skip":skip,
            "fields":"idReadable,summary,updated,resolved,customFields(name,value(name))"})
        results = _call("GET", "/api/issues?" + params)
        if not isinstance(results,list):
            raise RuntimeError("Unexpected issue search response")
        return {"issues":[{"id":r.get("idReadable"),"summary":(r.get("summary") or "")[:240],
                  "updated":r.get("updated"),"resolved":bool(r.get("resolved"))} for r in results],
                "limit":limit,"skip":skip,"has_more":len(results)==limit}

    @mcp.tool(annotations=read)
    def youtrack_issue_detail(issue_id: str, max_description: int = 5000) -> dict:
        """Read a bounded ESE issue description plus current status and metadata."""
        if not 0 <= max_description <= 12000:
            raise ValueError("Description limit must be 0..12000")
        obj=_call("GET",_issue(issue_id)+
                "?fields=idReadable,summary,description,updated,customFields(name,value(name))")
        body=obj.get("description") or ""
        return {"id":obj.get("idReadable"),"summary":obj.get("summary"),
                "updated":obj.get("updated"),"description":body[:max_description],
                "description_truncated":len(body)>max_description,
                "fields":{f.get("name"):(f.get("value") or {}).get("name")
                 for f in obj.get("customFields",[]) if isinstance(f.get("value"),dict)}}

    @mcp.tool(annotations=read)
    def youtrack_issue_comments(issue_id: str, limit: int = 10) -> dict:
        """Read recent ESE issue comments as short excerpts, without full comment dumps."""
        if not 1 <= limit <= 30:
            raise ValueError("Limit must be 1..30")
        params=urllib.parse.urlencode({"$top":limit,"fields":"id,author(login),created,text"})
        obj=_call("GET",_issue(issue_id)+"/comments?"+params)
        if not isinstance(obj,list):
            raise RuntimeError("Unexpected comments response")
        return {"issue":issue_id,"comments":[{"id":x.get("id"),
            "author":(x.get("author") or {}).get("login"),"created":x.get("created"),
            "excerpt":(x.get("text") or "")[:1200]} for x in obj]}

    @mcp.tool(annotations=write)
    def youtrack_apply_state(issue_id: str, new_state: str) -> dict:
        """Change ESE issue state via YouTrack command API (explicit state only)."""
        allowed={"Open","In Progress","Fixed","Verified","Reopened","To Do"}
        if new_state not in allowed:
            raise ValueError("Unrecognised state; check project state values")
        _confirm_gpt_account()
        issue=_call("GET",_issue(issue_id)+"?fields=id,idReadable")
        if not issue.get("id") or issue.get("idReadable")!=issue_id:
            raise RuntimeError("Unexpected issue identity")
        _call("POST","/api/commands",{"query":"State "+new_state,
                                    "issues":[{"id":issue["id"]}]})
        return {"issue":issue_id,"requested_state":new_state,
                "result":"accepted by YouTrack; refresh issue to confirm"}

    @mcp.tool(annotations=read)
    def youtrack_issue_overview(issue_id: str) -> dict:
        """Read an ESE issue summary, state, and section headings (not raw secret data)."""
        obj = _call(
            "GET",
            _issue(issue_id) + "?fields=idReadable,summary,description,customFields(name,value(name))",
        )
        sections = [
            x.strip()[:180]
            for x in (obj.get("description") or "").splitlines()
            if x.startswith("## ")
        ]
        state = next((
            (x.get("value") or {}).get("name")
            for x in obj.get("customFields") or []
            if x.get("name") == "State"
        ), None)
        return {
            "id": obj.get("idReadable"), "summary": obj.get("summary"),
            "state": state, "sections": sections,
        }

    @mcp.tool(annotations=write)
    def youtrack_add_comment(issue_id: str, text: str) -> dict:
        """Post a GPT-authored comment to an ESE issue; no shell access."""
        route = _issue(issue_id)
        text = text.strip()
        if not 1 <= len(text) <= 10_000:
            raise ValueError("Comment must contain between 1 and 10000 characters")
        _confirm_gpt_account()
        result = _call(
            "POST", route + "/comments?fields=id,author(login)",
            {"text": text},
        )
        author = (result.get("author") or {}).get("login")
        if author != "gpt":
            raise RuntimeError("Posted comment author differs from GPT")
        return {"issue": issue_id, "comment_id": result.get("id"), "author": author}

    @mcp.tool(annotations=write)
    def youtrack_append_decision(issue_id: str, heading: str, body: str) -> dict:
        """Idempotently prepend a decision section to an ESE issue description."""
        route = _issue(issue_id)
        heading, body = heading.strip(), body.strip()
        if (not 5 <= len(heading) <= 180 or
                any(c in heading for c in "\r\n#") or
                not 1 <= len(body) <= 12000):
            raise ValueError("Expected a short plain heading and decision body")
        _confirm_gpt_account()
        marker = "## " + heading
        endpoint = route + "?fields=idReadable,description"
        original = _call("GET", endpoint)
        if original.get("idReadable") != issue_id:
            raise RuntimeError("YouTrack returned an unexpected issue")
        desc = original.get("description") or ""
        if marker in desc.splitlines():
            return {"issue": issue_id, "changed": False, "reason": "heading exists"}
        updated = marker + "\n\n" + body + "\n\n" + desc
        outcome = _call("POST", endpoint, {"description": updated})
        if marker not in (outcome.get("description") or "").splitlines():
            raise RuntimeError("YouTrack failed to confirm the description update")
        return {"issue": issue_id, "changed": True, "heading": heading}
