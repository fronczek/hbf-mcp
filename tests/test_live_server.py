"""End-to-end smoke test: boot the real server and talk MCP to it.

This is what proves the server actually starts, that the SDK version is
compatible, and that the annotations survive the HTTP wire.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp import Client

ROOT = Path(__file__).resolve().parents[1]

READ_ONLY_TOOLS = {
    "info",
    "get_hostname",
    "get_time",
    "system_status",
    "read_logs",
    "list_dir",
    "read_file",
    "read_secret_file",
    "git_status",
    "git_diff",
    "kuk_boiler_watch",
}
MUTATING_TOOLS = {"shell", "write_file"}


GITEA_READ_ONLY_TOOLS = {
    "gitea_connection", "gitea_get_repository", "gitea_list_branches",
    "gitea_get_branch", "gitea_read_path", "gitea_list_commits",
    "gitea_get_commit", "gitea_compare_refs", "gitea_get_tree",
    "gitea_search_code", "gitea_list_issues", "gitea_get_issue",
    "gitea_list_pull_requests", "gitea_get_pull_request", "gitea_get_pull_diff",
    "gitea_list_pull_files", "gitea_list_reviews", "gitea_list_comments",
    "gitea_commit_status", "gitea_list_action_runs", "gitea_list_action_jobs",
    "gitea_read_action_log", "gitea_operation_status",
}
GITEA_MUTATING_TOOLS = {
    "gitea_add_comment", "gitea_create_issue", "gitea_create_pull_request",
    "gitea_submit_review", "gitea_create_branch",
}
YOUTRACK_MUTATING_TOOLS = {"youtrack_add_comment", "youtrack_append_decision"}
READ_ONLY_TOOLS |= GITEA_READ_ONLY_TOOLS | {"youtrack_issue_overview"}
MUTATING_TOOLS |= GITEA_MUTATING_TOOLS | YOUTRACK_MUTATING_TOOLS

GIT_READ_ONLY_TOOLS = {"git_workflow_status"}
GIT_MUTATING_TOOLS = {"git_workflow_sync", "git_workflow_branch", "git_workflow_stage",
                      "git_workflow_signed_commit", "git_workflow_push"}
AGENT_READ_ONLY_TOOLS = {"deepseek_sessions"}
AGENT_MUTATING_TOOLS = {"deepseek_flash_task", "ese_browser_gate"}
READ_ONLY_TOOLS |= GIT_READ_ONLY_TOOLS | AGENT_READ_ONLY_TOOLS | {"mcp_health","mcp_api_health","zabbix_health",
                       "youtrack_search_issues","youtrack_issue_detail","youtrack_issue_comments",
                       "gitea_ci_summary","emby_instances","emby_status","emby_library_counts","emby_movies","emby_movie_details"}
MUTATING_TOOLS |= GIT_MUTATING_TOOLS | AGENT_MUTATING_TOOLS | {"youtrack_apply_state"}


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.stdout.read()}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not start listening in time")


@pytest.fixture
def live_server(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "HBF_MCP_HOST": "127.0.0.1",
            "HBF_MCP_PORT": str(port),
            "HBF_MCP_DEFAULT_CWD": str(allowed),
            "HBF_MCP_READ_ROOTS": str(allowed),
            "HBF_MCP_WRITE_ROOTS": str(allowed),
            "HBF_MCP_SECRET_READ_ALLOWLIST": "",
        }
    )
    # Do not inherit a stale repo-level .env value.
    env.pop("HBF_MCP_LOG_FILES", None)

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_port(port, proc)
        yield f"http://127.0.0.1:{port}/mcp", allowed
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_live_server_lists_tools_with_annotations(live_server):
    url, _ = live_server

    async def run():
        async with Client(url) as client:
            result = await client.list_tools()
            return {tool.name: tool for tool in result.tools}

    tools = asyncio.run(run())
    assert set(tools) == READ_ONLY_TOOLS | MUTATING_TOOLS
    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    for name in MUTATING_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name


def test_live_server_read_only_tools_over_http(live_server):
    url, allowed = live_server
    (allowed / "hello.txt").write_text("hello over http\n", encoding="utf-8")

    async def run():
        async with Client(url) as client:
            hostname = await client.call_tool("get_hostname", {})
            clock = await client.call_tool("get_time", {})
            status = await client.call_tool("system_status", {})
            read = await client.call_tool("read_file", {"path": str(allowed / "hello.txt")})
            outside = await client.call_tool("read_file", {"path": str(allowed / ".." / "etc-passwd")})
            return hostname, clock, status, read, outside

    hostname, clock, status, read, outside = asyncio.run(run())

    assert hostname.structured_content["hostname"]
    assert clock.structured_content["timezone"]
    assert clock.structured_content["utc_iso"]
    assert "uptime_seconds" in status.structured_content
    assert "memory" in status.structured_content
    assert read.structured_content["content"] == "hello over http\n"
    assert outside.is_error is True


def test_live_server_refuses_secrets_over_http(live_server):
    url, allowed = live_server
    secret = allowed / ".env"
    secret.write_text("TOKEN=do-not-return\n", encoding="utf-8")

    async def run():
        async with Client(url) as client:
            return await client.call_tool("read_file", {"path": str(secret)})

    result = asyncio.run(run())
    assert result.is_error is True
    # The credential must not be part of the response.
    assert "do-not-return" not in str(result)


def test_live_server_shell_runs_over_http(live_server):
    url, _ = live_server

    async def run():
        async with Client(url) as client:
            return await client.call_tool("shell", {"command": "echo live-mcp"})

    result = asyncio.run(run())
    assert result.is_error is False
    assert "live-mcp" in result.structured_content["stdout"]


def test_deployment_client_checks_running_source_over_http(live_server):
    url, _ = live_server
    env = os.environ.copy()
    env["HBF_MCP_TEST_URL"] = url
    proc = subprocess.run(
        [sys.executable, str(ROOT / "test_client.py")],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Runtime source matches disk:" in proc.stdout
