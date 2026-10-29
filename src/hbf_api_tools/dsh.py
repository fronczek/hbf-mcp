"""Bounded adapter for the established DSH Harness HTTPS helper.

The helper owns Harness authentication. This module never opens SSH or accesses
the DSH host filesystem. Configure HBF_DSH_HELPER to its absolute path.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from pathlib import Path

ISSUE = re.compile(r"^ESE-[1-9][0-9]{0,6}$")
DISPATCH = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
DEFAULT_WORKDIR = "/home/dsh/esekretariat"
DEFAULT_MAX_USD = 0.20
NORMAL_MAX_USD = 0.50
OWNER_MAX_USD = 1.00


class DshError(RuntimeError):
    pass


def _helper() -> Path:
    raw = os.environ.get("HBF_DSH_HELPER") or str(Path.home() / "bin/dsh-remote")
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise DshError("Configured DSH helper is unavailable or not executable")
    return path


def _run(args: list[str], *, input_text: str | None = None, timeout: int = 45) -> str:
    try:
        result = subprocess.run(args, input=input_text, text=True, capture_output=True,
                                timeout=timeout, check=False, shell=False)
    except subprocess.TimeoutExpired:
        raise DshError("DSH helper timed out; inspect session state before retrying") from None
    except OSError:
        raise DshError("DSH helper could not be started") from None
    if result.returncode:
        # Do not expose helper stderr: it could contain prompt content or credential details.
        raise DshError(f"DSH helper failed with exit status {result.returncode}")
    if len(result.stdout) > 160_000:
        raise DshError("DSH output exceeds the configured bound")
    return result.stdout


def sessions(limit: int = 10) -> dict:
    if not 1 <= limit <= 25:
        raise ValueError("Limit must be 1..25")
    obj = json.loads(_run([str(_helper()), "sessions", "--brief-limit", str(limit)]))
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list) or len(items) > limit:
        raise DshError("Unexpected bounded session response")
    return {"sessions": [{"session": x.get("sessionId"),
                           "dispatch": _dispatch_from_session(x.get("sessionId")),
                           "running": x.get("running"), "cwd": x.get("cwd"),
                           "updated": x.get("updatedAt")} for x in items],
            "count_shown": len(items), "has_more": bool(obj.get("has_more"))}


def _dispatch_from_session(session_id: object) -> str | None:
    prefix = "gpt-flash-"
    return session_id[len(prefix):] if isinstance(session_id, str) and session_id.startswith(prefix) else None


def dispatch_id(issue: str, task: str, workdir: str) -> str:
    normalized = "\n".join(line.rstrip() for line in task.strip().splitlines())
    payload = json.dumps([issue, os.path.normpath(workdir), normalized],
                         ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{issue.lower()}-{digest}"


def dispatch_status(identifier: str) -> dict:
    if not DISPATCH.fullmatch(identifier):
        raise ValueError("Invalid DSH dispatch ID")
    obj = json.loads(_run([str(_helper()), "dispatch-status", identifier]))
    if not isinstance(obj, dict) or obj.get("dispatch") != identifier:
        raise DshError("Unexpected DSH dispatch status response")
    return obj


def flash_task(issue: str, task: str, *, workdir: str = DEFAULT_WORKDIR,
               max_usd: float = DEFAULT_MAX_USD,
               timeout_seconds: int = 180) -> dict:
    if not ISSUE.fullmatch(issue):
        raise ValueError("Expected a valid ESE issue ID")
    if not 15 <= len(task) <= 8_000:
        raise ValueError("Task must contain 15..8000 characters")
    owner_override = os.environ.get("DSH_BUDGET_OVERRIDE") == "1"
    ceiling = OWNER_MAX_USD if owner_override else NORMAL_MAX_USD
    if not 0.001 <= max_usd <= ceiling:
        suffix = " with explicit owner override" if owner_override else ""
        raise ValueError(f"Task budget must be USD 0.001..{ceiling:.2f}{suffix}")
    if not 30 <= timeout_seconds <= 600:
        raise ValueError("Timeout must be 30..600 seconds")
    if not workdir or not Path(workdir).is_absolute():
        raise ValueError("DSH workdir must be an explicit absolute path")
    allowed = os.environ.get("HBF_DSH_ALLOWED_WORKDIRS", DEFAULT_WORKDIR)
    roots = tuple(Path(os.path.normpath(item.strip())) for item in allowed.split(os.pathsep) if item.strip())
    target = Path(os.path.normpath(workdir))
    if not roots or not any(target == root or root in target.parents for root in roots):
        raise ValueError("DSH workdir is outside HBF_DSH_ALLOWED_WORKDIRS")
    identifier = dispatch_id(issue, task, workdir)
    prompt = (f"Work on {issue} only. Do not spawn subagents or delegate any part of the task. "
              "Before editing, fetch the target repository, inspect status and current refs, "
              "and preserve all existing work. Never pull into a dirty or ambiguous checkout; "
              "use a separate task branch/worktree when needed. Follow AGENTS.md and repository "
              "rules. Do not use GPT credentials, SSH into other hosts, access another agent "
              "identity, merge a PR, or expand the assigned scope. Stop after the bounded "
              "deliverable and report changed files, tests, branch, commits, PR, blockers, and "
              f"dispatch ID {identifier} succinctly.\n\n" + task)
    raw = _run([str(_helper()), "flash", "-", "--cwd", workdir,
                "--timeout", str(timeout_seconds), "--max-usd", str(max_usd),
                "--effort", "low", "--dispatch-id", identifier],
               input_text=prompt, timeout=timeout_seconds + 65)
    marker = "---- assistant result ----"
    answer = raw.split(marker, 1)[-1].strip() if marker in raw else ""
    def capture(pattern: str):
        m = re.search(pattern, raw, re.M)
        return m.group(1) if m else None
    if len(answer) <= 6000:
        excerpt = answer
    else:
        excerpt = answer[:3000] + "\n...[middle truncated]...\n" + answer[-3000:]
    return {"issue": issue, "dispatch": identifier,
            "session": capture(r"^session:\s*(\S+)"),
            "tokens": capture(r"^tokens:\s*(.+)$"),
            "cost_estimate": capture(r"^estimated USD cost:\s*(.+)$"),
            "result_excerpt": excerpt, "result_truncated": len(answer) > 6000}
