import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from auth0_resource import Auth0Verifier


def verifier():
    return Auth0Verifier("https://hbf.eu.auth0.com/", "https://example.hbf.pl/mcp",
                         "client-1", "auth0|user-1")


PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def encoded_token(**overrides):
    now = int(time.time())
    claims = {
        "iss": "https://hbf.eu.auth0.com/", "aud": "https://example.hbf.pl/mcp",
        "sub": "auth0|user-1", "client_id": "client-1", "iat": now,
        "exp": now + 300, "permissions": ["mcp:admin"], "scope": "mcp:admin",
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize("override", [
    {"sub": "auth0|wrong"}, {"azp": "client-2"}, {"client_id": "client-2", "azp": None},
    {"azp": None}, {"azp": "client-1", "client_id": "client-2"},
    {"scope": "mcp:read"}, {"permissions": ["mcp:read"]},
    {"permissions": []}, {"scope": ""}, {"permissions": "mcp:admin"},
])
def test_permissions_fail_closed(override):
    v = verifier()
    claims = {"sub": "auth0|user-1", "azp": "client-1",
              "permissions": ["mcp:admin"], "scope": "mcp:admin", "exp": 2000000000}
    claims.update(override)
    with patch("auth0_resource.asyncio.to_thread", new=AsyncMock(return_value=claims)):
        result = asyncio.run(v.verify_token("placeholder"))
        assert result is None


def test_valid_admin_token():
    v = verifier()
    claims = {"sub": "auth0|user-1", "azp": "client-1",
              "permissions": ["mcp:admin"], "scope": "mcp:admin", "exp": 2000000000}
    with patch("auth0_resource.asyncio.to_thread", new=AsyncMock(return_value=claims)):
        result = asyncio.run(v.verify_token("placeholder"))
        assert result.scopes == ["mcp:admin"]
        assert result.subject == "auth0|user-1"


def test_valid_rfc9068_client_id_token():
    v = verifier()
    claims = {"sub": "auth0|user-1", "client_id": "client-1",
              "permissions": ["mcp:admin"], "scope": "mcp:admin", "exp": 2000000000}
    with patch("auth0_resource.asyncio.to_thread", new=AsyncMock(return_value=claims)):
        result = asyncio.run(v.verify_token("placeholder"))
        assert result is not None
        assert result.client_id == "client-1"


def test_invalid_signature_is_rejected():
    v = verifier()
    with patch(
        "auth0_resource.asyncio.to_thread",
        new=AsyncMock(side_effect=jwt.InvalidTokenError()),
    ):
        assert asyncio.run(v.verify_token("placeholder")) is None


def test_decode_verifies_signature_issuer_audience_and_time_claims():
    v = verifier()
    with patch.object(v.jwks, "get_signing_key_from_jwt",
                      return_value=SimpleNamespace(key=PRIVATE_KEY.public_key())):
        assert v.decode(encoded_token())["sub"] == "auth0|user-1"
        with pytest.raises(jwt.InvalidAudienceError):
            v.decode(encoded_token(aud="https://other.example/mcp"))
        with pytest.raises(jwt.InvalidIssuerError):
            v.decode(encoded_token(iss="https://other.eu.auth0.com/"))
        with pytest.raises(jwt.ExpiredSignatureError):
            v.decode(encoded_token(exp=int(time.time()) - 31))


def test_decode_refuses_non_rs256_before_jwks_lookup():
    v = verifier()
    token = jwt.encode({"sub": "auth0|user-1"}, "synthetic-secret-at-least-32-bytes", algorithm="HS256",
                       headers={"kid": "test-key"})
    with patch.object(v.jwks, "get_signing_key_from_jwt") as lookup:
        with pytest.raises(ValueError, match="Unsupported JWT header"):
            v.decode(token)
        lookup.assert_not_called()


def test_configuration_validation():
    with pytest.raises(ValueError):
        Auth0Verifier("http://hbf.eu.auth0.com/", "https://example.hbf.pl/mcp", "c", "u")
    with pytest.raises(ValueError):
        Auth0Verifier("https://hbf.eu.auth0.com/", "http://example.hbf.pl/mcp", "c", "u")
    with pytest.raises(ValueError):
        Auth0Verifier("https://hbf.eu.auth0.com/", "https://example.hbf.pl/mcp", "", "u")


def test_oauth_enablement_is_explicit_and_fail_closed(monkeypatch):
    import server

    monkeypatch.setenv("HBF_MCP_OAUTH_ENABLED", "false")
    assert server.oauth_settings() == {}
    monkeypatch.setenv("HBF_MCP_OAUTH_ENABLED", "tru")
    with pytest.raises(RuntimeError, match="explicit boolean"):
        server.oauth_settings()
    monkeypatch.setenv("HBF_MCP_OAUTH_ENABLED", "true")
    monkeypatch.delenv("HBF_MCP_AUTH0_CLIENT_ID", raising=False)
    with pytest.raises(KeyError):
        server.oauth_settings()


def test_complete_oauth_configuration_builds_current_sdk_settings(monkeypatch):
    import server

    monkeypatch.setenv("HBF_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("HBF_MCP_AUTH0_ISSUER", "https://hbf.eu.auth0.com/")
    monkeypatch.setenv("HBF_MCP_PUBLIC_URL", "https://example.hbf.pl/mcp")
    monkeypatch.setenv("HBF_MCP_AUTH0_CLIENT_ID", "client-1")
    monkeypatch.setenv("HBF_MCP_AUTH0_OWNER_SUB", "auth0|user-1")
    settings = server.oauth_settings()
    assert isinstance(settings["token_verifier"], Auth0Verifier)
    assert settings["auth"].required_scopes == ["mcp:admin"]
    assert settings["auth"].validate_token_resource is True
