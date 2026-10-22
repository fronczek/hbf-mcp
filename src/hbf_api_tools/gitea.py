"""Typed Gitea operations restricted to git.hbf.pl and an exact repo allowlist."""
from __future__ import annotations

import os
import re
import urllib.parse

from .config import ConfigurationError, load_scalar_yaml
from .http import request_json, request_raw

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_REPOS = ("secnet/ese-agent", "secnet/ese-backend", "secnet/ese-frontend",
                 "secnet/ese-ansible", "hbf/hbf-api-tools")


class Gitea:
    def __init__(self):
        cfg = load_scalar_yaml("gitea.yml")
        self.base = cfg.get("gitea_url", "").rstrip("/")
        self.token = cfg.get("token_value", "")
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != "https" or parsed.netloc != "git.hbf.pl"
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ConfigurationError("Gitea credential must use https://git.hbf.pl")
        if not self.token:
            raise ConfigurationError("Gitea token is missing")
        configured = os.environ.get("HBF_GITEA_REPOSITORIES", "")
        self.repositories = frozenset(x.strip() for x in configured.split(",") if x.strip()) if configured else frozenset(DEFAULT_REPOS)
        if not self.repositories or any(not REPO_RE.fullmatch(x) or
                any(p in (".", "..") for p in x.split("/")) for x in self.repositories):
            raise ConfigurationError("Invalid exact Gitea repository allowlist")

    def _repo(self, name: str) -> str:
        if name not in self.repositories:
            raise ValueError("Repository is outside the configured allowlist")
        return "/repos/" + "/".join(urllib.parse.quote(p, safe="") for p in name.split("/"))

    def _call(self, route: str, query: dict | None = None, *, method: str = "GET",
              payload: dict | None = None):
        return request_json(self.base, "/api/v1" + route, self.token,
                            method=method, query=query, payload=payload,
                            auth_scheme="token")

    @staticmethod
    def _ref(value: str, label: str) -> str:
        if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,150}", value)
                or value.startswith("/") or ".." in value.split("/")):
            raise ValueError(f"Invalid {label} ref")
        return value

    def _confirm_gpt(self) -> None:
        user = self._call("/user")
        if user.get("login") != "gpt":
            raise ConfigurationError("Refusing Gitea mutation: credential is not the GPT identity")

    def connection(self):
        result = self._call("/user")
        return {"login": result.get("login"), "active": bool(result.get("active", True))}

    def repository(self, repo: str):
        return self._call(self._repo(repo))

    def branches(self, repo: str, page: int = 1, limit: int = 30):
        if page < 1 or not 1 <= limit <= 100:
            raise ValueError("Invalid pagination bounds")
        return self._call(self._repo(repo) + "/branches", {"page": page, "limit": limit})

    def pull_requests(self, repo: str, state: str = "open", page: int = 1, limit: int = 20):
        if state not in {"open", "closed", "all"} or page < 1 or not 1 <= limit <= 100:
            raise ValueError("Invalid state or pagination bounds")
        return self._call(self._repo(repo) + "/pulls", {"state": state, "page": page, "limit": limit})

    def pull_request(self, repo: str, number: int):
        if number < 1:
            raise ValueError("PR number must be positive")
        return self._call(self._repo(repo) + f"/pulls/{number}")

    def pull_diff(self, repo: str, number: int) -> str:
        if number < 1:
            raise ValueError("PR number must be positive")
        raw = request_raw(self.base, "/api/v1" + self._repo(repo) + f"/pulls/{number}.diff",
                          self.token, auth_scheme="token", accept="text/plain")
        return raw.decode("utf-8", errors="replace")

    def pull_files(self, repo: str, number: int, page: int = 1, limit: int = 30):
        if number < 1 or page < 1 or not 1 <= limit <= 100:
            raise ValueError("Invalid PR or pagination bounds")
        return self._call(self._repo(repo) + f"/pulls/{number}/files",
                          {"page": page, "limit": limit})

    def create_pull_request(self, repo: str, title: str, head: str, base: str,
                            body: str) -> dict:
        route = self._repo(repo)
        head, base = self._ref(head, "head"), self._ref(base, "base")
        title, body = title.strip(), body.strip()
        if not 1 <= len(title) <= 200 or len(body) > 20_000:
            raise ValueError("PR title must be 1..200 characters and body at most 20000")
        self._confirm_gpt()
        # Inspect all pages up to a fixed ceiling before writing. On a timeout or
        # other ambiguous outcome, inspect again before retrying the operation.
        for page in range(1, 11):
            existing = self._call(route + "/pulls", {"state": "all", "page": page, "limit": 100})
            if not isinstance(existing, list):
                raise RuntimeError("Gitea returned an invalid PR list")
            for item in existing:
                source = item.get("head") or {}
                target = item.get("base") or {}
                source_repo = source.get("repo") or {}
                if (source.get("ref") == head and target.get("ref") == base
                        and source_repo.get("full_name") == repo):
                    return {"created": False, "existing": True, "number": item.get("number"),
                            "state": item.get("state"), "url": item.get("html_url"),
                            "head_sha": (source.get("sha"))}
            if len(existing) < 100:
                break
        else:
            raise RuntimeError("PR duplicate check reached its page limit; refusing to create")
        result = self._call(route + "/pulls", method="POST", payload={
            "title": title, "head": head, "base": base, "body": body,
            "maintainer_can_modify": False,
        })
        user = result.get("user") or {}
        if user.get("login") != "gpt":
            raise RuntimeError("Gitea did not confirm GPT as the PR author")
        return {"created": True, "number": result.get("number"),
                "state": result.get("state"), "url": result.get("html_url"),
                "head_sha": (result.get("head") or {}).get("sha")}

    def commit_status(self, repo: str, ref: str):
        if not re.fullmatch(r"[A-Fa-f0-9]{7,64}|[A-Za-z0-9._/-]{1,150}", ref) or ".." in ref:
            raise ValueError("Invalid commit/ref")
        data = self._call(self._repo(repo) + "/commits/" + urllib.parse.quote(ref, safe="") + "/status")
        statuses = data.get("statuses", [])
        if not isinstance(statuses, list):
            raise RuntimeError("Gitea returned an invalid status list")
        return {
            "state": data.get("state"),
            "sha": data.get("sha"),
            "total_count": data.get("total_count", len(statuses)),
            "statuses": [{key: item.get(key) for key in
                          ("id", "status", "context", "description", "target_url", "created_at")}
                         for item in statuses[:50] if isinstance(item, dict)],
            "statuses_truncated": len(statuses) > 50,
        }
