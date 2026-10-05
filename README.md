# HBF MCP

Personal MCP bridge for ChatGPT running on a disposable VM.

The goal is deliberately simple:

- ChatGPT stays in the normal browser chat.
- This VM exposes a small MCP server over Streamable HTTP.
- The MCP server can run shell commands, read/write files, and inspect Git.
- The service account has passwordless sudo on this VM.
- Access to external systems (Proxmox, InfluxDB, SSH, GitHub, etc.) is added later using dedicated credentials.

## Architecture

```text
ChatGPT
   |
   | MCP / authenticated private tunnel
   v
hbf-mcp VM
   |
   +-- shell
   +-- filesystem
   +-- git
   +-- later: SSH / Proxmox / InfluxDB / PBS / ...
```

The MCP endpoint binds to localhost only by default:

```text
http://127.0.0.1:8765/mcp
```

Do **not** expose port 8765 directly to the Internet. The `shell` tool is intentionally unrestricted.

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

## Tools in v1

- `info`
- `shell`
- `read_file`
- `write_file`
- `list_dir`
- `git_status`
- `git_diff`

### Security model

This first version intentionally gives the MCP agent broad power **inside this disposable VM**.

The service runs as `gptagent`, not root, but:

```text
gptagent ALL=(ALL) NOPASSWD:ALL
```

So the `shell` tool can use `sudo` whenever needed.

The important security boundary is outside the VM. External credentials should be created specifically for this agent and scoped to the blast radius you accept.

Snapshots recover the VM itself, but they do not undo actions against external systems such as GitHub, Proxmox, DNS, PBS, or production SSH hosts.

## Configuration

Environment file:

```text
/etc/hbf-mcp.env
```

After editing it:

```bash
sudo systemctl restart hbf-mcp
```

## MCP protocol

The server uses the official Python MCP SDK v2 and Streamable HTTP.

The service is configured as stateless HTTP with JSON responses because the current toolset does not require server-initiated callbacks.
