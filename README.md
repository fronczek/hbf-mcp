# hbf-api-tools

Client-independent command-line tools and Python clients for HBF APIs. This repository is the shared base for local AI clients and adapters such as `hbf-mcp`.

## Design

- API logic lives here; MCP or another AI-client adapter should be a thin caller, not a second implementation.
- Tools use typed operations against fixed HBF service origins. No arbitrary URL, shell command, or SSH target is accepted.
- Credentials are never printed, logged, or included in errors.
- Credential directory resolution: `HBF_CREDENTIALS_DIR` if explicitly set; otherwise `.credentials` found by walking upward from the executable/repository location, then `~/.credentials`. The first existing directory wins. File permissions must not grant group/other access.
- The checkout-relative search makes a workspace checkout work regardless of the caller's current directory. For installations outside the workspace, set `HBF_CREDENTIALS_DIR`.

Credential contracts (flat YAML scalar keys; values remain local):

| File | Required fields |
|---|---|
| `youtrack.yml` | `youtrack_url`, `youtrack_token_value` |
| `gitea.yml` | `gitea_url`, `token_value` |
| DSH helper | `HBF_DSH_HELPER` points to the installed `dsh-remote` HTTPS helper; its Harness auth remains private to that helper |

The first CLI surface is:

```text
hbf-api youtrack search [query] [--limit N] [--skip N]
hbf-api youtrack issue ISSUE-ID
hbf-api youtrack comments ISSUE-ID [--limit N]
hbf-api youtrack add-comment ISSUE-ID --apply   # comment body from stdin
hbf-api gitea connection|branches|pulls|pull|diff|files|status ...
hbf-api gitea create-pr REPO --title TITLE --head BRANCH --base master --apply  # body from stdin
hbf-api dsh sessions [--limit N]
hbf-api dsh flash ISSUE-ID --workdir ABSOLUTE-HARNESS-PATH --apply [--max-usd N]
```

DSH task, YouTrack comment, and Gitea PR body text are read from stdin, not command-line arguments (so they do not end up in shell history or command approval prompts). Set `HBF_DSH_HELPER` to the installed helper; the default is `~/bin/dsh-remote`. Its HTTPS authentication is managed by that helper and is not yet migrated to a portable token-file contract. Task dispatch requires an explicit work directory, a bounded budget, an allowed workdir (`HBF_DSH_ALLOWED_WORKDIRS`, path-separated), and `--apply`. PR creation checks for an existing matching head/base before writing; if the HTTP outcome is ambiguous, inspect Gitea before retrying.

Gitea defaults to an exact repository allowlist; override it with `HBF_GITEA_REPOSITORIES` (comma-separated exact `owner/repo` values). Read operations are the default. Every external mutation must have an explicit, reviewable invocation and must never be retried blindly after an ambiguous network outcome.

This initial version has not performed live API calls. Network smoke tests should be opt-in and read-only by default; local unit tests must not need credentials.

## Development

The package targets Python 3.11+ and the standard library for its core. Run offline tests with `PYTHONPATH=src python3 -m unittest discover -s tests -v`.

## Use from Codex or an MCP service

Install the same package into each client's own Python environment with `python3 -m pip install .`, or deploy a built wheel pinned to a reviewed commit. The library has no MCP dependency:

```python
from hbf_api_tools.gitea import Gitea
from hbf_api_tools.youtrack import YouTrack

yt = YouTrack()
issue = yt.issue("ESE-801")
```

An MCP adapter should call these typed methods and expose only the operations it needs. Do not copy API logic into `hbf-mcp`. Set `HBF_CREDENTIALS_DIR` in the service environment if its installed package cannot find the machine-local `.credentials` directory by walking upward from its own package path. MCP server tools should mark reads and writes accurately and keep `--apply`-equivalent confirmation for mutations.

For shell-based clients, install into a dedicated virtual environment and approve only that environment's stable `hbf-api` executable prefix. The package does not and cannot override the client's command-approval policy.
