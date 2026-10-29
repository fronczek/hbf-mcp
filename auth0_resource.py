"""Auth0 JWT token verification for the Gitea MCP resource server.

Auth0 signs access tokens; this service verifies them locally against cached JWKS.
No client secret or Auth0 Management API credentials are needed at runtime.
"""
import asyncio
import logging
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

LOG = logging.getLogger(__name__)
PERMISSIONS = frozenset(("mcp:admin",))

class Auth0Verifier(TokenVerifier):
    def __init__(self, issuer: str, audience: str, client_id: str = "", owner_sub: str = "",
                 *, allowed_bindings: frozenset[tuple[str, str]] | None = None):
        issuer = issuer.rstrip("/") + "/"
        parsed = urlsplit(issuer)
        resource = urlsplit(audience)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path != "/"
        ):
            raise ValueError("HBF_MCP_AUTH0_ISSUER must be a tenant HTTPS origin")
        if (
            resource.scheme != "https" or not resource.hostname or resource.username or
            resource.password or resource.query or resource.fragment or
            not resource.path.endswith("/mcp")
        ):
            raise ValueError("HBF_MCP_PUBLIC_URL must produce an HTTPS MCP URL")
        if allowed_bindings is None and (not client_id.strip() or not owner_sub.strip()):
            raise ValueError("HBF_MCP_AUTH0_CLIENT_ID and HBF_MCP_AUTH0_OWNER_SUB are required")
        if allowed_bindings is not None and (not allowed_bindings or any(
            not isinstance(pair, tuple) or len(pair) != 2 or
            any(not isinstance(value, str) or not value.strip() for value in pair)
            for pair in allowed_bindings
        )):
            raise ValueError("Explicit valid identity bindings are required")
        self.allowed_bindings = allowed_bindings
        self.issuer = issuer
        self.audience = audience
        self.client_id = client_id
        self.owner_sub = owner_sub
        # Never derive JWKS URL from untrusted tokens. Cache keys to limit lookups.
        self.jwks = PyJWKClient(issuer + ".well-known/jwks.json", cache_jwk_set=True,
                               lifespan=300, cache_keys=False, timeout=5)

    def _validate_client(self, claims: dict) -> str:
        """Client identity comes only from signed, mutually consistent claims."""
        azp = claims.get("azp")
        client = claims.get("client_id")
        if azp is not None and (not isinstance(azp, str) or not azp):
            raise ValueError("Invalid OAuth client")
        if client is not None and (not isinstance(client, str) or not client):
            raise ValueError("Invalid OAuth client")
        if azp is not None and client is not None and azp != client:
            raise ValueError("Conflicting OAuth clients")
        actual = azp if azp is not None else client
        if actual is None:
            raise ValueError("Access token has no authorized client")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("Invalid token subject")
        if self.allowed_bindings is not None:
            if (subject, actual) not in self.allowed_bindings:
                raise ValueError("Unknown user/client binding")
        elif actual != self.client_id or subject != self.owner_sub:
            raise ValueError("Unexpected OAuth user/client")
        return actual

    def decode(self, token: str) -> dict:
        if len(token) > 16384:
            raise ValueError("Token too large")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("Unsupported JWT header")
        key = self.jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=self.audience,
            issuer=self.issuer, leeway=30,
            options={"require": ["exp", "iss", "aud", "iat", "sub"]},
        )
        if self.allowed_bindings is None and claims["sub"] != self.owner_sub:
            raise ValueError("Access token belongs to an unknown user")
        # Legacy Auth0 tokens use azp; the RFC 9068 profile uses client_id.
        self._validate_client(claims)
        return claims

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.to_thread(self.decode, token)
            if self.allowed_bindings is None and claims.get("sub") != self.owner_sub:
                return None
            actual_client = self._validate_client(claims)
            granted = claims.get("permissions", [])
            requested = claims.get("scope", "")
            if not isinstance(granted, list) or not all(isinstance(v, str) for v in granted):
                return None
            if not isinstance(requested, str):
                return None
            # A permission is usable only when both delegated scope and RBAC allow it.
            required = "mcp:access" if self.allowed_bindings is not None else "mcp:admin"
            scopes = sorted({required}.intersection(granted).intersection(requested.split()))
            if required not in scopes:
                return None
            return AccessToken(
                token=token, client_id=actual_client, scopes=scopes,
                expires_at=int(claims["exp"]), resource=self.audience,
                subject=claims["sub"],
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError) as exc:
            LOG.warning("Auth0 bearer token rejected: %s", type(exc).__name__)
            return None
