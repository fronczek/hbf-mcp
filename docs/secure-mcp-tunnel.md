# HBF MCP01 migration to OpenAI Secure MCP Tunnel

Source: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
Latest binary: https://github.com/openai/tunnel-client/releases/latest

## Goals and current boundaries
- Replace the public hidden Caddy path with a private outbound HTTPS tunnel.
- Keep the existing external path working during setup and tests; avoid lockout.
- Initially keep the existing MCP backend at `http://127.0.0.1:8765/mcp`.
- Use a dedicated system service user `hbf-tunnel`; do **not** put the API key in
  `gptagent`'s home or in Git.
- The tunnel is a transport and workspace authorization layer, *not* a Linux
  account mapper. A separate user identity and backend are still needed per agent.
- The current full-shell MCP is unauthenticated. Restrict tunnel permissions and
  app access tightly; prefer an additional OAuth identity check before broad access.

## 0. What has already been prepared
Verified official `tunnel-client` version 0.0.16 on MCP01 at
`/home/gptagent/.local/bin/tunnel-client`, SHA256SUMS.txt matches the release.
Configuration and systemd installer are in the `tunnel/` directory of this
repository. There is no tunnel ID or runtime API key on this VM yet.

## 1. OpenAI Platform — tunnel IDs already assigned

- Artur / MCP01: `tunnel_6ad8b44bd0fc8191895d81356ab3651e`
- Mariusz / MCP02: `tunnel_6ad8b484fdac8191b6d3e3f81793adb3`

Mappings are saved in `tunnel/hosts/<hostname>.conf` (identifiers, **not** keys).
Installer reads the hostname and uses the proper ID automatically.

**Still required:** one restricted runtime key **per tunnel**, created in
https://platform.openai.com/settings/organization/api-keys by principals with
**Tunnels Read + Use** on their own tunnel (not Manage). Keep keys out of
Git and chat. Associate tunnels with intended HBF ChatGPT workspace and
limit each plugin's visibility to the respective user.

## 2. Install / configure / diagnose (root terminal on MCP01)
Download/verify a complete official tunnel client (unprivileged shell):
```bash
cd /home/gptagent/projects/hbf-mcp-upstream-review
./tunnel/download-client.sh
```

From a **root shell** on MCP01 (the `gptagent` service account has no sudo):
```bash
cd /home/gptagent/projects/hbf-mcp-upstream-review
./tunnel/install.sh install
./tunnel/install.sh configure
./tunnel/install.sh doctor
```

For MCP02 repeat from its own checkout and use:
`HBF_TUNNEL_BINARY=/home/<mcp02-account>/.local/bin/tunnel-client
./tunnel/install.sh install` (replace the account path). The hostname must
be `hbfplhmcpu02` so the right ID is selected. Use a different runtime
API key. The MCP02 destination service is not accessible from MCP01 yet
over verified SSH, so this step is **not completed**.

`configure` prompts **only** for the runtime key; the tunnel ID is already
known. The script refuses to overwrite an existing key.
The `configure` command asks for tunnel ID and runtime API key. It writes only
root/group-hbf-tunnel-readable files under `/etc/hbf-mcp-tunnel/`.
`install` deliberately does not start or enable the service. Once `doctor`
passes:
```bash
systemctl enable --now hbf-mcp-tunnel
systemctl status hbf-mcp-tunnel --no-pager
curl -fsS http://127.0.0.1:18865/readyz
```
Health + local admin UI are only on loopback `127.0.0.1:18865`; optional SSH
forwarding makes them visible to an administrator. No public admin UI.
The service cannot read the home directory of `gptagent`.

## 3. ChatGPT and end-to-end proof
- ChatGPT > Plugins > + > Add custom MCP server > Connection: Tunnel.
- Select MCP01 tunnel or paste its ID. Restrict who can use the plugin.
- Prefer OAuth protection of the privileged shell where supported.
- Test `info`, then a harmless `get_hostname` read-only invocation.
- Observe the client readiness and logs; do not declare success based on local
  `doctor` alone.

## Request timeout
The OpenAI tunnel has a 120-second total request deadline, including queue time.
Long shell or agent tasks need a job ID + status/polling pattern instead of one
synchronous MCP request; keeping the client connection alive does not extend it.

## 4. Cutover, only after successful ChatGPT test
- Restrict MCP backend to `127.0.0.1:8765`, including HBF_MCP_HOST in root-owned
  `/etc/hbf-mcp.env` and a planned MCP service restart.
- Remove the old public Caddy path/route (and firewall holes if any).
- Confirm direct old URL is inaccessible externally and the tunneled plugin
  still works. Disable/retire old ChatGPT plugin.
- Do not remove `gptagent` until all runtime data, credentials, tools and
  services have been moved to dedicated accounts.
- On MCP02, repeat with Mariusz's dedicated user, separate tunnel and runtime key.

## Rollback
If tunnel discovery or calls fail, inspect `journalctl -u hbf-mcp-tunnel`,
`/readyz`, and `tunnel/install.sh doctor`. Before removing the old ingress,
the existing ChatGPT plugin remains the rollback path. After cutover, restore
the previous Caddy route and original backend bind only through an explicit
administrator change; never silently expose the full-shell endpoint.
