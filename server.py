#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from mcp.server import MCPServer


NAME = os.getenv("HBF_MCP_NAME", "HBF MCP - Artur")
HOST = os.getenv("HBF_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("HBF_MCP_PORT", "8765"))

DEFAULT_CWD = Path(
    os.getenv("HBF_MCP_DEFAULT_CWD", "/home/gptagent")
).expanduser()

DEFAULT_TIMEOUT = int(os.getenv("HBF_MCP_SHELL_TIMEOUT", "120"))
MAX_OUTPUT = int(os.getenv("HBF_MCP_MAX_OUTPUT", str(512 * 1024)))

mcp = MCPServer(
    NAME,
    instructions=(
        "This is Artur's disposable administrative bridge VM. "
        "The shell tool is intentionally unrestricted on this VM and the "
        "gptagent account has passwordless sudo. Prefer inspecting state "
        "before changing it. Never print secrets unless the user explicitly "
        "asks to see them."
    ),
)


def resolve_path(path: str | None) -> Path:
    """Resolve relative paths against the agent's default working directory."""
    if not path:
        return DEFAULT_CWD

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate

    return DEFAULT_CWD / candidate


def truncate(value: str, limit: int = MAX_OUTPUT) -> str:
    """Limit returned tool output so an accidental huge command stays usable."""
    data = value.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return value

    clipped = data[:limit].decode("utf-8", errors="replace")
    return clipped + f"\n\n[output truncated at {limit} bytes]"


@mcp.tool()
def info() -> dict[str, Any]:
    """Return basic information about this MCP bridge."""
    return {
        "name": NAME,
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "service_cwd": os.getcwd(),
        "default_cwd": str(DEFAULT_CWD),
        "home": str(Path.home()),
    }


@mcp.tool()
def shell(
    command: str,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run an unrestricted Bash command on the bridge VM.

    The MCP service account has passwordless sudo. Use sudo explicitly when
    root privileges are needed.

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


@mcp.tool()
def read_file(path: str, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Read a text file from the VM.

    Relative paths use HBF_MCP_DEFAULT_CWD. Binary data is decoded with
    replacement characters. Large files are truncated to max_bytes.
    """
    p = resolve_path(path)
    raw = p.read_bytes()
    clipped = raw[: max(1, max_bytes)]

    return {
        "path": str(p),
        "size": len(raw),
        "truncated": len(raw) > len(clipped),
        "content": clipped.decode("utf-8", errors="replace"),
    }


@mcp.tool()
def write_file(
    path: str,
    content: str,
    create_parents: bool = True,
) -> dict[str, Any]:
    """Create or replace a UTF-8 text file.

    Relative paths use HBF_MCP_DEFAULT_CWD. Files requiring root permissions
    should be changed with the shell tool and sudo.
    """
    p = resolve_path(path)

    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)

    p.write_text(content, encoding="utf-8")

    return {
        "path": str(p),
        "bytes_written": len(content.encode("utf-8")),
    }


@mcp.tool()
def list_dir(
    path: str = ".",
    include_hidden: bool = True,
) -> list[dict[str, Any]]:
    """List files and directories.

    Relative paths use HBF_MCP_DEFAULT_CWD.
    """
    p = resolve_path(path)

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


@mcp.tool()
def git_status(repo: str) -> dict[str, Any]:
    """Return branch and porcelain status for a Git repository."""
    p = resolve_path(repo)

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(p), *args],
            text=True,
            capture_output=True,
        )
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


@mcp.tool()
def git_diff(repo: str, staged: bool = False) -> str:
    """Return the current Git diff. Set staged=true for the index diff."""
    p = resolve_path(repo)
    args = ["git", "-C", str(p), "diff"]

    if staged:
        args.append("--cached")

    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"git diff failed: {proc.returncode}"
        )

    return truncate(proc.stdout)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
