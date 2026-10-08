"""Tests for the HBF MCP server.

These tests exercise the server in-process.  Nothing here needs root, a real
service account, or a network listener except the separate live smoke test.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def list_tools() -> dict[str, object]:
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def _replace_config(**kwargs):
    """Build a config variant from whatever the server currently uses."""
    return dataclasses.replace(server.CONFIG, **kwargs)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate the server to a temporary allowed root with an outside sibling."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    config = _replace_config(
        default_cwd=server.canonical(allowed),
        read_roots=(server.canonical(allowed),),
        write_roots=(server.canonical(allowed),),
        secret_read_allowlist=(),
        allowed_log_services=("hbf-mcp",),
        log_files={},
        boiler_helper=server.canonical(allowed) / "bin" / "kuk-boiler-watch",
    )
    monkeypatch.setattr(server, "CONFIG", config)
    return SimpleNamespace(allowed=allowed, outside=outside, config=config)


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    # Keep the test hermetic: no user/system git config (the owner's global
    # config signs commits) and a fixed synthetic identity.
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(sandbox):
    repo = sandbox.allowed / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


# ---------------------------------------------------------------------------
# 1. Annotations
# ---------------------------------------------------------------------------

def test_every_tool_is_classified_and_annotated():
    tools = list_tools()
    assert set(tools) == READ_ONLY_TOOLS | MUTATING_TOOLS
    for name, tool in tools.items():
        assert tool.annotations is not None, f"{name} has no annotations"
        assert tool.annotations.read_only_hint is not None, f"{name} has no readOnlyHint"


def test_read_only_hint_matches_the_required_table():
    tools = list_tools()
    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    for name in MUTATING_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name


def test_mutating_tools_are_marked_destructive_not_idempotent():
    tools = list_tools()
    for name in MUTATING_TOOLS:
        annotations = tools[name].annotations
        assert annotations.destructive_hint is True, name
        assert annotations.idempotent_hint is False, name


def test_shell_is_not_annotated_as_read_only():
    tool = list_tools()["shell"]
    assert tool.annotations.read_only_hint is False


def test_annotations_serialise_to_the_camel_case_wire_fields():
    wire = asyncio.run(server.mcp.list_tools())
    payload = {tool.name: tool.model_dump(by_alias=True) for tool in wire}
    assert payload["read_file"]["annotations"]["readOnlyHint"] is True
    assert payload["shell"]["annotations"]["readOnlyHint"] is False
    assert payload["write_file"]["annotations"]["readOnlyHint"] is False


# ---------------------------------------------------------------------------
# 2. No stale sudo declarations
# ---------------------------------------------------------------------------

SCANNED_FILES = [
    "server.py",
    "README.md",
    "install.sh",
    "test_client.py",
    "hbf-mcp.env.example",
    "systemd/hbf-mcp.service",
]

FORBIDDEN_DECLARATIONS = [
    "passwordless",
    "nopasswd",
    "unrestricted bash",
    "can use sudo",
    "sudo -n",
    "when root privileges are needed",
    "use sudo explicitly",
]


def test_no_passwordless_sudo_declaration_anywhere():
    root = Path(server.__file__).resolve().parent
    for relative in SCANNED_FILES:
        text = (root / relative).read_text(encoding="utf-8").lower()
        for forbidden in FORBIDDEN_DECLARATIONS:
            assert forbidden not in text, f"{relative} still declares {forbidden!r}"


def test_server_never_grants_sudo():
    """Any mention of sudo in the server must be a statement that there is none."""
    text = Path(server.__file__).read_text(encoding="utf-8")
    for line in text.splitlines():
        lowered = line.lower()
        if "sudo" not in lowered:
            continue
        assert (
            "no sudo" in lowered
            or "not a sudoer" in lowered
            or "sudoer" in lowered
        ), line


def test_shell_description_is_honest():
    doc = server.shell.__doc__ or ""
    assert "unprivileged" in doc
    assert "No privilege escalation is provided." in doc
    assert "unrestricted" not in doc.lower()
    assert "sudo" not in doc.lower()


def test_server_instructions_describe_an_unprivileged_account():
    instructions = server.mcp.instructions or ""
    assert "unprivileged" in instructions.lower()
    assert "no sudo" in instructions.lower()


# ---------------------------------------------------------------------------
# 3. New read-only tools
# ---------------------------------------------------------------------------

def test_get_hostname_returns_the_host_without_a_shell():
    result = server.get_hostname()
    assert result["hostname"] == socket.gethostname()
    assert result["hostname"]
    assert result["node"]


def test_get_time_reports_timezone_and_utc():
    result = server.get_time()
    assert result["timezone"]
    assert result["utc_offset"][0] in "+-"
    assert isinstance(result["utc_offset_seconds"], int)
    local = dt.datetime.fromisoformat(result["local_iso"])
    utc = dt.datetime.fromisoformat(result["utc_iso"])
    assert local.utcoffset() is not None
    assert utc.utcoffset() == dt.timedelta(0)
    assert abs((local.astimezone(dt.timezone.utc) - utc).total_seconds()) < 1


def test_system_status_returns_uptime_ram_disk_load():
    result = server.system_status()
    assert set(result) == {"uptime_seconds", "memory", "disk", "load_average"}
    if result["uptime_seconds"] is not None:
        assert result["uptime_seconds"] >= 0
    if result["memory"] is not None:
        assert result["memory"]["total_bytes"] > 0
    assert isinstance(result["disk"], dict)
    if result["load_average"] is not None:
        assert len(result["load_average"]) == 3


def test_system_status_disk_reports_default_cwd(sandbox):
    result = server.system_status()
    assert "default_cwd" in result["disk"]
    assert result["disk"]["default_cwd"]["total_bytes"] > 0


# ---------------------------------------------------------------------------
# 4. read_logs
# ---------------------------------------------------------------------------

def test_read_logs_refuses_unknown_service(sandbox):
    with pytest.raises(server.PathAccessError):
        server.read_logs("sshd")


def test_read_logs_reads_only_the_allowlisted_file(sandbox, monkeypatch):
    log = sandbox.allowed / "service.log"
    log.write_text("\n".join(f"line{i}" for i in range(200)) + "\n", encoding="utf-8")

    config = _replace_config(
        log_files={"mysvc": str(log)},
        allowed_log_services=("mysvc",),
    )
    monkeypatch.setattr(server, "CONFIG", config)

    result = server.read_logs("mysvc", lines=5)
    body = result["content"].strip().splitlines()
    assert body == ["line195", "line196", "line197", "line198", "line199"]


def test_read_logs_caps_the_line_count(sandbox, monkeypatch):
    log = sandbox.allowed / "service.log"
    log.write_text("x\n" * 10, encoding="utf-8")
    config = _replace_config(log_files={"mysvc": str(log)}, max_log_lines=25)
    monkeypatch.setattr(server, "CONFIG", config)

    assert server.read_logs("mysvc", lines=10**9)["lines"] == 25


def test_read_logs_cannot_escape_the_read_roots(sandbox, monkeypatch):
    log = sandbox.outside / "outside.log"
    log.write_text("secret\n", encoding="utf-8")
    monkeypatch.setattr(
        server,
        "CONFIG",
        _replace_config(log_files={"mysvc": str(log)}),
    )
    with pytest.raises(server.PathAccessError):
        server.read_logs("mysvc", lines=1)


# ---------------------------------------------------------------------------
# 5. read_file: allowlist, traversal and secrets
# ---------------------------------------------------------------------------

def test_read_file_reads_a_normal_file(sandbox):
    path = sandbox.allowed / "notes.txt"
    path.write_text("hello\n", encoding="utf-8")
    result = server.read_file(str(path))
    assert result["content"] == "hello\n"
    assert result["truncated"] is False


def test_read_file_refuses_absolute_path_outside_the_roots(sandbox):
    target = sandbox.outside / "data.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(server.PathAccessError):
        server.read_file(str(target))


def test_read_file_refuses_parent_traversal(sandbox):
    (sandbox.allowed / "sub").mkdir()
    target = sandbox.outside / "data.txt"
    target.write_text("x", encoding="utf-8")

    escaping = sandbox.allowed / "sub" / ".." / ".." / "outside" / "data.txt"
    with pytest.raises(server.PathAccessError):
        server.read_file(str(escaping))
    with pytest.raises(server.PathAccessError):
        server.read_file("../outside/data.txt")


def test_read_file_refuses_a_symlink_that_escapes(sandbox):
    target = sandbox.outside / "data.txt"
    target.write_text("x", encoding="utf-8")
    link = sandbox.allowed / "link.txt"
    link.symlink_to(target)

    with pytest.raises(server.PathAccessError):
        server.read_file(str(link))


def test_read_file_refuses_a_symlinked_directory_that_escapes(sandbox):
    link_dir = sandbox.allowed / "linked"
    link_dir.symlink_to(sandbox.outside, target_is_directory=True)
    (sandbox.outside / "data.txt").write_text("x", encoding="utf-8")

    with pytest.raises(server.PathAccessError):
        server.read_file(str(link_dir / "data.txt"))


@pytest.mark.parametrize(
    "name",
    ["id_ed25519", "id_rsa", ".env", "credentials.json", "api_token.txt", "server.key"],
)
def test_read_file_refuses_secret_looking_files(sandbox, name):
    path = sandbox.allowed / name
    path.write_text("TOP SECRET", encoding="utf-8")
    with pytest.raises(server.SecretAccessError):
        server.read_file(str(path))


def test_read_file_refuses_files_under_dot_ssh(sandbox):
    ssh_dir = sandbox.allowed / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "some_key"
    key.write_text("PRIVATE", encoding="utf-8")
    with pytest.raises(server.SecretAccessError):
        server.read_file(str(key))


def test_read_file_truncates_to_the_output_limit(sandbox, monkeypatch):
    path = sandbox.allowed / "big.txt"
    path.write_text("a" * 5000, encoding="utf-8")
    monkeypatch.setattr(server, "CONFIG", _replace_config(max_output=100))

    result = server.read_file(str(path))
    assert result["truncated"] is True
    assert len(result["content"]) == 100


# ---------------------------------------------------------------------------
# 6. read_secret_file: deliberately granted access only
# ---------------------------------------------------------------------------

def test_read_secret_file_refuses_without_an_allowlist(sandbox):
    secret = sandbox.allowed / "credentials.json"
    secret.write_text('{"token": "x"}', encoding="utf-8")
    with pytest.raises(server.SecretAccessError):
        server.read_secret_file(str(secret))


def test_read_secret_file_refuses_paths_outside_the_allowlist(sandbox, monkeypatch):
    allowed_secret = sandbox.allowed / "credentials.json"
    allowed_secret.write_text('{"a": 1}', encoding="utf-8")
    other = sandbox.allowed / "other.json"
    other.write_text('{"b": 2}', encoding="utf-8")

    monkeypatch.setattr(
        server,
        "CONFIG",
        _replace_config(secret_read_allowlist=(server.canonical(allowed_secret),)),
    )
    with pytest.raises(server.SecretAccessError):
        server.read_secret_file(str(other))


def test_read_secret_file_reads_an_explicitly_granted_path(sandbox, monkeypatch):
    secret = sandbox.allowed / "credentials.json"
    secret.write_text('{"token": "x"}', encoding="utf-8")
    monkeypatch.setattr(
        server,
        "CONFIG",
        _replace_config(secret_read_allowlist=(server.canonical(secret),)),
    )

    result = server.read_secret_file(str(secret))
    assert result["content"] == '{"token": "x"}'


def test_secret_allowlist_does_not_leak_other_secrets(sandbox, monkeypatch):
    granted = sandbox.allowed / "granted.json"
    granted.write_text("granted", encoding="utf-8")
    withheld = sandbox.allowed / "withheld.json"
    withheld.write_text("withheld", encoding="utf-8")

    monkeypatch.setattr(
        server,
        "CONFIG",
        _replace_config(secret_read_allowlist=(server.canonical(granted),)),
    )
    with pytest.raises(server.SecretAccessError):
        server.read_secret_file(str(withheld))


# ---------------------------------------------------------------------------
# 7. write_file
# ---------------------------------------------------------------------------

def test_write_file_writes_inside_the_write_roots(sandbox):
    path = sandbox.allowed / "new.txt"
    result = server.write_file(str(path), "content")
    assert path.read_text(encoding="utf-8") == "content"
    assert result["bytes_written"] == len("content")


def test_write_file_creates_parents_inside_the_roots(sandbox):
    path = sandbox.allowed / "a" / "b" / "new.txt"
    server.write_file(str(path), "content")
    assert path.read_text(encoding="utf-8") == "content"


def test_write_file_refuses_paths_outside_the_roots(sandbox):
    target = sandbox.outside / "escape.txt"
    with pytest.raises(server.PathAccessError):
        server.write_file(str(target), "x")
    assert not target.exists()


def test_write_file_refuses_parent_traversal(sandbox):
    target = sandbox.outside / "escape.txt"
    relative = "../outside/escape.txt"
    with pytest.raises(server.PathAccessError):
        server.write_file(relative, "x")
    assert not target.exists()


def test_write_file_refuses_a_symlinked_directory_that_escapes(sandbox):
    link_dir = sandbox.allowed / "linked"
    link_dir.symlink_to(sandbox.outside, target_is_directory=True)

    with pytest.raises(server.PathAccessError):
        server.write_file(str(link_dir / "escape.txt"), "x")
    assert not (sandbox.outside / "escape.txt").exists()


def test_write_file_refuses_a_symlinked_file_that_escapes(sandbox):
    target = sandbox.outside / "escape.txt"
    target.write_text("original", encoding="utf-8")
    link = sandbox.allowed / "link.txt"
    link.symlink_to(target)

    with pytest.raises(server.PathAccessError):
        server.write_file(str(link), "overwritten")
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("name", [".env", "id_ed25519", "credentials.json", "x.pem"])
def test_write_file_refuses_credential_paths(sandbox, name):
    with pytest.raises(server.SecretAccessError):
        server.write_file(str(sandbox.allowed / name), "x")


# ---------------------------------------------------------------------------
# 8. list_dir and git tools
# ---------------------------------------------------------------------------

def test_list_dir_refuses_paths_outside_the_roots(sandbox):
    with pytest.raises(server.PathAccessError):
        server.list_dir(str(sandbox.outside))


def test_list_dir_lists_allowed_directories(sandbox):
    (sandbox.allowed / "file.txt").write_text("x", encoding="utf-8")
    (sandbox.allowed / "sub").mkdir()
    names = {item["name"] for item in server.list_dir(str(sandbox.allowed))}
    assert {"file.txt", "sub"} <= names


def test_git_status_reports_branch_and_clean_tree(git_repo):
    result = server.git_status(str(git_repo))
    assert result["branch"] == _git(git_repo, "branch", "--show-current")
    assert result["status"].startswith("##")


def test_git_status_reports_modified_files(git_repo):
    (git_repo / "file.txt").write_text("changed\n", encoding="utf-8")
    status = server.git_status(str(git_repo))["status"]
    assert "file.txt" in status


def test_git_diff_reports_working_tree_changes(git_repo):
    (git_repo / "file.txt").write_text("changed\n", encoding="utf-8")
    diff = server.git_diff(str(git_repo))
    assert "-hello" in diff
    assert "+changed" in diff


def test_git_diff_staged_only_when_requested(git_repo):
    (git_repo / "file.txt").write_text("staged\n", encoding="utf-8")
    _git(git_repo, "add", "file.txt")
    staged = server.git_diff(str(git_repo), staged=True)
    assert "+staged" in staged
    assert server.git_diff(str(git_repo)) == ""


def test_git_tools_refuse_repositories_outside_the_roots(sandbox):
    outside_repo = sandbox.outside / "repo"
    outside_repo.mkdir()
    _git(outside_repo, "init", "-q")
    with pytest.raises(server.PathAccessError):
        server.git_status(str(outside_repo))
    with pytest.raises(server.PathAccessError):
        server.git_diff(str(outside_repo))


# ---------------------------------------------------------------------------
# 9. shell limits
# ---------------------------------------------------------------------------

def test_shell_runs_a_command_as_the_service_account(sandbox):
    result = server.shell("id -un")
    assert result["exit_code"] == 0
    assert result["stdout"].strip()


def test_shell_enforces_a_timeout(sandbox):
    result = server.shell("sleep 5", timeout=1)
    assert result.get("timed_out") is True


def test_shell_output_is_truncated(sandbox, monkeypatch):
    monkeypatch.setattr(server, "CONFIG", _replace_config(max_output=128))
    result = server.shell("printf 'a%.0s' $(seq 1 5000)")
    assert "[output truncated at 128 bytes]" in result["stdout"]


def test_truncate_keeps_short_values_untouched():
    assert server.truncate("short", limit=100) == "short"


# ---------------------------------------------------------------------------
# 10. Fixed helper tool
# ---------------------------------------------------------------------------

def test_kuk_boiler_watch_is_read_only(sandbox):
    tool = list_tools()["kuk_boiler_watch"]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


def test_kuk_boiler_watch_runs_only_the_fixed_helper(sandbox):
    helper = sandbox.allowed / "bin" / "kuk-boiler-watch"
    helper.parent.mkdir()
    helper.write_text('#!/bin/sh\nprintf \'%s\' \'{"ok": true, "target": 42}\'\n', encoding="utf-8")
    helper.chmod(0o755)

    result = server.kuk_boiler_watch()
    assert result == {"ok": True, "target": 42}


def test_kuk_boiler_watch_accepts_no_arguments():
    import inspect

    signature = inspect.signature(server.kuk_boiler_watch)
    assert list(signature.parameters) == []


def test_kuk_boiler_watch_fails_cleanly_without_a_helper(sandbox):
    with pytest.raises(RuntimeError):
        server.kuk_boiler_watch()
