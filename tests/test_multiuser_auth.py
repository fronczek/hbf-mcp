"""Offline checks for user/client isolation and the central MCP boundary."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

from auth.policy import Policy, current_policy, current_principal, correlation_id, load_policy
from auth.server import AuthorizedMCPServer
from auth0_resource import Auth0Verifier

def policy_data():
    return {
        "version": 1,
        "roles": {
            "admin": {"tools": {"*": ["*"]}},
            "reader": {"tools": {
                "api_gitea_branches": ["hbf/allowed"],
                "api_youtrack_issue": ["ESE"],
                "api_monitor_zabbix": ["pve"],
                "api_monitor_list": ["*"],
            }},
        },
        "bindings": [
            {"subject": "artur", "client_id": "chatgpt", "roles": ["admin"],
             "agent": "chatgpt", "technical_account": "gpt"},
            {"subject": "artur", "client_id": "claude", "roles": ["reader"]},
            {"subject": "mariusz", "client_id": "chatgpt", "roles": ["reader"]},
        ],
        "service_accounts": {
            "gitea": {"login": "gpt", "host": "mcp01", "unix_user": "mcp"},
        },
    }

def token(subject="mariusz", client="chatgpt", **changes):
    values = dict(token="NEVER-LOG-BEARER", client_id=client, subject=subject,
                  scopes=["mcp:access"], expires_at=int(time.time()) + 300)
    values.update(changes)
    return AccessToken(**values)

def make_server(events=None, **kwargs):
    events = [] if events is None else events
    server = AuthorizedMCPServer("auth-test", policy=Policy(policy_data()),
                                 audit_writer=events.append, **kwargs)

    @server.tool()
    def api_gitea_branches(repo: str) -> dict:
        return {"repo": repo, "subject": current_principal.get().subject,
                "correlation": correlation_id.get()}

    @server.tool()
    def shell(command: str) -> dict:
        return {"executed": command}

    @server.tool()
    def brand_new_unsafe() -> dict:
        return {}

    return server, events

def test_policy_exact_subject_and_client_binding():
    policy = Policy(policy_data())
    assert policy.principal("artur", "chatgpt").roles == ("admin",)
    assert policy.principal("artur", "claude").roles == ("reader",)
    for subject, client in [("mariusz", "claude"), ("unknown", "chatgpt"), ("artur", "evil")]:
        with pytest.raises(PermissionError):
            policy.principal(subject, client)

@pytest.mark.parametrize("tool,args", [
    ("shell", {"command": "echo admin"}),
    ("read_file", {"path": "/home/mcp/.credentials/gitea.yml"}),
    ("write_file", {"path": "/opt/hbf-mcp/server.py"}),
    ("git_workflow_push", {"repo": "hbf/allowed"}),
    ("deepseek_flash_task", {"task": "do admin operation"}),
    ("gitea_search_code", {"repo": "hbf/allowed"}),
    ("brand_new_unsafe", {}),
    ("api_gitea_branches", {"repo": "hbf/other"}),
    ("api_gitea_branches", {"repo": "hbf/allowed/../other"}),
    ("api_gitea_branches", {"repo": ["hbf/allowed"]}),
    ("api_gitea_branches", {}),
    ("api_youtrack_issue", {"issue_id": "OTHER-1"}),
    ("api_youtrack_issue", {"issue_id": "ESE-1/../../OTHER-2"}),
    ("api_monitor_zabbix", {"profile": "other", "config": "/etc/other"}),
])
def test_restricted_user_cannot_escape_via_tool_or_resource(tool, args):
    policy = Policy(policy_data())
    with pytest.raises(PermissionError):
        policy.authorize(policy.principal("mariusz", "chatgpt"), tool, args)

def test_restricted_grants_are_exact_and_admin_is_explicit():
    policy = Policy(policy_data())
    reader = policy.principal("mariusz", "chatgpt")
    assert policy.authorize(reader, "api_gitea_branches", {"repo": "hbf/allowed"}) == "hbf/allowed"
    assert policy.authorize(reader, "api_youtrack_issue", {"issue_id": "ESE-123"}) == "ESE"
    assert policy.authorize(reader, "api_monitor_zabbix", {"profile": "pve"}) == "pve"
    assert policy.authorize(reader, "api_monitor_list", {}) == "*"
    assert policy.authorize(policy.principal("artur", "chatgpt"), "shell", {}) == "administrative"

@pytest.mark.parametrize("tool,resources", [("shell", ["*"]), ("*", ["*"]),
                                              ("api_gitea_branches", ["*"])])
def test_policy_cannot_grant_generic_surface_to_restricted_role(tool, resources):
    data = policy_data()
    data["roles"]["reader"]["tools"] = {tool: resources}
    with pytest.raises(ValueError):
        Policy(data)

def test_loader_rejects_relative_user_owned_and_symlink_policy(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        load_policy("policy.json")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_data()))
    with pytest.raises(ValueError, match="root owned"):
        load_policy(str(path))
    link = tmp_path / "policy-link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_policy(str(link))

def test_list_hides_administrative_tools_and_unknown_tools():
    server, _ = make_server()
    with patch("auth.server.get_access_token", return_value=token()):
        assert [x.name for x in asyncio.run(server.list_tools())] == ["api_gitea_branches"]
    with patch("auth.server.get_access_token", return_value=token("artur")):
        assert len(asyncio.run(server.list_tools())) == 3

@pytest.mark.parametrize("access", [None, token(subject="unknown"), token(client="evil"),
                                      token(expires_at=0), token(scopes=["mcp:admin"])])
def test_unauthenticated_or_invalid_identity_denied_everywhere(access):
    server, events = make_server()
    with patch("auth.server.get_access_token", return_value=access):
        assert asyncio.run(server.list_tools()) == []
        with pytest.raises(ToolError, match="not permitted"):
            asyncio.run(server.call_tool("api_gitea_branches", {"repo": "hbf/allowed"}))
    assert events[-1]["outcome"] == "denied"

def test_parameters_cannot_spoof_subject_or_client():
    server, events = make_server()
    with patch("auth.server.get_access_token", return_value=token()):
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("shell", {"command": "echo privileged",
                                                  "subject": "artur", "client_id": "chatgpt"}))
    assert events[-1]["subject"] == "mariusz"
    assert events[-1]["outcome"] == "denied"

def test_audit_success_is_correlated_and_contains_no_inputs_or_token():
    server, events = make_server()
    with patch("auth.server.get_access_token", return_value=token()):
        result = asyncio.run(server.call_tool("api_gitea_branches", {"repo": "hbf/allowed"}))
    assert result.is_error is False
    assert [e["outcome"] for e in events] == ["started", "success"]
    assert events[0]["correlation_id"] == events[1]["correlation_id"]
    assert events[-1]["resource"] == "hbf/allowed"
    assert "NEVER-LOG-BEARER" not in json.dumps(events)
    assert current_principal.get() is None
    assert current_policy.get() is None
    assert correlation_id.get() is None

def test_audit_error_has_no_exception_content():
    server, events = make_server()
    @server.tool()
    def api_monitor_zabbix(profile: str) -> dict:
        raise RuntimeError("EXTERNAL-SECRET")
    with patch("auth.server.get_access_token", return_value=token()):
        with pytest.raises(Exception):
            asyncio.run(server.call_tool("api_monitor_zabbix", {"profile": "pve"}))
    assert [e["outcome"] for e in events] == ["started", "error"]
    assert "EXTERNAL-SECRET" not in json.dumps(events)
    assert current_principal.get() is None

def test_audit_failure_prevents_operation():
    server, _ = make_server()
    server.audit_writer = lambda _: (_ for _ in ()).throw(OSError("sink unavailable"))
    with patch("auth.server.get_access_token", return_value=token()):
        with patch("mcp.server.MCPServer.call_tool", new=AsyncMock()) as execute:
            with pytest.raises(ToolError, match="not executed"):
                asyncio.run(server.call_tool("api_gitea_branches", {"repo": "hbf/allowed"}))
            execute.assert_not_awaited()

def test_stdio_identity_does_not_authenticate_http(monkeypatch):
    server, _ = make_server(stdio_identity=("artur", "chatgpt"))
    with patch("auth.server.get_access_token", return_value=None):
        monkeypatch.setenv("HBF_MCP_TRANSPORT", "streamable-http")
        assert asyncio.run(server.list_tools()) == []
        monkeypatch.setenv("HBF_MCP_TRANSPORT", "stdio")
        assert len(asyncio.run(server.list_tools())) == 3

def test_concurrent_calls_keep_principals_and_correlation_separate():
    from contextvars import ContextVar
    access = ContextVar("test_access")
    server, events = make_server()
    seen = []
    @server.tool(name="api_monitor_zabbix")
    async def observe(profile: str) -> dict:
        first = (current_principal.get().subject, correlation_id.get())
        await asyncio.sleep(0)
        second = (current_principal.get().subject, correlation_id.get())
        seen.append((first, second))
        return {"subject": second[0]}
    async def one(subject):
        handle = access.set(token(subject))
        try:
            return await server.call_tool("api_monitor_zabbix", {"profile": "pve"})
        finally:
            access.reset(handle)
    async def run():
        return await asyncio.gather(one("mariusz"), one("artur"))
    with patch("auth.server.get_access_token", side_effect=lambda: access.get()):
        asyncio.run(run())
    assert all(a == b for a, b in seen)
    assert {a[0] for a, _ in seen} == {"mariusz", "artur"}
    assert len({a[1] for a, _ in seen}) == 2

def test_resource_and_prompt_surfaces_fail_closed():
    server, _ = make_server()
    assert asyncio.run(server.list_resources()) == []
    assert asyncio.run(server.list_resource_templates()) == []
    assert asyncio.run(server.list_prompts()) == []
    with pytest.raises(ToolError):
        asyncio.run(server.read_resource("file:///etc/passwd"))
    with pytest.raises(ToolError):
        asyncio.run(server.get_prompt("admin"))

def test_multiuser_verifier_uses_signed_client_binding_and_scope():
    policy = Policy(policy_data())
    verifier = Auth0Verifier("https://hbf.eu.auth0.com/", "https://mcp.hbf.pl/mcp",
                            "", "", allowed_bindings=policy.bindings)
    claims = dict(sub="mariusz", azp="chatgpt", permissions=["mcp:access"],
                  scope="mcp:access", exp=int(time.time()) + 300)
    with patch("auth0_resource.asyncio.to_thread", new=AsyncMock(return_value=claims)):
        result = asyncio.run(verifier.verify_token("synthetic"))
    assert result.subject == "mariusz"
    assert result.client_id == "chatgpt"
    for override in [
        {"sub": "other"}, {"azp": "claude"}, {"client_id": "other"},
        {"azp": None}, {"scope": "mcp:admin"}, {"permissions": ["mcp:admin"]},
        {"sub": ["mariusz"]},
    ]:
        bad = {**claims, **override}
        with patch("auth0_resource.asyncio.to_thread", new=AsyncMock(return_value=bad)):
            assert asyncio.run(verifier.verify_token("synthetic")) is None


def test_stdio_binding_requires_real_policy_pair():
    data = policy_data()
    data["stdio_bindings"] = [{"unix_user": "mcp", "subject": "mariusz", "client_id": "chatgpt"}]
    assert Policy(data).stdio_bindings == {"mcp": ("mariusz", "chatgpt")}
    data["stdio_bindings"][0]["client_id"] = "unknown"
    with pytest.raises(ValueError):
        Policy(data)

def test_audit_denied_attempt_has_resource_and_only_typed_targets():
    server, events = make_server()
    with patch("auth.server.get_access_token", return_value=token()):
        with pytest.raises(ToolError):
            asyncio.run(server.call_tool("api_gitea_branches", {"repo": "hbf/forbidden",
                                                              "secret": "NEVER-LOG"}))
    assert events[-1]["resource"] == "hbf/forbidden"
    assert events[-1]["target"] == {"repo": "hbf/forbidden"}
    assert events[-1]["roles"] == ["reader"]
    assert events[-1]["downstream_accounts"] == {"gitea": "gpt"}
    assert "NEVER-LOG" not in json.dumps(events)

def test_audit_writer_appends_private_synced_json(tmp_path):
    from auth.audit import AuditWriter
    path = tmp_path / "audit.jsonl"
    # Production rejects /tmp's writable ancestor. Bypass parent check only to
    # exercise real file IO in pytest's isolated temporary directory.
    with patch.object(AuditWriter, "_secure_parent"):
        writer = AuditWriter(str(path))
        writer({"outcome": "started"})
        writer({"outcome": "success"})
        assert [json.loads(x)["outcome"] for x in path.read_text().splitlines()] == ["started", "success"]
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
        with pytest.raises(ValueError):
            writer({"outcome": "denied"})

def test_audit_writer_refuses_symlinks_and_writable_parents(tmp_path):
    from auth.audit import AuditWriter
    path = tmp_path / "audit.jsonl"
    with pytest.raises(ValueError):
        AuditWriter(str(path))
    destination = tmp_path / "destination"
    destination.write_text("do not change")
    path.symlink_to(destination)
    with patch.object(AuditWriter, "_secure_parent"):
        writer = AuditWriter(str(path))
        with pytest.raises(OSError):
            writer({"outcome": "started"})
    assert destination.read_text() == "do not change"

def test_audit_writer_propagates_write_and_sync_errors(tmp_path):
    from auth.audit import AuditWriter
    with patch.object(AuditWriter, "_secure_parent"):
        writer = AuditWriter(str(tmp_path / "audit.jsonl"))
        with patch("auth.audit.os.write", return_value=0):
            with pytest.raises(OSError, match="Incomplete"):
                writer({"outcome": "started"})
        with patch("auth.audit.os.fsync", side_effect=OSError("disk unavailable")):
            with pytest.raises(OSError):
                writer({"outcome": "started"})
    writer = AuditWriter()
    with patch("auth.audit.os.write", side_effect=OSError("journal unavailable")):
        with pytest.raises(OSError):
            writer({"outcome": "started"})
