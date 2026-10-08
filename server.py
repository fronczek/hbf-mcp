#!/usr/bin/env python3
"""HBF MCP bridge.

A small MCP server exposing a deliberately limited administrative surface for a
single-purpose bridge VM.  It is *not* a privilege boundary: ``shell`` executes
Bash with the ordinary permissions of the MCP service account.  The service
account is unprivileged and has no sudo; this server never escalates privileges
and never claims to.

Tools that only observe the machine are annotated ``readOnlyHint=True`` so that
MCP clients can treat them as safe to invoke without confirmation.  Tools that
can modify user-accessible state are annotated ``readOnlyHint=False``.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations


NAME = os.getenv("HBF_MCP_NAME", "HBF MCP - Artur")
HOST = os.getenv("HBF_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("HBF_MCP_PORT", "8765"))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _split_paths(value: str) -> tuple[Path, ...]:
    """Split an os.pathsep-separated list of paths and canonicalise each one."""
    parts = [part.strip() for part in value.split(os.pathsep) if part.strip()]
    return tuple(Path(os.path.realpath(os.path.expanduser(part))) for part in parts)


def _split_log_files(value: str) -> dict[str, str]:
    """Parse ``service=/path/to/file`` pairs separated by commas."""
    mapping: dict[str, str] = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        service, path = chunk.split("=", 1)
        service = service.strip()
        path = path.strip()
        if service and path:
            mapping[service] = path
    return mapping


@dataclass(frozen=True)
class Config:
    """Runtime limits and allowlists, derived from the environment once."""

    name: str
    default_cwd: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    # Files (or directories) whose *contents* may only be read through the
    # dedicated credential tool.  Empty by default: credential access must be
    # granted deliberately.
    secret_read_allowlist: tuple[Path, ...]
    allowed_log_services: tuple[str, ...]
    log_files: dict[str, str] = field(default_factory=dict)
    boiler_helper: Path = Path("/home/gptagent/bin/kuk-boiler-watch")
    max_output: int = 512 * 1024
    shell_timeout: int = 120
    command_timeout: int = 30
    max_log_lines: int = 1000
    max_log_bytes: int = 256 * 1024


def load_config() -> Config:
    default_cwd = Path(
        os.path.realpath(os.path.expanduser(os.getenv("HBF_MCP_DEFAULT_CWD", "/home/gptagent")))
    )
    read_roots = _split_paths(
        os.getenv("HBF_MCP_READ_ROOTS", str(default_cwd))
    ) or (default_cwd,)
    write_roots = _split_paths(
        os.getenv("HBF_MCP_WRITE_ROOTS", str(default_cwd))
    ) or (default_cwd,)

    return Config(
        name=NAME,
        default_cwd=default_cwd,
        read_roots=read_roots,
        write_roots=write_roots,
        secret_read_allowlist=_split_paths(os.getenv("HBF_MCP_SECRET_READ_ALLOWLIST", "")),
        allowed_log_services=tuple(
            item.strip()
            for item in os.getenv("HBF_MCP_ALLOWED_LOG_SERVICES", "hbf-mcp").split(",")
            if item.strip()
        ),
        log_files=_split_log_files(os.getenv("HBF_MCP_LOG_FILES", "")),
        boiler_helper=Path(
            os.path.realpath(
                os.path.expanduser(
                    os.getenv(
                        "HBF_MCP_BOILER_HELPER", str(default_cwd / "bin" / "kuk-boiler-watch")
                    )
                )
            )
        ),
        max_output=int(os.getenv("HBF_MCP_MAX_OUTPUT", str(512 * 1024))),
        shell_timeout=int(os.getenv("HBF_MCP_SHELL_TIMEOUT", "120")),
        command_timeout=int(os.getenv("HBF_MCP_COMMAND_TIMEOUT", "30")),
        max_log_lines=int(os.getenv("HBF_MCP_MAX_LOG_LINES", "1000")),
        max_log_bytes=int(os.getenv("HBF_MCP_MAX_LOG_BYTES", str(256 * 1024))),
    )


CONFIG = load_config()


# ---------------------------------------------------------------------------
# Tool annotations
# ---------------------------------------------------------------------------

def read_only_annotations(*, open_world: bool = False) -> ToolAnnotations:
    """Annotation set for tools that only observe the machine."""
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=open_world,
    )


def mutating_annotations(*, open_world: bool = False) -> ToolAnnotations:
    """Annotation set for tools that can change user-accessible state."""
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=open_world,
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PathAccessError(PermissionError):
    """Raised when a path is outside every configured allowlist."""


class SecretAccessError(PathAccessError):
    """Raised when a credential path is used outside the credential tool."""


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------

_SECRET_NAME_RE = re.compile(
    r"("
    r"^\.env(\.|$)"
    r"|^\.netrc$"
    r"|^\.pgpass$"
    r"|^\.my\.cnf$"
    r"|^\.git-credentials$"
    r"|^id_(rsa|dsa|ecdsa|ed25519)$"
    r"|\.(pem|key|p12|pfx|kdbx|jks|keystore)$"
    r"|(secret|credential|password|passwd|token|api[_-]?key)"
    r")",
    re.IGNORECASE,
)

_SECRET_DIR_NAMES = {".ssh", ".gnupg", ".aws"}


def resolve_path(path: str | None) -> Path:
    """Resolve relative paths against the agent's default working directory."""
    if not path:
        return CONFIG.default_cwd

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate

    return CONFIG.default_cwd / candidate


