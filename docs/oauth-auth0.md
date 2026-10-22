# HBF MCP OAuth deployment (privileged shell)

This full-shell endpoint must require OAuth before traffic can reach the MCP tools.
Do not treat the unguessable URL as a security boundary.

The reference pattern is our existing `hbf-gpt-gitea/docs/auth0.md`. Create
**separate** Auth0 API, Regular Web Application, user-delegated client grant,
RBAC role and identity. The resource Identifier must match the full external
HTTPS `/mcp` endpoint exactly; API uses RS256 and access token permissions.
Grant the exact `mcp:admin` permission via role and delegated scope. Configure
ChatGPT callback URL exactly as reported by the ChatGPT plugin UI.

In Auth0 tenant advanced settings, enable **Resource Parameter Compatibility
Profile** so the MCP client's RFC 8707 `resource` parameter selects this API.
Configure the API for the **RFC 9068** access-token profile, enable RBAC and
enable adding permissions to access tokens. RFC 9068 uses `client_id`; legacy
Auth0 access tokens use `azp`. The verifier accepts either only when it equals
the configured client and rejects missing, conflicting or foreign values.

The existing `hbf-gpt-gitea/auth0/manage.sh` can initialize another Auth0
profile named `hbf-mcp`; review its generated profile before `setup`.

Root-owned `/etc/hbf-mcp.env` should contain:
```ini
HBF_MCP_OAUTH_ENABLED=true
HBF_MCP_AUTH0_ISSUER=https://hbf.eu.auth0.com/
HBF_MCP_PUBLIC_URL=https://PUBLIC-HOST/mcp
HBF_MCP_AUTH0_CLIENT_ID=CLIENT_ID
HBF_MCP_AUTH0_OWNER_SUB=auth0|USER_ID
```
The resource server needs no OAuth client secret. It validates JWT via issuer
JWKS, RS256, expiry, audience, authorized client (`azp` or RFC 9068
`client_id`), exact owner `sub`, and `mcp:admin` in both `scope` and
`permissions`.

**Go-live gate:** keep `HBF_MCP_OAUTH_ENABLED=false` on the existing instance.
Stage a separate named instance on loopback/temporary port; test protected-resource
discovery, missing/invalid JWT rejection, verified real login and read-only
tool invocation; keep existing ChatGPT connection working meanwhile. Then
change reverse proxy routing, block direct public TCP/8765 ingress (prefer
loopback binding), remove old unprotected route, and re-test externally.
Do not restart the current production MCP until integration is confirmed.
