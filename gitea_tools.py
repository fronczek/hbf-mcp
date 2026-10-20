"""Typed Gitea REST tools for HBF MCP on hbfplhmcpu01.

Adapted from security/operations patterns in hbf-gpt-gitea (no external deps).
No remote shell, SSH, arbitrary URLs, or unsigned Git commit creation.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import binascii
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

from mcp.types import ToolAnnotations

CREDENTIAL_FILE = Path("/home/gptagent/credentials/gitea.yml")
DB_FILE = Path("/home/gptagent/.local/state/hbf-mcp-gitea/operations.sqlite3")
DEFAULT_REPOS = (
    "secnet/ese-agent", "secnet/ese-backend", "secnet/ese-frontend",
    "secnet/ese-ansible", "hbf/hbf-gpt-gitea", "hbf/hbf-gpt-mailer",
    "hbf/hbf-azure-ocr", "hbf/hbf-runner-u26",
)
REPAT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REFPAT = re.compile(r"^[A-Za-z0-9._/-]{1,150}$")
KEYPAT = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_RESPONSE = 2_000_000


class UpstreamError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Gitea returned HTTP {status}; inspect current state before retrying a write")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Gitea redirect refused")


def _credentials() -> tuple[str, str]:
    v: dict[str, str] = {}
    for line in CREDENTIAL_FILE.read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, x = line.split(":", 1)
            v[k.strip()] = x.strip().strip("'\"")
    raw = v.get("gitea_url", "").rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if (parsed.scheme != "https" or parsed.netloc != "git.hbf.pl"
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise RuntimeError("Untrusted or missing Gitea HTTPS endpoint")
    token = v.get("token_value", "")
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise RuntimeError("Invalid Gitea credential")
    return raw, token


def _scope() -> frozenset[str]:
    custom = os.environ.get("HBF_GITEA_MCP_REPOSITORIES", "")
    items = custom.split(",") if custom.strip() else DEFAULT_REPOS
    scope = frozenset(x.strip() for x in items if x.strip())
    if not scope or any(not REPAT.fullmatch(x) or any(y in (".", "..") for y in x.split("/")) for x in scope):
        raise RuntimeError("Invalid exact repository allowlist")
    return scope


def _repo(name: str) -> str:
    if name not in _scope():
        raise ValueError("Repository not present in configured Gitea allowlist")
    owner, repo = name.split("/")
    return "/repos/" + _segment(owner) + "/" + _segment(repo)


def _segment(value: str) -> str:
    if not value or value in (".", "..") or any(ord(c) < 32 or c == "\\" for c in value):
        raise ValueError("Invalid URL path component")
    return urllib.parse.quote(value, safe="")


def _file_path(value: str) -> str:
    if not value or value.startswith("/") or any(x in ("", ".", "..") for x in value.split("/")):
        raise ValueError("Expected a relative repository path")
    return "/".join(_segment(x) for x in value.split("/"))


def _index(n: int) -> int:
    if not isinstance(n, int) or n < 1:
        raise ValueError("Index must be positive")
    return n


def _page(page: int, limit: int) -> dict[str, int]:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("Invalid page/limit (1..100)")
    return {"page": page, "limit": limit}


def _ref(text: str) -> str:
    if not isinstance(text, str) or not REFPAT.fullmatch(text) or text.startswith("/") or ".." in text.split("/"):
        raise ValueError("Invalid branch/ref")
    return text


def _sha(text: str) -> str:
    if not re.fullmatch(r"[A-Fa-f0-9]{40,64}", text or ""):
        raise ValueError("Full commit SHA expected")
    return text


def _api(method: str, path: str, *, query: dict | None = None,
         payload: dict | None = None, text: bool = False) -> Any:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("API path must be absolute and relative to Gitea API")
    base, token = _credentials()
    url = base + "/api/v1" + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else None
    request = urllib.request.Request(
        url, method=method, data=data,
        headers={"Authorization": "token " + token, "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "hbf-mcp-gitea/0.1"},
    )
    # Reject redirects (including to an untrusted origin) and avoid env proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(request, timeout=25) as response:
            blob = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(exc.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Gitea API connection failed; if writing, inspect before retrying") from None
    if len(blob) > MAX_RESPONSE:
        raise RuntimeError("Response too large; narrow the request")
    if text:
        return blob.decode("utf-8", errors="replace")
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except ValueError:
        raise RuntimeError("Gitea did not return JSON") from None


def _gpt_account() -> None:
    data = _api("GET", "/user")
    if data.get("login") != "gpt":
        raise RuntimeError("Refusing to mutate Gitea: credential is not GPT")


@contextmanager
def _db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(DB_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    connection = sqlite3.connect(DB_FILE, timeout=10)
    connection.execute("""CREATE TABLE IF NOT EXISTS operation (
        key TEXT PRIMARY KEY, repo TEXT NOT NULL, fingerprint TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT, error TEXT, created_at REAL NOT NULL
    )""")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _operation_status(repo: str, key: str) -> dict:
    if not KEYPAT.fullmatch(key):
        raise ValueError("idempotency_key must be 8..128 alphanumeric characters/.-_:")
    with _db() as db:
        row = db.execute("SELECT status,result,error,created_at FROM operation WHERE key=? AND repo=?", (key, repo)).fetchone()
    return ({"status": row[0], "result": json.loads(row[1]) if row[1] else None,
             "error": row[2], "created_at": row[3]} if row else {"status": "not_found"})


def _mutation(repo: str, key: str, method: str, route: str, body: dict) -> dict:
    if not KEYPAT.fullmatch(key):
        raise ValueError("Provide a unique 8..128-character idempotency_key (UUID recommended)")
    _gpt_account()
    payload = {"repo": repo, "method": method, "route": route, "body": body}
    fingerprint = hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT repo,fingerprint,status,result,error FROM operation WHERE key=?", (key,)).fetchone()
        if row:
            if row[0] != repo or row[1] != fingerprint:
                raise ValueError("idempotency_key reused for different operation")
            if row[2] == "done":
                return {**json.loads(row[3]), "reused_result": True}
            raise RuntimeError("Operation already attempted (" + row[2] + "); inspect operation status instead")
        db.execute("INSERT INTO operation VALUES(?,?,?,?,?,?,?)",
                   (key, repo, fingerprint, "uncertain", None, None, time.time()))
    try:
        result = _api(method, route, payload=body)
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected mutation response")
    except UpstreamError as e:
        with _db() as db:
            db.execute("UPDATE operation SET status=?,error=? WHERE key=?",
                       ("rejected" if e.status < 500 else "uncertain", str(e), key))
        raise
    except BaseException:
        # Timeout or crash can happen after server-side success.
        raise
    with _db() as db:
        db.execute("UPDATE operation SET status='done',result=? WHERE key=?",
                   (json.dumps(result,ensure_ascii=False), key))
    return {**result, "reused_result": False}


def _compact_pr(pr: dict) -> dict:
    keys = ("number", "title", "body", "state", "html_url", "merged", "mergeable",
            "draft", "merged_at", "updated_at", "content_version")
    result = {key: pr.get(key) for key in keys}
    for field in ("head", "base"):
        ref = pr.get(field) or {}
        result[field] = {key: ref.get(key) for key in ("ref", "sha")}
    return result


def register_tools(mcp: Any) -> None:
    """Register typed tools on an existing MCPServer."""
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    @mcp.tool(annotations=read)
    def gitea_connection() -> dict:
        """Show authenticated Gitea account and exact repository allowlist."""
        user = _api("GET", "/user")
        return {"login": user.get("login"), "repositories": sorted(_scope()),
                "transport": "REST API; no SSH or shell"}

    @mcp.tool(annotations=read)
    def gitea_get_repository(repo: str) -> dict:
        """Read repo metadata and permissions for an allowlisted repository."""
        r = _api("GET", _repo(repo))
        return {k: r.get(k) for k in ("full_name", "default_branch", "description", "private", "html_url", "permissions")}

    @mcp.tool(annotations=read)
    def gitea_list_branches(repo: str, page: int = 1, limit: int = 30) -> dict:
        """List branches and commit hashes."""
        rows = _api("GET", _repo(repo)+"/branches", query=_page(page,limit))
        return {"branches": [{"name":r.get("name"),"commit":(r.get("commit") or {}).get("id")} for r in rows], "page": page}

    @mcp.tool(annotations=read)
    def gitea_get_branch(repo: str, branch: str) -> dict:
        """Read branch head and protection status."""
        return _api("GET", _repo(repo)+"/branches/"+_segment(_ref(branch)))

    @mcp.tool(annotations=read)
    def gitea_read_path(repo: str, ref: str, path: str = "", start_line: int = 1, max_lines: int = 300) -> dict:
        """List repo directory or read line-numbered UTF-8 file, preserving blob SHA."""
        if start_line < 1 or not 1 <= max_lines <= 1000:
            raise ValueError("Invalid line range")
        route = _repo(repo)+"/contents"+("/"+_file_path(path) if path else "")
        item = _api("GET",route,query={"ref":_ref(ref)})
        if isinstance(item,list):
            return {"entries": [{k: x.get(k) for k in ("name","path","type","size","sha")} for x in item]}
        if item.get("type") != "file" or item.get("encoding") != "base64":
            return {k: item.get(k) for k in ("name","path","type","sha","size","target")}
        if item.get("size",0) > 256000:
            raise ValueError("File larger than 256 kB; use clone or narrow data")
        try:
            raw = base64.b64decode("".join(item.get("content","").split()),validate=True)
            data = raw.decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise ValueError("File is not valid base64 UTF-8") from None
        if "\x00" in data or len(raw)>256000:
            raise ValueError("Binary/oversize file")
        lines=data.splitlines(keepends=True)
        selected=lines[start_line-1:start_line-1+max_lines]
        next_line=start_line+len(selected)
        return {"path":item.get("path"),"sha":item.get("sha"),"ref":ref,
                "content":"".join(selected),"start_line":start_line,
                "total_lines":len(lines),"has_more":next_line<=len(lines),
                "next_line":next_line if next_line<=len(lines) else None}

    @mcp.tool(annotations=read)
    def gitea_list_commits(repo: str, ref: str="master", page: int=1, limit: int=20) -> dict:
        """Read commit history, authors and short messages."""
        rows=_api("GET",_repo(repo)+"/commits",query={"sha":_ref(ref),**_page(page,limit)})
        return {"commits":[{"sha":x.get("sha"),"message":((x.get("commit") or {}).get("message") or "")[:600],
                           "author":((x.get("commit") or {}).get("author") or {}).get("name")} for x in rows], "page":page}

    @mcp.tool(annotations=read)
    def gitea_get_commit(repo: str, commit_sha: str) -> dict:
        """Inspect commit details at an exact full hash."""
        return _api("GET", _repo(repo)+"/git/commits/"+_sha(commit_sha))

    @mcp.tool(annotations=read)
    def gitea_compare_refs(repo: str, base: str, head: str) -> dict:
        """Compare two refs and list changed files/commits."""
        return _api("GET", _repo(repo)+"/compare/"+_segment(_ref(base))+"..."+_segment(_ref(head)))

    @mcp.tool(annotations=read)
    def gitea_get_tree(repo: str, ref: str, recursive: bool = False) -> dict:
        """List Git tree paths at a branch/commit; large trees fail clearly."""
        data = _api("GET",_repo(repo)+"/git/trees/"+_segment(_ref(ref)),
                    query={"recursive":str(recursive).lower()})
        items = data.get("tree") or []
        if not isinstance(items,list): raise RuntimeError("Unexpected Git tree response")
        return {"sha":data.get("sha"),"truncated":data.get("truncated"),
                "tree":[{k:x.get(k) for k in ("path","type","mode","sha","size")} for x in items]}

    @mcp.tool(annotations=read)
    def gitea_search_code(repo: str, commit_sha: str, paths: list[str],
                          query: str, case_sensitive: bool = True, max_matches: int = 50) -> dict:
        """Literal search in 1..10 named files at one immutable commit SHA."""
        _sha(commit_sha)
        if not 1<=len(paths)<=10 or not 1<=len(query)<=300 or not 1<=max_matches<=50:
            raise ValueError("Provide 1..10 files, a query and max_matches<=50")
        hits=[]
        for path in paths:
            _file_path(path)
            result=gitea_read_path(repo,commit_sha,path,start_line=1,max_lines=1000)
            if "content" not in result:
                raise ValueError("Search path is not a UTF-8 file")
            while True:
                start=result["start_line"]
                needle=query if case_sensitive else query.casefold()
                for index,line in enumerate(result["content"].splitlines(),start):
                    found=needle in (line if case_sensitive else line.casefold())
                    if found:
                        hits.append({"path":path,"line":index,"text":line[:600]})
                        if len(hits)>=max_matches:
                            return {"matches":hits,"limit_reached":True}
                if not result.get("has_more"):break
                result=gitea_read_path(repo,commit_sha,path,start_line=result["next_line"],max_lines=1000)
        return {"matches":hits,"limit_reached":False}

    @mcp.tool(annotations=read)
    def gitea_list_issues(repo: str, state: Literal["open","closed","all"]="open", page:int=1, limit:int=20) -> dict:
        """List issues in an allowed repo (not pull requests)."""
        rows=_api("GET", _repo(repo)+"/issues", query={"state":state,"type":"issues",**_page(page,limit)})
        return {"issues":[{"number":x.get("number"),"title":x.get("title"),
                          "state":x.get("state"),"html_url":x.get("html_url")} for x in rows],"page":page}

    @mcp.tool(annotations=read)
    def gitea_get_issue(repo: str, number: int) -> dict:
        """Read a single Gitea issue."""
        return _api("GET",_repo(repo)+f"/issues/{_index(number)}")

    @mcp.tool(annotations=read)
    def gitea_list_pull_requests(repo: str,state:Literal["open","closed","all"]="open",page:int=1,limit:int=20) -> dict:
        """List pull requests with current head SHA."""
        rows=_api("GET",_repo(repo)+"/pulls",query={"state":state,**_page(page,limit)})
        return {"pull_requests":[_compact_pr(x) for x in rows],"page":page}

    @mcp.tool(annotations=read)
    def gitea_get_pull_request(repo: str, number: int) -> dict:
        """Read PR details, branch refs, review state and current head SHA."""
        return _compact_pr(_api("GET",_repo(repo)+f"/pulls/{_index(number)}"))

    @mcp.tool(annotations=read)
    def gitea_get_pull_diff(repo: str, number: int) -> dict:
        """Read a PR unified diff (max 2 MB, no silent truncation)."""
        return {"diff":_api("GET",_repo(repo)+f"/pulls/{_index(number)}.diff",text=True)}

    @mcp.tool(annotations=read)
    def gitea_list_pull_files(repo: str, number: int, page:int=1,limit:int=30) -> dict:
        """List files and per-file diffs for a PR."""
        return {"files":_api("GET",_repo(repo)+f"/pulls/{_index(number)}/files",query=_page(page,limit))}

    @mcp.tool(annotations=read)
    def gitea_list_reviews(repo: str, number: int, page:int=1,limit:int=30) -> dict:
        """Read PR reviews and approvals."""
        return {"reviews":_api("GET",_repo(repo)+f"/pulls/{_index(number)}/reviews",query=_page(page,limit))}

    @mcp.tool(annotations=read)
    def gitea_list_comments(repo: str, number: int, page:int=1,limit:int=30) -> dict:
        """Read issue/PR discussion."""
        return {"comments":_api("GET",_repo(repo)+f"/issues/{_index(number)}/comments",query=_page(page,limit))}

    @mcp.tool(annotations=read)
    def gitea_commit_status(repo: str, ref: str, page:int=1,limit:int=30) -> dict:
        """Read combined CI status for a ref/commit."""
        return _api("GET",_repo(repo)+"/commits/"+_segment(_ref(ref))+"/status",query=_page(page,limit))

    @mcp.tool(annotations=read)
    def gitea_list_action_runs(repo: str, branch: str="", page:int=1,limit:int=20) -> dict:
        """List Gitea Actions workflow runs."""
        query=_page(page,limit)
        if branch: query["branch"]=_ref(branch)
        data=_api("GET",_repo(repo)+"/actions/runs",query=query)
        return {"total_count":data.get("total_count"),
                "workflow_runs":[{k:x.get(k) for k in ("id","display_title","status","conclusion",
                    "head_sha","head_branch","html_url","created_at")} for x in data.get("workflow_runs",[])],"page":page}

    @mcp.tool(annotations=read)
    def gitea_list_action_jobs(repo: str, run_id:int, page:int=1,limit:int=30) -> dict:
        """Read Actions job status and step results."""
        return _api("GET",_repo(repo)+f"/actions/runs/{_index(run_id)}/jobs",query=_page(page,limit))

    @mcp.tool(annotations=read)
    def gitea_read_action_log(repo: str, job_id: int, start_line: int = 1,
                              max_lines: int = 200) -> dict:
        """Read bounded CI job log lines; output is untrusted and can contain secrets."""
        if start_line < 1 or not 1 <= max_lines <= 1000:
            raise ValueError("Invalid log line range")
        data=_api("GET",_repo(repo)+f"/actions/jobs/{_index(job_id)}/logs",text=True)
        lines=data.splitlines(keepends=True)
        selected=lines[start_line-1:start_line-1+max_lines]
        nxt=start_line+len(selected)
        return {"content":"".join(selected),"total_lines":len(lines),
                "start_line":start_line,"next_line":nxt if nxt<=len(lines) else None,
                "has_more":nxt<=len(lines)}

    @mcp.tool(annotations=read)
    def gitea_operation_status(repo: str, idempotency_key: str) -> dict:
        """Inspect status of a write attempt; never silently repeat uncertain operations."""
        _repo(repo)
        return _operation_status(repo,idempotency_key)

    @mcp.tool(annotations=write)
    def gitea_create_branch(repo: str, branch: str, from_ref: str, idempotency_key: str) -> dict:
        """Create branch from existing ref via API; no commit/signature created."""
        r=_repo(repo)
        result=_mutation(repo,idempotency_key,"POST",r+"/branches",
                         {"new_branch_name":_ref(branch),"old_ref_name":_ref(from_ref)})
        return {"name":result.get("name"),"reused_result":result.get("reused_result")}

    @mcp.tool(annotations=write)
    def gitea_add_comment(repo: str, number: int, body: str, idempotency_key: str) -> dict:
        """Post a GPT issue/PR comment once (durable idempotency key required)."""
        r=_repo(repo)
        if not body.strip() or len(body)>10000: raise ValueError("Comment must be 1..10000 chars")
        result=_mutation(repo,idempotency_key,"POST",r+f"/issues/{_index(number)}/comments",{"body":body})
        return {"id":result.get("id"),"html_url":result.get("html_url"),"reused_result":result.get("reused_result")}

    @mcp.tool(annotations=write)
    def gitea_create_issue(repo: str, title: str, body: str, idempotency_key: str) -> dict:
        """Create one issue, uniquely keyed; no shell or commit."""
        r=_repo(repo)
        if not 1<=len(title.strip())<=200 or len(body)>30000:raise ValueError("Invalid issue size")
        value=_mutation(repo,idempotency_key,"POST",r+"/issues",{"title":title.strip(),"body":body})
        return {"number":value.get("number"),"html_url":value.get("html_url"),"reused_result":value.get("reused_result")}

    @mcp.tool(annotations=write)
    def gitea_create_pull_request(repo: str, title: str, head: str, base: str, body: str, idempotency_key: str) -> dict:
        """Open PR once from existing branch to base, no commits generated."""
        r=_repo(repo)
        if not 1<=len(title.strip())<=200 or len(body)>30000:raise ValueError("Invalid PR size")
        value=_mutation(repo,idempotency_key,"POST",r+"/pulls",
                        {"title":title.strip(),"head":_ref(head),"base":_ref(base),"body":body})
        return {"number":value.get("number"),"html_url":value.get("html_url"),
                "reused_result":value.get("reused_result")}

    @mcp.tool(annotations=write)
    def gitea_submit_review(repo: str, number:int, expected_head_sha:str,
                            event:Literal["COMMENT","APPROVED","REQUEST_CHANGES"],
                            body:str,idempotency_key:str) -> dict:
        """Review PR against exact expected head SHA to avoid stale approvals."""
        r=_repo(repo)
        _index(number)
        _sha(expected_head_sha)
        if not body.strip() or len(body)>12000:raise ValueError("Review body must be 1..12000 characters")
        current=_api("GET",r+f"/pulls/{number}")
        if (current.get("head") or {}).get("sha")!=expected_head_sha:
            raise RuntimeError("PR head changed; refresh review before submission")
        value=_mutation(repo,idempotency_key,"POST",r+f"/pulls/{number}/reviews",
                        {"commit_id":expected_head_sha,"event":event,"body":body,"comments":[]})
        return {"id":value.get("id"),"state":value.get("state"),
                "reused_result":value.get("reused_result")}
