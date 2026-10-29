# Unified HBF MCP — implementation and rollout status, 2026-10-29

## Architecture

One source repository: `fronczek/hbf-mcp`. The complete API-tools history through
`196b5d744de0e37c4213f1ee9b23e29950a06d60` was imported without squashing.
The old repository remains available during consumer migration.

- `src/hbf_api_tools/`: shared Gitea, YouTrack, DSH, monitoring, HTTP and
  identity services, including the existing CLI in `cli.py`.
- `scripts/`: original dsh-remote, dsh-budget-guard and deepseek-api.
- `services/identity.py`: MCP-specific trusted downstream identity binding.
- `tools/api.py`: thin adapters calling the same services as the CLI.
- `auth/`: local policy, SDK dispatch boundary and synchronous JSON audit.
- Existing flat adapters remain for interface compatibility. Their extra
  operations were not deleted or declared equivalent merely because a CLI
  implementation looks similar.
- `deploy/`: Ansible, immutable releases, isolated candidate and rollback.
- `tests/api_tools/`: original regression suite alongside MCP/security tests.

The package distribution remains named `hbf-api-tools`; this preserves pip
metadata and the `hbf-api` entry point, and does not imply a second source project.

## Identities and authorization

HTTP uses the existing Auth0 resource-server implementation and MCP SDK OAuth
middleware. JWT validation checks fixed HTTPS issuer/JWKS, RS256 signature,
audience/resource, expiry, issued-at and not-before when present. Multiuser
tokens must carry `mcp:access` in BOTH Auth0 permissions and delegated scopes.
Auth0 must enable permissions in access tokens. The authorization server owns
login, PKCE, refresh token rotation and client registration; the MCP service
does not issue tokens or need an OAuth client secret.

Exact `(sub, azp/client_id)` bindings select roles. Conflicting client claims
are rejected. A client name, userAgent, custom header, tool argument or model
tier is not an identity. `agent` in policy is an administrator-managed label
for a trusted binding; it does not prove the model executing inside a client.

The root-owned policy and every parent directory must be non-writable by the
service account. Policy changes take effect after restart. Unknown bindings,
tools, roles and resources fail closed. Restricted roles can use only the nine
explicitly reviewed typed read tools in `RESOURCE_PARAMETERS`, with exact
repository, project or packaged monitoring-profile grants. Wildcards are
rejected for restricted roles except the global profile-list operation.
The server filters tool discovery AND checks every direct call.

Shell, files, local Git, generic search/API, all legacy tools, writes and DSH
delegation require the explicit administrator role. Delegation to DSH cannot
safely serve restricted users until the worker enforces delegated capabilities;
a prompt telling it to obey a restriction is insufficient. Administrators
retain the service account's intentional administrative authority.

Local stdio uses a root-policy mapping from the actual Unix user to one exact
user/client binding. Caller environment cannot choose another subject or
client. This governs MCP protocol access; it does not sandbox someone who
already controls that Unix account. HTTP never falls back to the stdio identity.
Separate local Unix accounts are needed for separate local OS trust boundaries.

Downstream credentials stay private to the service account. MCP bearer tokens
are never forwarded to Gitea, YouTrack or monitoring APIs. New Gitea/YouTrack
adapters inject a service identity provider that validates trusted host+Unix
binding, then the existing clients validate remote /user or /me. CLI defaults
still use the original reviewed agent-host bindings; they were not weakened.
The audit subject/client is distinct from the downstream technical account.
Legacy adapter audit mappings describe configured accounts; legacy reads have
not all been converted to independently verified downstream identities.

## Audit

Each policy-mode call records UTC time, a fresh correlation UUID, signed user
and client identity, configured agent label, roles, tool, a sanitized typed
resource target, configured downstream accounts, and started/success/error/denied
outcome. No token, arbitrary arguments, prompts, results or exception text are
logged. Request context uses ContextVars and is isolated across concurrent calls.

`HBF_MCP_AUDIT_FILE` uses private owned regular files, synchronous append/fsync,
no symlinks or hard links, and protected parents. Without this variable, JSON
is written directly to stderr for capture by the service journal. Failure to
write the start event prevents execution. Failure after an external operation
does not undo it: inspect existing state before retrying. Administrators with
shell access have local account authority; this is not a tamper-proof audit
store against administrators. Forward journals to the existing central log
stack when stronger retention is needed.

## Configuration and transports

Copy `auth/policy.example.json` to a root-managed path and replace every
placeholder with actual issuer subjects/client registrations. The example is
illustrative least-privilege access, not an automatically approved assignment.
Set service_accounts for the selected deployment host and existing technical
API account. Grant credentials separately outside Git.

