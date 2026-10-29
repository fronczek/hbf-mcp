"""Real Streamable HTTP: OAuth verifier, SDK auth context and policy boundary.

The only mocked piece is JWKS retrieval: JWT signing and cryptographic token
validation run normally. Fixture tools do not contact or mutate real services.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx2
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
import pytest
import uvicorn

from auth.policy import Policy, current_principal, correlation_id
from auth.server import AuthorizedMCPServer
from auth0_resource import Auth0Verifier

ISSUER = "https://hbf.eu.auth0.com/"
AUDIENCE = "https://mcp.example.hbf.pl/mcp"

@pytest.fixture(scope="module")
def protected_http():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    policy = Policy({
        "version": 1,
        "roles": {
            "admin": {"tools": {"*": ["*"]}},
            "reader": {"tools": {"api_gitea_branches": ["hbf/allowed"]}},
        },
        "bindings": [
            {"subject": "artur", "client_id": "chatgpt", "roles": ["admin"]},
            {"subject": "artur", "client_id": "claude", "roles": ["reader"]},
            {"subject": "mariusz", "client_id": "chatgpt", "roles": ["reader"]},
        ],
    })
    verifier = Auth0Verifier(ISSUER, AUDIENCE, allowed_bindings=policy.bindings)
    verifier.jwks.get_signing_key_from_jwt = lambda _: SimpleNamespace(key=key.public_key())
    events, executed = [], []
    server = AuthorizedMCPServer(
        "protected-http-test", policy=policy, audit_writer=events.append,
        token_verifier=verifier,
        auth=AuthSettings(issuer_url=AnyHttpUrl(ISSUER),
                          resource_server_url=AnyHttpUrl(AUDIENCE),
                          required_scopes=["mcp:access"], validate_token_resource=True),
    )

    @server.tool(structured_output=True)
    def api_gitea_branches(repo: str) -> dict[str, Any]:
        executed.append(("branches", repo))
        principal = current_principal.get()
        return {"repo": repo, "subject": principal.subject,
                "client_id": principal.client_id, "correlation": correlation_id.get()}

    @server.tool(structured_output=True)
    def shell(command: str) -> dict[str, Any]:
        # Records acceptance only; no shell or subprocess is ever executed.
        executed.append(("shell", command))
        return {"accepted": True, "subject": current_principal.get().subject}

    def bearer(**changes):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "mariusz",
                  "client_id": "chatgpt", "iat": now, "exp": now + 300,
                  "permissions": ["mcp:access"], "scope": "mcp:access"}
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture-key"})

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    application = server.streamable_http_app(stateless_http=True, json_response=True, host="127.0.0.1")
    runner = uvicorn.Server(uvicorn.Config(application, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=runner.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.started, "Protected HTTP fixture failed to start"
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", bearer=bearer,
                              events=events, executed=executed)
    finally:
        runner.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Protected HTTP fixture failed to stop"

@asynccontextmanager
async def authenticated_client(fixture, bearer):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + bearer}) as http:
        async with streamable_http_client(fixture.url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session

def test_protected_resource_discovery(protected_http):
    async def run():
        async with httpx2.AsyncClient() as http:
            base = protected_http.url.removesuffix("/mcp")
            metadata = await http.get(base + "/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200
            data = metadata.json()
            assert data["resource"] == AUDIENCE
            assert data["authorization_servers"] == [ISSUER]
            assert "mcp:access" in data["scopes_supported"]
    asyncio.run(run())

@pytest.mark.parametrize("kind", ["missing", "expired", "wrong_audience", "wrong_issuer",
                                  "wrong_user", "wrong_client", "conflicting_client",
                                  "missing_scope", "invalid_signature"])
def test_invalid_tokens_fail_at_http_boundary(protected_http, kind):
    changes = {
        "expired": {"exp": int(time.time()) - 60},
        "wrong_audience": {"aud": "https://other.example/mcp"},
        "wrong_issuer": {"iss": "https://other.eu.auth0.com/"},
        "wrong_user": {"sub": "unknown"},
        "wrong_client": {"client_id": "unknown"},
        "conflicting_client": {"azp": "claude"},
        "missing_scope": {"scope": "mcp:admin"},
    }
    value = protected_http.bearer(**changes.get(kind, {}))
    if kind == "invalid_signature":
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        value = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "mariusz",
                            "client_id": "chatgpt", "iat": int(time.time()),
                            "exp": int(time.time()) + 300, "permissions": ["mcp:access"],
                            "scope": "mcp:access"}, other, algorithm="RS256",
                           headers={"kid": "fixture-key"})
    before = len(protected_http.executed)
    async def run():
        headers = {"Accept": "application/json, text/event-stream"}
        if kind != "missing":
            headers["Authorization"] = "Bearer " + value
        async with httpx2.AsyncClient() as http:
            response = await http.post(protected_http.url, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}},
            })
            assert response.status_code == 401
            assert "resource_metadata=" in response.headers["www-authenticate"]
    asyncio.run(run())
    assert len(protected_http.executed) == before

def test_reader_real_http_auth_context_and_exact_resource(protected_http):
    async def run():
        async with authenticated_client(protected_http, protected_http.bearer()) as client:
            listed = await client.list_tools()
            assert [item.name for item in listed.tools] == ["api_gitea_branches"]
            result = await client.call_tool("api_gitea_branches", {"repo": "hbf/allowed"})
            assert result.is_error is False
            assert result.structured_content["subject"] == "mariusz"
            assert result.structured_content["client_id"] == "chatgpt"
            assert result.structured_content["correlation"]
            forbidden = await client.call_tool("api_gitea_branches", {"repo": "hbf/other"})
            assert forbidden.is_error is True
            assert "not permitted" in str(forbidden)
            shell = await client.call_tool("shell", {"command": "never executed"})
            assert shell.is_error is True
            spoof = await client.call_tool("shell", {"command": "never executed",
                                                     "subject": "artur", "client_id": "chatgpt",
                                                     "roles": ["admin"]})
            assert spoof.is_error is True
    before = len(protected_http.executed)
    asyncio.run(run())
    assert protected_http.executed[before:] == [("branches", "hbf/allowed")]
    for event in protected_http.events:
        assert "eyJ" not in str(event)
        assert "never executed" not in str(event)

def test_admin_can_use_fixture_shell_and_client_binding_limits_same_user(protected_http):
    async def run():
        async with authenticated_client(protected_http, protected_http.bearer(sub="artur")) as client:
            tools = await client.list_tools()
            assert {x.name for x in tools.tools} == {"api_gitea_branches", "shell"}
            result = await client.call_tool("shell", {"command": "fixture acceptance"})
            assert result.is_error is False
            assert result.structured_content["subject"] == "artur"
        async with authenticated_client(protected_http,
                protected_http.bearer(sub="artur", client_id="claude")) as client:
            assert [x.name for x in (await client.list_tools()).tools] == ["api_gitea_branches"]
            assert (await client.call_tool("shell", {"command": "never executed"})).is_error is True
            allowed = await client.call_tool("api_gitea_branches", {"repo": "hbf/allowed"})
            assert allowed.structured_content["client_id"] == "claude"
    asyncio.run(run())