def canonical(path: Path) -> Path:
    """Return the fully canonical path, resolving ``..`` and symlinks.

    ``os.path.realpath`` deliberately resolves even when the final component
    does not exist yet, so a not-yet-created file cannot be redirected outside
    an allowlist through a symlinked parent.
    """
    return Path(os.path.realpath(str(path)))


def is_secret_path(path: Path) -> bool:
    """Heuristically decide whether a path holds credentials."""
    resolved = canonical(path)
    if resolved.name and _SECRET_NAME_RE.search(resolved.name):
        return True
    return any(part.lower() in _SECRET_DIR_NAMES for part in resolved.parts)


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(canonical(root))
            return True
        except ValueError:
            continue
    return False


def ensure_allowed(
    path: Path,
    roots: tuple[Path, ...],
    *,
    purpose: str,
) -> Path:
    """Canonicalise *path* and require it to live under one of *roots*."""
    resolved = canonical(path)
    if not _within(resolved, roots):
        allowed = ", ".join(str(root) for root in roots) or "(none configured)"
        raise PathAccessError(
            f"{purpose} denied: {path} resolves to {resolved}, which is outside "
            f"the allowed roots ({allowed})"
        )
    return resolved


def ensure_normal_read(path: Path) -> Path:
    """Resolve a path for ordinary ``read_file``/``list_dir`` access."""
    resolved = canonical(path)
    if is_secret_path(resolved):
        raise SecretAccessError(
            f"credential access denied: {resolved} looks like a secret. "
            "Reading credentials requires an explicitly allowlisted path via "
            "read_secret_file (HBF_MCP_SECRET_READ_ALLOWLIST)."
        )
    return ensure_allowed(resolved, CONFIG.read_roots, purpose="read")


def ensure_write(path: Path) -> Path:
    """Resolve a path for ``write_file`` access."""
    resolved = canonical(path)
    if is_secret_path(resolved):
        raise SecretAccessError(
            f"write denied: {resolved} looks like a credential path and is "
            "never written through the file tools"
        )
    return ensure_allowed(resolved, CONFIG.write_roots, purpose="write")


