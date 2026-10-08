# HBF MCP

Personal MCP bridge for ChatGPT running on a disposable VM.

The goal is deliberately simple:

- ChatGPT stays in the normal browser chat.
- This VM exposes a small MCP server over Streamable HTTP.
- The MCP server can run shell commands, read/write files, and inspect Git.
- The MCP service account is **unprivileged**: it has no sudo and the server
  provides no privilege escalation.
- Tools that only observe the machine are annotated as read-only so clients can
  call them without confirmation; tools that can change state are not.
- Access to external systems (Proxmox, InfluxDB, SSH, GitHub, etc.) is added later using dedicated credentials.

## Architecture

```text
ChatGPT
   |
   | HTTPS
   v
external reverse proxy (e.g. Caddy)
   |
   | LAN / private network
   v
hbf-mcp VM:8765
   |
   +-- shell
   +-- filesystem
   +-- git
   +-- later: SSH / Proxmox / InfluxDB / PBS / ...
```

By default the MCP server listens on all interfaces:

```text
0.0.0.0:8765
```

The MCP endpoint is:

```text
http://<vm-address>:8765/mcp
```

Do **not** expose port 8765 directly to the Internet. The `shell` tool can run
any Bash command with the ordinary permissions of the service account. Put an
HTTPS reverse proxy in front of it and restrict direct access to port 8765 to
trusted hosts/networks.

## Requirements

Fresh Debian/Ubuntu VM with Internet access.

Suggested VM size:

- 2 vCPU
- 2-4 GB RAM
- 20 GB disk

## Install

```bash
git clone https://github.com/fronczek/hbf-mcp.git
cd hbf-mcp
sudo ./install.sh
```

Check the service:

```bash
systemctl status hbf-mcp --no-pager
journalctl -u hbf-mcp -f
ss -lntp | grep 8765
```

## Local test

The repository includes a tiny MCP client.

```bash
sudo -u gptagent /opt/hbf-mcp/.venv/bin/python /opt/hbf-mcp/test_client.py
```

It should list the available tools and call `info`.

## Tools

| Tool | `readOnlyHint` | Purpose |
|---|---|---|
| `info` | true | Bridge identity, limits and allowlists |
| `get_hostname` | true | Host name without a shell |
| `get_time` | true | Local time with time zone, plus UTC |
| `system_status` | true | Uptime, RAM, disk and load average |
| `read_logs` | true | Allowlisted service logs, line- and byte-capped |
| `list_dir` | true | Directory listing inside the read roots |
| `read_file` | true | Text file inside the read roots, no credentials |
| `read_secret_file` | true | Credential file, only if explicitly allowlisted |
| `git_status` | true | Branch and porcelain status |
| `git_diff` | true | Working-tree or staged diff |
| `kuk_boiler_watch` | true | Fixed boiler health check; takes no input |
| `shell` | **false** | Bash as the unprivileged service account |
| `write_file` | **false** | Write a text file inside the write roots |

The annotations are the official MCP `ToolAnnotations` hints. They describe what
a tool does; they are not a security boundary.

### Security model

The service runs as `gptagent`, an **unprivileged** account. It is not a
sudoer: no sudoers entry grants it privilege escalation, and neither the server
nor the installer grants or requests one:

```text
The shell tool runs with ordinary user permissions.
No privilege escalation is provided.
```

Concretely:

- **`shell` is not read-only.** It can modify anything the service account can
  modify. It is not advertised as safe merely because the account has no sudo.
- **File tools are allowlisted.** `read_file`, `list_dir`, `git_status` and
  `git_diff` are confined to `HBF_MCP_READ_ROOTS`; `write_file` is confined to
  `HBF_MCP_WRITE_ROOTS`. Every path is canonicalised with `os.path.realpath`
  *before* the allowlist check, so `..` and symlinks cannot escape a root.
- **Credentials are separate.** `read_file` refuses secret-looking names
  (`.env`, `id_ed25519`, `*.pem`, `*token*`, anything under `.ssh/`, and any
  path component such as `credentials/` or `secrets/`).
  `read_secret_file` only accepts paths in `HBF_MCP_SECRET_READ_ALLOWLIST`,
  which is empty by default.
- **Output and time are bounded.** Shell and Git calls have hard timeouts;
  shell, file, log and diff output is truncated to `HBF_MCP_MAX_OUTPUT`.

The important security boundary is outside the VM. External credentials should
be created specifically for this agent and scoped to the blast radius you
accept.

Snapshots recover the VM itself, but they do not undo actions against external
systems such as GitHub, Proxmox, DNS, PBS, or production SSH hosts.

### Authentication

The official Python MCP SDK used here can protect an HTTP endpoint with an
OAuth authorization-server provider or a bearer-token verifier (`auth=` /
`token_verifier=` on `MCPServer`). This server currently runs **without** MCP
authentication, because the ChatGPT workspace connection is already configured
that way and changing it would break the app until the client side is prepared.
Do not enable MCP auth here without agreeing the matching ChatGPT configuration
first; until then the network boundary (HTTPS reverse proxy plus restricted
port 8765 access) is what protects the endpoint.

## Configuration

Environment file:

```text
/etc/hbf-mcp.env
```

The full set of keys is in [`hbf-mcp.env.example`](hbf-mcp.env.example). The
ones that change behaviour most:

```bash
HBF_MCP_HOST=0.0.0.0
HBF_MCP_PORT=8765
HBF_MCP_READ_ROOTS=/home/gptagent        # read_file, list_dir, git_*
HBF_MCP_WRITE_ROOTS=/home/gptagent       # write_file
HBF_MCP_SECRET_READ_ALLOWLIST=           # empty: no credential access
HBF_MCP_ALLOWED_LOG_SERVICES=hbf-mcp     # read_logs
```

`HBF_MCP_READ_ROOTS`, `HBF_MCP_WRITE_ROOTS` and
`HBF_MCP_SECRET_READ_ALLOWLIST` accept colon-separated path lists; read and
write roots both default to `HBF_MCP_DEFAULT_CWD`.

After editing it:

```bash
sudo systemctl restart hbf-mcp
```

## MCP protocol

The server uses the official Python MCP SDK v2 and Streamable HTTP.

The service is configured as stateless HTTP with JSON responses because the current toolset does not require server-initiated callbacks.
