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
    def __init__(self, issuer: str, audience: str, client_id: str, owner_sub: str):
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
        if not client_id.strip() or not owner_sub.strip():
            raise ValueError("HBF_MCP_AUTH0_CLIENT_ID and HBF_MCP_AUTH0_OWNER_SUB are required")
        self.issuer = issuer
        self.audience = audience
        self.client_id = client_id
        self.owner_sub = owner_sub
        # Never derive JWKS URL from untrusted tokens. Cache keys to limit lookups.
        self.jwks = PyJWKClient(issuer + ".well-known/jwks.json", cache_jwk_set=True,
                               lifespan=300, cache_keys=True, timeout=5)

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
        if claims["sub"] != self.owner_sub:
            raise ValueError("Access token belongs to an unknown user")
        # Auth0 uses azp for the authorized client in custom API access tokens.
        if claims.get("azp") != self.client_id:
            raise ValueError("Unexpected OAuth client")
        return claims

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await asyncio.to_thread(self.decode, token)
            if claims.get("sub") != self.owner_sub or claims.get("azp") != self.client_id:
                return None
            granted = claims.get("permissions", [])
            requested = claims.get("scope", "")
            if not isinstance(granted, list) or not all(isinstance(v, str) for v in granted):
                return None
            if not isinstance(requested, str):
                return None
            # A permission is usable only when both delegated scope and RBAC allow it.
            scopes = sorted(PERMISSIONS.intersection(granted).intersection(requested.split()))
            if "mcp:admin" not in scopes:
                return None
            return AccessToken(
                token=token, client_id=self.client_id, scopes=scopes,
                expires_at=int(claims["exp"]), resource=self.audience,
                subject=claims["sub"],
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OSError) as exc:
            LOG.warning("Auth0 bearer token rejected: %s", type(exc).__name__)
            return None