def ensure_secret_read(path: Path) -> Path:
    """Resolve a path that was deliberately granted through the allowlist."""
    resolved = canonical(path)
    allowlist = CONFIG.secret_read_allowlist
    if not allowlist:
        raise SecretAccessError(
            "credential access denied: no credentialed path has been granted "
            "(HBF_MCP_SECRET_READ_ALLOWLIST is empty)"
        )
    for allowed in allowlist:
        allowed = canonical(allowed)
        if resolved == allowed:
            return resolved
        try:
            resolved.relative_to(allowed)
            return resolved
        except ValueError:
            continue
    raise SecretAccessError(
        f"credential access denied: {resolved} is not in the explicit "
        "credential allowlist"
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def truncate(value: str, limit: int | None = None) -> str:
    """Limit returned tool output so an accidental huge result stays usable."""
    limit = CONFIG.max_output if limit is None else limit
    data = value.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return value

    clipped = data[:limit].decode("utf-8", errors="replace")
    return clipped + f"\n\n[output truncated at {limit} bytes]"


def _run_readonly(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a read-only helper command with a hard timeout."""
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout or CONFIG.command_timeout,
    )


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    NAME,
    instructions=(
        "This is Artur's single-purpose administrative bridge VM. The MCP "
        "service account is unprivileged: it has no sudo and this server "
        "provides no privilege escalation. The shell tool runs Bash with "
        "ordinary user permissions and can modify files the account owns. "
        "File tools are restricted to configured directories, and credential "
        "files require a separately granted allowlist. Prefer inspecting state "
        "before changing it, and never print secrets unless the user "
        "explicitly asks to see them."
    ),
)


@mcp.tool(annotations=read_only_annotations())
def info() -> dict[str, Any]:
    """Return basic information about this MCP bridge and its limits."""
    return {
        "name": NAME,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "service_cwd": os.getcwd(),
        "default_cwd": str(CONFIG.default_cwd),
        "home": str(Path.home()),
        "read_roots": [str(root) for root in CONFIG.read_roots],
        "write_roots": [str(root) for root in CONFIG.write_roots],
        "secret_read_allowlist": [str(p) for p in CONFIG.secret_read_allowlist],
        "privilege_escalation": "none (the service account is not a sudoer)",
        "max_output_bytes": CONFIG.max_output,
        "shell_timeout_seconds": CONFIG.shell_timeout,
    }


@mcp.tool(annotations=read_only_annotations())
def get_hostname() -> dict[str, Any]:
    """Return the host name without starting a shell."""
    return {
        "hostname": socket.gethostname(),
        "node": platform.node(),
    }


@mcp.tool(annotations=read_only_annotations())
def get_time() -> dict[str, Any]:
    """Return the local time with its time zone, plus the UTC time."""
    now = dt.datetime.now().astimezone()
    offset = now.utcoffset() or dt.timedelta()
    return {
        "timezone": str(now.tzinfo),
        "utc_offset": now.strftime("%z"),
        "utc_offset_seconds": int(offset.total_seconds()),
        "local_iso": now.isoformat(),
        "utc_iso": now.astimezone(dt.timezone.utc).isoformat(),
        "unix": int(now.timestamp()),
    }


@mcp.tool(annotations=read_only_annotations())
def system_status() -> dict[str, Any]:
    """Return read-only host statistics: uptime, RAM, disk and load average."""
    return {
        "uptime_seconds": _uptime_seconds(),
        "memory": _memory_info(),
        "disk": _disk_info(),
        "load_average": _load_average(),
    }


@mcp.tool(annotations=read_only_annotations())
def read_logs(service: str, lines: int = 100) -> dict[str, Any]:
    """Read recent log lines for an allowlisted service.

    Only services named in HBF_MCP_ALLOWED_LOG_SERVICES (or explicitly mapped
    in HBF_MCP_LOG_FILES) may be read, the line count is capped, and the
    returned payload is size-limited.

    Args:
        service: Allowlisted service name, for example ``hbf-mcp``.
        lines: Number of trailing lines, capped at HBF_MCP_MAX_LOG_LINES.
    """
    if not service:
        raise ValueError("service must not be empty")

    allowed = service in CONFIG.allowed_log_services or service in CONFIG.log_files
    if not allowed:
        raise PathAccessError(
            f"log access denied: {service!r} is not allowlisted "
            f"(allowed: {', '.join(CONFIG.allowed_log_services) or '(none)'})"
        )

    requested = max(1, min(int(lines), CONFIG.max_log_lines))
    log_file = CONFIG.log_files.get(service)
    if log_file:
        content = _tail_file(Path(log_file), requested)
        return {
            "service": service,
            "source": log_file,
            "lines": requested,
            "truncated": len(content) >= CONFIG.max_log_bytes,
            "content": truncate(content),
        }

    if shutil.which("journalctl") is None:
        raise RuntimeError("journalctl is not available on this host")

    try:
        proc = _run_readonly(
            [
                "journalctl",
                "-u",
                service,
                "-n",
                str(requested),
                "--no-pager",
                "--output=short-iso",
            ],
            timeout=CONFIG.command_timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"reading logs for {service} timed out")

    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"journalctl failed: {proc.returncode}"
        )

    return {
        "service": service,
        "source": f"journalctl -u {service}",
        "lines": requested,
        "truncated": len(proc.stdout.encode("utf-8", "replace")) > CONFIG.max_output,
        "content": truncate(proc.stdout),
    }


def _uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _memory_info() -> dict[str, Any] | None:
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                number = rest.strip().split()[0]
                values[key.strip()] = int(number) * 1024
    except (OSError, ValueError, IndexError):
        return None

    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    if total is None:
        return None
    result: dict[str, Any] = {"total_bytes": total}
    if available is not None:
        result["available_bytes"] = available
        result["used_bytes"] = max(0, total - available)
    return result


def _disk_info() -> dict[str, Any]:
    result: dict[str, Any] = {}
    targets = {"/": Path("/"), "default_cwd": CONFIG.default_cwd}
    for label, target in targets.items():
        try:
            usage = shutil.disk_usage(str(target))
        except OSError:
            continue
        result[label] = {
            "path": str(target),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    return result


def _load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _tail_file(path: Path, lines: int) -> str:
    """Read the last *lines* lines of a file without loading all of it."""
    resolved = ensure_allowed(path, CONFIG.read_roots, purpose="log read")
    size = resolved.stat().st_size
    start = max(0, size - CONFIG.max_log_bytes)
    with resolved.open("rb") as handle:
        handle.seek(start)
        raw = handle.read(CONFIG.max_log_bytes)
    text = raw.decode("utf-8", errors="replace")
    if start > 0:
        text = text.split("\n", 1)[-1]
    return "\n".join(text.splitlines()[-lines:]) + "\n"


@mcp.tool(annotations=mutating_annotations(open_world=True))
def shell(
    command: str,
    cwd: str | None = None,
    timeout: int = CONFIG.shell_timeout,
) -> dict[str, Any]:
    """Execute a Bash command as the unprivileged MCP service account.

    The command runs with ordinary user permissions and may modify
    user-accessible files. No privilege escalation is provided.

    Args:
        command: Bash command to execute.
        cwd: Optional working directory. Relative paths use HBF_MCP_DEFAULT_CWD.
        timeout: Kill the command after this many seconds (max 3600).
    """
    workdir = resolve_path(cwd)

    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            cwd=str(workdir),
            text=True,
            capture_output=True,
            timeout=max(1, min(timeout, 3600)),
            env=os.environ.copy(),
        )
        return {
            "command": command,
            "cwd": str(workdir),
            "exit_code": proc.returncode,
            "stdout": truncate(proc.stdout),
            "stderr": truncate(proc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return {
            "command": command,
            "cwd": str(workdir),
            "timed_out": True,
            "timeout": timeout,
            "stdout": truncate(stdout),
            "stderr": truncate(stderr),
        }


@mcp.tool(annotations=read_only_annotations())
def read_file(path: str, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Read a text file from an allowlisted directory.

    Relative paths use HBF_MCP_DEFAULT_CWD. Symlinks and ``..`` are resolved
    before the allowlist check, so they cannot escape the allowed roots.
    Credential-looking files are refused here; use ``read_secret_file`` with an
    explicitly granted path instead. Binary data is decoded with replacement
    characters and output is truncated to max_bytes.
    """
    p = ensure_normal_read(resolve_path(path))
    limit = max(1, min(int(max_bytes), CONFIG.max_output))
    raw = p.read_bytes()
    clipped = raw[:limit]

    return {
        "path": str(p),
        "size": len(raw),
        "truncated": len(raw) > len(clipped),
        "content": clipped.decode("utf-8", errors="replace"),
    }


@mcp.tool(annotations=read_only_annotations())
def read_secret_file(path: str, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Read a credential file that was deliberately granted to this bridge.

    Unlike ``read_file``, this tool only accepts paths present in
    HBF_MCP_SECRET_READ_ALLOWLIST. That allowlist is empty by default, so
    credential access has to be granted on purpose.

    Relative paths use HBF_MCP_DEFAULT_CWD.
    """
    p = ensure_secret_read(resolve_path(path))
    limit = max(1, min(int(max_bytes), CONFIG.max_output))
    raw = p.read_bytes()
    clipped = raw[:limit]

    return {
        "path": str(p),
        "size": len(raw),
        "truncated": len(raw) > len(clipped),
        "content": clipped.decode("utf-8", errors="replace"),
    }


@mcp.tool(annotations=mutating_annotations())
def write_file(
    path: str,
    content: str,
    create_parents: bool = True,
) -> dict[str, Any]:
    """Create or replace a UTF-8 text file in an allowlisted directory.

    Relative paths use HBF_MCP_DEFAULT_CWD. Symlinks and ``..`` are resolved
    before the allowlist check, so a write cannot be redirected outside the
    write roots. Credential-looking paths are refused. Files that require
    root permissions cannot be written, because the service account has no
    privilege escalation.
    """
    p = ensure_write(resolve_path(path))

    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)

    p.write_text(content, encoding="utf-8")

    return {
        "path": str(p),
        "bytes_written": len(content.encode("utf-8")),
    }


@mcp.tool(annotations=read_only_annotations())
def list_dir(
    path: str = ".",
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """List files and directories inside an allowlisted directory.

    Relative paths use HBF_MCP_DEFAULT_CWD.
    """
    p = ensure_allowed(resolve_path(path), CONFIG.read_roots, purpose="list")

    result: list[dict[str, Any]] = []
    for item in sorted(
        p.iterdir(),
        key=lambda x: (not x.is_dir(), x.name.lower()),
    ):
        if not include_hidden and item.name.startswith("."):
            continue

        try:
            stat = item.stat()
            size = stat.st_size
        except OSError:
            size = None

        result.append(
            {
                "name": item.name,
                "path": str(item),
                "type": (
                    "dir"
                    if item.is_dir()
                    else "file"
                    if item.is_file()
                    else "other"
                ),
                "size": size,
            }
        )

    return result


@mcp.tool(annotations=read_only_annotations())
def git_status(repo: str) -> dict[str, Any]:
    """Return branch and porcelain status for an allowlisted Git repository."""
    p = ensure_allowed(resolve_path(repo), CONFIG.read_roots, purpose="git read")

    def run(*args: str) -> str:
        try:
            proc = _run_readonly(["git", "-C", str(p), *args])
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git {' '.join(args)} timed out")
        if proc.returncode != 0:
            raise RuntimeError(
                proc.stderr.strip()
                or f"git {' '.join(args)} failed: {proc.returncode}"
            )
        return proc.stdout.strip()

    return {
        "repo": str(p),
        "branch": run("branch", "--show-current"),
        "status": run("status", "--short", "--branch"),
    }


@mcp.tool(annotations=read_only_annotations())
def git_diff(repo: str, staged: bool = False) -> str:
    """Return the current Git diff for an allowlisted repository.

    Set staged=true for the index diff.
    """
    p = ensure_allowed(resolve_path(repo), CONFIG.read_roots, purpose="git read")
    args = ["git", "-C", str(p), "diff"]

    if staged:
        args.append("--cached")

    try:
        proc = _run_readonly(args)
    except subprocess.TimeoutExpired:
        raise RuntimeError("git diff timed out")
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"git diff failed: {proc.returncode}"
        )

    return truncate(proc.stdout)


@mcp.tool(annotations=read_only_annotations(open_world=True))
def kuk_boiler_watch() -> dict[str, Any]:
    """Run the fixed read-only KUK Midea boiler health check.

    This tool accepts no command, path, credentials, or other user input. It
    executes only the configured helper (HBF_MCP_BOILER_HELPER, by default
    ``~/bin/kuk-boiler-watch``), which reads the local InfluxDB credential and
    returns a JSON health summary.
    """
    helper = CONFIG.boiler_helper
    try:
        proc = _run_readonly([str(helper)], timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError("boiler helper timed out")
    except OSError as exc:
        raise RuntimeError(f"boiler helper could not be started: {exc}")
    if proc.returncode != 0:
        raise RuntimeError(
            truncate(proc.stderr.strip() or proc.stdout.strip() or
                     f"boiler helper failed: {proc.returncode}")
        )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "boiler helper returned invalid JSON: " + truncate(proc.stdout, 4096)
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("boiler helper returned a non-object JSON result")
    return result


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
