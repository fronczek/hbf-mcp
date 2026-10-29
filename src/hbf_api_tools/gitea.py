"""Typed Gitea operations restricted to git.hbf.pl and an exact repo allowlist."""
from __future__ import annotations

import os
import re
import urllib.parse

from .config import ConfigurationError, load_scalar_yaml
from .http import request_json, request_raw
from .identity import acting_agent, add_attribution

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_REPOS = ("secnet/ese-agent", "secnet/ese-backend", "secnet/ese-frontend",
                 "secnet/ese-ansible", "hbf/hbf-api-tools")


class Gitea:
    def __init__(self, *, identity_provider=None, attribution_provider=None):
        # Internal dependency injection for MCP service identities; CLI defaults stay host-bound.
        self._identity_provider = identity_provider or acting_agent
        self._attribution_provider = attribution_provider or add_attribution
        cfg = load_scalar_yaml("gitea.yml")
        self.base = cfg.get("gitea_url", "").rstrip("/")
        self.token = cfg.get("token_value", "")
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != "https" or parsed.netloc != "git.hbf.pl"
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ConfigurationError("Gitea credential must use https://git.hbf.pl")
        if not self.token:
            raise ConfigurationError("Gitea token is missing")
        self._identity_verified = False
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
        # Before EVERY API operation (including reads), verify that the token
        # belongs to this host's designated agent. Cache only within this client.
        if route != "/user" and not self._identity_verified:
            self._call("/user")
        result = request_json(self.base, "/api/v1" + route, self.token,
                              method=method, query=query, payload=payload,
                              auth_scheme="token")
        if route == "/user":
            if result.get("login") != self._identity_provider():
                raise ConfigurationError("Gitea token does not match local agent identity")
            self._identity_verified = True
        return result

    @staticmethod
    def _ref(value: str, label: str) -> str:
        if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,150}", value)
                or value.startswith("/") or ".." in value.split("/")):
            raise ValueError(f"Invalid {label} ref")
        return value

    def _confirm_agent(self) -> str:
        actor = self._identity_provider()
        user = self._call("/user")
        if user.get("login") != actor:
            raise ConfigurationError("Refusing Gitea mutation: token does not match local agent identity")
        return actor

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
        if not self._identity_verified:
            self._call("/user")
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
        actor = self._confirm_agent()
        body = self._attribution_provider(body, actor)
        if len(body) > 20_000:
            raise ValueError("PR body with agent attribution exceeds the limit")
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
        if user.get("login") != actor:
            raise RuntimeError("Gitea did not confirm acting agent as the PR author")
        return {"created": True, "number": result.get("number"),
                "state": result.get("state"), "url": result.get("html_url"),
                "head_sha": (result.get("head") or {}).get("sha")}

    def mark_manually_merged(self, repo: str, number: int, *,
                             expected_head_sha: str, merge_commit_sha: str) -> dict:
        """Mark an ALREADY signed/pushed manual merge in Gitea without writing Git.

        Requires exact PR HEAD, master tip, and a two-parent merge that includes
        the reviewed PR head. No generic merge or arbitrary status change.
        Do not retry after an ambiguous POST; inspect the PR first.
        """
        if number < 1 or not all(re.fullmatch(r"[a-f0-9]{40}", x)
                                  for x in (expected_head_sha, merge_commit_sha)):
            raise ValueError("A PR number and two exact SHA-1 commit IDs are required")
        self._confirm_agent()
        pr = self.pull_request(repo, number)
        if pr.get("merged") is True:
            return {"number": number, "merged": True, "already_merged": True}
        if (pr.get("state") != "open"
                or (pr.get("head") or {}).get("sha") != expected_head_sha
                or (pr.get("base") or {}).get("ref") != "master"):
            raise RuntimeError("PR state, expected head or base has changed; refusing")
        tip = self._call(self._repo(repo) + "/branches/master")
        remote_master = (tip.get("commit") or {}).get("id")
        if remote_master != merge_commit_sha:
            raise RuntimeError("Signed merge is not the current remote master HEAD")
        merge_commit = self._call(self._repo(repo) + "/git/commits/" + merge_commit_sha)
        parents = [parent.get("sha") for parent in merge_commit.get("parents", [])]
        if len(parents) != 2 or parents[1] != expected_head_sha:
            raise RuntimeError("Verified PR head is not second parent of remote merge commit")
        status = self.commit_status(repo, expected_head_sha)
        if status.get("state") != "success":
            raise RuntimeError("PR HEAD does not have completely successful CI checks")
        self._call(self._repo(repo) + f"/pulls/{number}/merge", method="POST", payload={
            "Do": "manually-merged", "MergeCommitID": merge_commit_sha,
        })
        updated = self.pull_request(repo, number)
        if updated.get("merged") is not True or updated.get("state") != "closed":
            raise RuntimeError("Gitea did not confirm manually merged PR; inspect before retry")
        return {"number": number, "merged": True, "already_merged": False,
                "merge_commit_sha": merge_commit_sha}

    def close_integrated_pr(self, repo: str, number: int, *,
                            expected_head_sha: str, merge_commit_sha: str,
                            dsh_tier: str | None = None) -> dict:
        """Close a PR after signed Git merge when Gitea disables manual-merge marking.

        NOT a Gitea `merged` status: post an audit explanation first, then close.
        Never call this for a PR not present as the direct second parent of the
        current remote master. Does not mutate any source code or merge itself.
        """
        if number < 1 or not all(re.fullmatch(r"[a-f0-9]{40}", x)
                                  for x in (expected_head_sha, merge_commit_sha)):
            raise ValueError("A PR number and two exact SHA-1 commit IDs are required")
        actor = self._confirm_agent()
        if actor == "dsh" and dsh_tier not in {"dsh-pro", "dsh-flash"}:
            raise ValueError("DSH must explicitly identify the model tier")
        if actor == "gpt" and dsh_tier is not None:
            raise ValueError("GPT must not claim a DSH model tier")
        pr = self.pull_request(repo, number)
        if pr.get("merged") is True:
            return {"number": number, "already_merged": True, "merged_in_git": True}
        if (pr.get("state") != "open"
                or (pr.get("head") or {}).get("sha") != expected_head_sha
                or (pr.get("base") or {}).get("ref") != "master"):
            raise RuntimeError("PR state, expected head or base has changed; refusing")
        tip = self._call(self._repo(repo) + "/branches/master")
        if (tip.get("commit") or {}).get("id") != merge_commit_sha:
            raise RuntimeError("Signed merge is not the current remote master HEAD")
        commit = self._call(self._repo(repo) + "/git/commits/" + merge_commit_sha)
        parents = [parent.get("sha") for parent in commit.get("parents", [])]
        if len(parents) != 2 or parents[1] != expected_head_sha:
            raise RuntimeError("PR head is not the second parent of this manual merge")
        if self.commit_status(repo, expected_head_sha).get("state") != "success":
            raise RuntimeError("PR HEAD CI is not green")

        audit = ("Code was integrated into master by an independently signed Git merge "
                 f"{merge_commit_sha} (exact PR head: {expected_head_sha}). "
                 "This repository currently disables Gitea's manually-merged status "
                 "endpoint (HTTP 405), so this PR is closed as administrative cleanup "
                 "rather than marked merged by Gitea. No additional code merge is needed.")
        if actor == "dsh":
            audit += f"\n\nadded by {dsh_tier}"
        audit = self._attribution_provider(audit, actor)
        self._call(self._repo(repo) + f"/issues/{number}/comments", method="POST",
                   payload={"body": audit})
        # If either request is ambiguous, inspect remotely before considering a retry.
        self._call(self._repo(repo) + f"/pulls/{number}", method="PATCH",
                   payload={"state": "closed"})
        updated = self.pull_request(repo, number)
        if updated.get("state") != "closed":
            raise RuntimeError("Gitea did not confirm close; inspect before retrying")
        return {"number": number, "merged_in_git": True,
                "gitea_state": "closed", "gitea_merged_flag": bool(updated.get("merged")),
                "merge_commit_sha": merge_commit_sha}

    def merge_pull_request(self, repo: str, number: int, expected_head_sha: str,
                           delete_branch: bool = True) -> dict:
        if number < 1:
            raise ValueError("PR number must be positive")
        if not re.fullmatch(r"[A-Fa-f0-9]{40}", expected_head_sha):
            raise ValueError("Expected PR head must be a full 40-character SHA")
        route = self._repo(repo)
        self._confirm_agent()
        pr = self.pull_request(repo, number)
        head = pr.get("head") or {}
        if head.get("sha") != expected_head_sha:
            raise ValueError("PR head changed; refusing to merge")
        if (pr.get("base") or {}).get("ref") != "master":
            raise ValueError("Only PRs targeting master can be merged by this operation")
        if pr.get("merged") is True:
            return {"merged": True, "already_merged": True,
                    "number": number, "head_sha": expected_head_sha,
                    "merge_commit_sha": pr.get("merged_commit_sha")}
        if pr.get("state") != "open":
            raise ValueError("PR is not open")
        if pr.get("mergeable") is not True:
            raise ValueError("PR is not confirmed mergeable")
        status = self.commit_status(repo, expected_head_sha)
        statuses = status.get("statuses") or []
        if (status.get("state") != "success" or not statuses
                or status.get("statuses_truncated")
                or len(statuses) != status.get("total_count")
                or any(item.get("status") != "success" for item in statuses)):
            raise ValueError("Exact-head CI is not fully successful; refusing to merge")
        result = self._call(route + f"/pulls/{number}/merge", method="POST", payload={
            "do": "fast-forward-only",
            "head_commit_id": expected_head_sha,
            "delete_branch_after_merge": bool(delete_branch),
        })
        merged = self.pull_request(repo, number)
        if (merged.get("merged") is not True
                or (merged.get("head") or {}).get("sha") != expected_head_sha):
            raise RuntimeError("Gitea did not confirm the expected PR merge")
        return {"merged": True, "already_merged": False, "number": number,
                "head_sha": expected_head_sha, "merge_commit_sha": merged.get("merged_commit_sha")}

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