Multiuser HTTP requires `HBF_MCP_POLICY_FILE` and
`HBF_MCP_OAUTH_ENABLED=true`; startup refuses policy-mode HTTP without OAuth.
Use `HBF_MCP_TRANSPORT=streamable-http` or `stdio` only. The CLI remains a local
administrative interface using each Unix account's private credentials.
Legacy startup without policy retains the previous single-admin/unauthenticated
behavior for compatibility and MUST NOT be advertised as a multiuser endpoint.
The existing `install` command is retained for old installations; use the new
root-managed Ansible release deployment for multiuser service.

Compatibility checked against official documentation on 2026-10-29:

| Client | Transport and identity | Verification here |
| --- | --- | --- |
| ChatGPT | Remote HTTP; OAuth discovery, code+PKCE, separate registered client identity | Official contract reviewed; actual account linking still requires IdP provisioning |
| Claude Code | HTTP with OAuth; local stdio also supported | Official contract reviewed; no interactive Claude login performed |
| Claude Desktop | Must use a supported remote connector or supported local configuration; do not infer identical UI/config from Claude Code | Interactive account test pending |
| DSH | Existing HTTPS helper verified read-only; MCP capabilities depend on installed Harness/client plugin | Full MCP transport/auth support has NOT been confirmed from the current DSH deployment |
| Local stdio | Unix-bound startup policy; no HTTP OAuth exchange | Actual SDK subprocess test |
| Streamable HTTP | SDK transport with protected-resource discovery and bearer validation | Actual loopback HTTP tests with real RSA JWT verification |

No historical SSE transport was added. Existing SDK protocol compatibility is
used rather than implementing custom protocol negotiation.

Sources:
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://developers.openai.com/plugins/build/auth
- https://code.claude.com/docs/en/mcp

## Deployment and remaining rollout gates

See [deployment.md](deployment.md). Defaults create a separate loopback candidate
on port 18765; they do not replace the active bridge. Root-managed release code,
policy and startup environment prevent restricted users changing their grants.
The candidate import is preflighted as the actual unprivileged account, then
the current symlink is atomically switched; HTTP401 and active systemd state
are checked, with restoration of the previous release/unit on failure.

The live bridge is `hbf-mcp-artur.service`, running as `gptagent` from
`/opt/hbf-mcp-artur` on port8765. Its source predates repository OAuth integration.
The generic `hbf-mcp.service` is masked. No live MCP restart, public routing
change, IdP modification or production cutover was performed by this migration.

Outstanding before retiring the old repository:
1. Provision real user/client bindings and OAuth permissions; test Artur and
   Mariusz linking separately from ChatGPT/Claude.
2. Run candidate deployment with an authorized administrative SSH account.
   This bridge account has no sudo. Validate genuine upstream read integrations.
3. Complete consumer inventory on vps1123, DSH and Jenkins. SSH from this bridge
   to vps1123 failed strict host-key verification; no unknown key was accepted.
4. Switch remaining installed CLI wheels/checkouts, automation install URLs and
   guidance links to the consolidated repository; preserve rollback versions.
5. Prove legacy adapter contract equivalence before removing overlapping logic.
6. Confirm DSH MCP client capabilities and impose worker-side capability
   attenuation before allowing restricted-user delegation.

## Consumer observations

Read-only inspection found:
- `~/bin/hbf-api` -> `~/.local/share/hbf-api-tools-venv/bin/hbf-api`.
- `~/bin/dsh-remote`, `dsh-budget-guard`, `deepseek-api` point into the old
  API-tools checkout. Their imported script contents are byte-identical.
- `dsh-budget-guard.service` executes the stable `~/bin/dsh-budget-guard`
  path; its timer runs every minute.
- Active legacy MCP agent tools execute `~/bin/dsh-remote`.
- ESE agent guidance links to the old canonical helper source.
- No hbf-api-tools usage was found in the inspected ansible-vai YAML/Jenkins
  files. This is a bounded local search, not proof that no Jenkins consumer exists.

No old repository, existing branch, unrelated worktree, credential or unknown
consumer was deleted. Live health checks also report absent Gitea/YouTrack credentials in the bridge
runtime and failed upstream identity/connectivity checks. The service itself is
active and its current source fingerprint matches disk. Do not copy another
agent's credentials to make these checks pass; provision the selected service
identity on the chosen host through the existing secret-management mechanism.

Source consolidation is implemented; full consumer and production migration
remains explicitly open.
