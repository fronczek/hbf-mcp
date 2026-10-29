# Repeatable deployment and rollback

The Ansible playbook deploys a reviewed commit into an isolated systemd instance.
Its defaults are `hbf-mcp-candidate`, `/opt/hbf-mcp-candidate` and
`127.0.0.1:18765`. It does not modify the existing `hbf-mcp-artur` unit,
`/opt/hbf-mcp-artur`, proxy routes, DNS or existing helper symlinks.

## Prerequisites

Run from an Ansible controller with an administrator SSH account and become
rights on the target. The MCP bridge account has no sudo. The service runs as
the existing unprivileged `gptagent` account with `NoNewPrivileges=yes`.
The target needs network access to the source repository and Python package
index. Deployments to an instance must be serialized by the existing CI system
(for Jenkins use `disableConcurrentBuilds()`).

Provision these files using the existing secret-management mechanism:

- `/etc/hbf-mcp/candidate.env`: root-owned, mode `0600`, plain
  `KEY=value` lines. Set `HBF_MCP_OAUTH_ENABLED=true`, issuer, public MCP
  audience and the required OAuth configuration. Do not place secret values in
  inventory, Git, command arguments or CI output.
- `/etc/hbf-mcp/policy.json`: root-owned, mode `0600` or `0640`
  (a public, nonsecret policy may be `0644`). Use the documented policy
  format with exact authenticated subject/client bindings and explicit grants.

The environment source must not set `HBF_MCP_TRANSPORT`,
`HBF_MCP_POLICY_FILE`, `HBF_MCP_AUDIT_FILE`, `HBF_MCP_HOST`,
`HBF_MCP_PORT` or `HBF_MCP_AGENT_HOME`: these are fixed by the systemd
template. OAuth cannot be disabled through this deployment.

## Deploy

Create a private inventory from the example, select the administrator account,
and replace `CHANGE_ME` with a reviewed **full 40-character commit SHA**.
Do not use a moving branch or tag.

```bash
cd deploy
ansible-playbook -i /path/to/private-inventory.yml deploy.yml --syntax-check
ansible-playbook -i /path/to/private-inventory.yml deploy.yml
```

The playbook:

1. Validates root ownership, input permissions and enabled OAuth.
2. Creates a release keyed by commit plus environment/policy checksums.
3. Checks out the exact revision and installs server dependencies and the
   compatible CLI package into its own virtual environment.
4. Installs a root-private environment and root-owned policy readable by the
   service group. Source and virtual environment remain root-managed.
5. Compiles and imports the candidate with its authenticated configuration,
   without opening a listener.
6. Atomically replaces the `current` symlink and starts/restarts systemd.
7. Requires both an HTTP `401` for unauthenticated `GET /mcp` and an active
   systemd service. A failed activation restores the previous symlink,
   environment, policy and service definition, then restarts it. A failed
   first installation is stopped and its `current` pointer removed.

Compilation/import and unauthenticated rejection are deployment checks, not a
substitute for authenticated multiuser integration tests. Before changing the
public proxy, test permitted and denied tools with real client credentials on
the isolated listener. The public audience still uses the eventual HTTPS MCP
URL; no plaintext public access should be exposed.

## Rollback and inspection

Automatic rollback retains the old release directory, including that release's
environment and policy. To deliberately return to an older code/configuration
combination, rerun this playbook with the previous exact SHA and the matching
previous environment/policy source files supplied by the secret-management
system. A rollback to old code with new policy is a separate deployment and
needs its own review.

```bash
systemctl status hbf-mcp-candidate
journalctl -u hbf-mcp-candidate --since '10 minutes ago'
readlink -f /opt/hbf-mcp-candidate/current
```

No release directories are deleted automatically. Retain a known-good release
until the public cutover and consumer migration are verified. Audit events are
written to `/var/log/hbf-mcp-candidate/operations.jsonl` in a private directory
owned by the service account. Configure the existing log collector and retention
policy before production use. A local audit file is not tamper-proof against
an administrator with shell access; forward it to the existing central log
store if independent retention is required.

## Production cutover

Keep the current instance serving while testing the candidate. Record the old
proxy upstream and exact release/configuration identifiers. Switch the existing
proxy to the tested candidate only after authenticated regression checks pass;
rollback restores that upstream immediately. Update CLI installations and
helper symlinks through the infrastructure repository, then verify their real
consumers. This playbook intentionally does not archive the old repository,
replace helper symlinks, change production routing or modify the legacy
installer.

## Validation performed

The playbook and inventory were checked with `ansible-playbook --syntax-check`;
embedded Python preflight/symlink snippets were compiled. No Ansible deployment,
service restart, production cutover or live rollback was performed by this
change. Those checks require an administrator execution context and provisioned
identity/policy inputs.
