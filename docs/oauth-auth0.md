# HBF MCP OAuth deployment (privileged shell)

This full-shell endpoint must require OAuth before traffic can reach the MCP tools.
Do not treat the unguessable URL as a security boundary.

The reference pattern is our existing `hbf-gpt-gitea/docs/auth0.md`. Create
**separate** Auth0 API, Regular Web Application, user-delegated client grant,
RBAC role and identity. The resource Identifier must match the full external
HTTPS `/mcp` endpoint exactly; API uses RS256 and access token permissions.
Grant the exact `mcp:admin` permission via role and delegated scope. Configure
ChatGPT callback URL exactly as reported by the ChatGPT plugin UI.

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
JWKS, RS256, expiry, audience, authorized client `azp`, exact owner `sub`,
and `mcp:admin` in both `scope` and `permissions`.

**Go-live gate:** stage on loopback/temporary port; test protected-resource
discovery, missing/invalid JWT rejection, verified real login and read-only
tool invocation; keep existing ChatGPT connection working meanwhile. Then
change reverse proxy routing, block direct public TCP/8765 ingress (prefer
loopback binding), remove old unprotected route, and re-test externally.
Do not restart the current production MCP until integration is confirmed.
