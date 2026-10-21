import asyncio
from unittest.mock import patch

import jwt
import pytest

from auth0_resource import Auth0Verifier


def verifier():
    return Auth0Verifier("https://hbf.eu.auth0.com/", "https://example.hbf.pl/mcp",
                         "client-1", "auth0|user-1")


@pytest.mark.parametrize("override", [
    {"sub": "auth0|wrong"}, {"azp": "client-2"},
    {"scope": "mcp:read"}, {"permissions": ["mcp:read"]},
    {"permissions": []}, {"scope": ""}, {"permissions": "mcp:admin"},
])
def test_permissions_fail_closed(override):
    v = verifier()
    claims = {"sub": "auth0|user-1", "azp": "client-1",
              "permissions": ["mcp:admin"], "scope": "mcp:admin", "exp": 2000000000}
    claims.update(override)
    with patch.object(v, "decode", return_value=claims):
        result = asyncio.run(v.verify_token("placeholder"))
        assert result is None


def test_valid_admin_token():
    v = verifier()
    claims = {"sub": "auth0|user-1", "azp": "client-1",
              "permissions": ["mcp:admin"], "scope": "mcp:admin", "exp": 2000000000}
    with patch.object(v, "decode", return_value=claims):
        result = asyncio.run(v.verify_token("placeholder"))
        assert result.scopes == ["mcp:admin"]
        assert result.subject == "auth0|user-1"


def test_invalid_signature_is_rejected():
    v = verifier()
    with patch.object(v, "decode", side_effect=jwt.InvalidTokenError()):
        assert asyncio.run(v.verify_token("placeholder")) is None


def test_configuration_validation():
    with pytest.raises(ValueError):
        Auth0Verifier("http://hbf.eu.auth0.com/", "https://example.hbf.pl/mcp", "c", "u")
    with pytest.raises(ValueError):
        Auth0Verifier("https://hbf.eu.auth0.com/", "http://example.hbf.pl/mcp", "c", "u")
    with pytest.raises(ValueError):
        Auth0Verifier("https://hbf.eu.auth0.com/", "https://example.hbf.pl/mcp", "", "u")
