# hbf-api-tools

Client-independent command-line tools and Python clients for HBF APIs. This repository is the shared base for local AI clients and adapters such as `hbf-mcp`.

## Design

- API logic lives here; MCP or another AI-client adapter should be a thin caller, not a second implementation.
- Tools use typed operations against fixed HBF service origins. No arbitrary URL, shell command, or SSH target is accepted.
- Credentials are never printed, logged, or included in errors.
- Credential directory resolution: `~/.credentials` of the actual Unix user **only**, unless `HBF_CREDENTIALS_DIR` is explicitly set (primarily for tests). No checkout-relative searches and no fallback to `~/credentials`. The directory must be owned by the invoking user and private (`0700`); credential files must be regular, owned files (`0600`).
- The private `~/.credentials/agent.yml` pins the expected agent login, allowed execution host, and Unix account; HBF API actions deny mismatches. Remote Gitea/YouTrack `/user` or `/me` identity checks deny a token for the owner or another agent, **including reads**. The host/Unix mapping is reviewed code, not CLI-controlled.
- Identity is **not portable across agents**. Never use, copy, import, delegate, sign with or select another agent's Git/GPG wrappers, credentials, cookies, or working tree. A technical fallback cannot silently use the owner's connected OpenAI plugin identity.
- All Gitea PR descriptions and YouTrack comments published by `gpt` receive `added by gpt-lindir` automatically. DSH actions require a caller-supplied `added by dsh-flash` or `added by dsh-pro` marker; the shared DSH token proves only the `dsh` ACCOUNT, not the model tier. Model selection needs separate Harness verification.
- **Security limit:** because the current DSH Unix user historically has both agents' private keyrings, code-level checks are not a full access-control boundary against a process that can directly access foreign keys. Physically separate Unix accounts/keyrings and remove foreign wrappers in a separately approved DSH maintenance action. This repository does not touch the DSH host or its private keys.

Credential contracts (flat YAML scalar keys; values remain local):

| File | Required fields |
|---|---|
| `youtrack.yml` | `youtrack_url`, `youtrack_token_value` |
| `gitea.yml` | `gitea_url`, `token_value` |
| `agent.yml` | `agent_login`, `agent_host`, `agent_unix_user` (must match reviewed host binding) |
| DSH helper | `HBF_DSH_HELPER` points to the installed `dsh-remote` HTTPS helper; its Harness auth remains private to that helper |
| Zabbix monitoring | `zabbix.yml`: `zabbix_url`, `zabbix_token_value` |
| Influx monitoring | `influxdb_<bucket>.yml`: `influx_url`, `influx_org`, `influx_bucket`, `influx_token` |

Identity profiles (no tokens in this sample):

```yaml
# hbfplhmcpu01: /home/gptagent/.credentials/agent.yml, mode 0600
agent_login: gpt
agent_host: hbfplhmcpu01
agent_unix_user: gptagent
```

The DSH-specific profile, provisioned by the owner **on DSH only**, uses `dsh`, `hbfplhdshu01` and `afronczek`. Never exchange credential files or signing helpers between these hosts. Automatic agent work and writes are limited to their bound hosts; owner-interactive plugin actions are distinct and must not masquerade as agent work.

The first CLI surface is:

```text
hbf-api youtrack search [query] [--limit N] [--skip N]
hbf-api youtrack issue ISSUE-ID
hbf-api youtrack comments ISSUE-ID [--limit N]
hbf-api youtrack add-comment ISSUE-ID --apply   # comment body from stdin
hbf-api gitea connection|branches|pulls|pull|diff|files|status ...
hbf-api gitea create-pr REPO --title TITLE --head BRANCH --base master --apply  # body from stdin
hbf-api gitea mark-merged REPO NUMBER --head EXACT_PR_SHA --commit SIGNED_MASTER_MERGE_SHA --apply
hbf-api gitea close-integrated REPO NUMBER --head EXACT_PR_SHA --commit SIGNED_MASTER_MERGE_SHA --apply
hbf-api dsh sessions [--limit N]
hbf-api dsh flash ISSUE-ID --workdir ABSOLUTE-HARNESS-PATH --apply [--max-usd N]
hbf-api monitor list
hbf-api monitor zabbix PROFILE [--hours 24] [--config PATH]
hbf-api monitor influx PROFILE [--hours N] [--config PATH]
```

DSH task, YouTrack comment, and Gitea PR body text are read from stdin, not command-line arguments (so they do not end up in shell history or command approval prompts). Set `HBF_DSH_HELPER` to the installed helper; the default is `~/bin/dsh-remote`. Its HTTPS authentication is managed by that helper and is not yet migrated to a portable token-file contract. Task dispatch requires an explicit work directory, a bounded budget, an allowed workdir (`HBF_DSH_ALLOWED_WORKDIRS`, path-separated), and `--apply`. PR creation checks for an existing matching head/base before writing; if the HTTP outcome is ambiguous, inspect Gitea before retrying. Manual-merge recording only marks a PR as merged after a separate **locally signed and already pushed** two-parent master merge, exact head validation and green CI; it does not run a Git merge or create an unsigned commit. If the repository disables `manually-merged` (HTTP 405), `close-integrated` is an **explicit**, independently verified fallback: it posts an agent-attributed audit note and closes the already-integrated PR. Gitea will show **Closed**, not **Merged**, while the signed master commit remains the authoritative code evidence.

Gitea defaults to an exact repository allowlist; override it with `HBF_GITEA_REPOSITORIES` (comma-separated exact `owner/repo` values). Read operations are the default. Every external mutation must have an explicit, reviewable invocation and must never be retried blindly after an ambiguous network outcome.

Live API checks are opt-in and read-only; local unit tests and CI do not need credentials.

## Development

The package targets Python 3.11+ and uses PyYAML for safe monitoring-profile parsing. Run offline tests with `PYTHONPATH=src python3 -m unittest discover -s tests -v`.

## Use from Codex or an MCP service

Install the same package into each client's own Python environment with `python3 -m pip install .`, or deploy a built wheel pinned to a reviewed commit. The library has no MCP dependency:

```python
from hbf_api_tools.gitea import Gitea
from hbf_api_tools.youtrack import YouTrack

yt = YouTrack()
issue = yt.issue("ESE-801")
```

An MCP adapter should call these typed methods and expose only the operations it needs. Do not copy API logic into `hbf-mcp`. Install the package under the intended agent Unix account and provide that account's private `~/.credentials`; set `HBF_CREDENTIALS_DIR` only for explicitly reviewed alternate installations and tests. MCP server tools should mark reads and writes accurately and keep `--apply`-equivalent confirmation for mutations.

For shell-based clients, install into a dedicated virtual environment and approve only that environment's stable `hbf-api` executable prefix. The package does not and cannot override the client's command-approval policy.

## Configuration-driven monitoring

`hbf-api monitor` runs read-only Zabbix and Influx checks defined in the packaged
[`src/hbf_api_tools/monitoring.yml`](src/hbf_api_tools/monitoring.yml). Override
the YAML with `--config PATH` to add or tune named profiles without editing
client code. It uses `safe_load`; profiles define selectors, windows, thresholds
and supported rule types, never credentials or arbitrary API/Flux requests.
Credentials stay in private `~/.credentials/zabbix.yml` and
`~/.credentials/influxdb_<bucket>.yml` files; each Influx profile selects the
credential file matching its bucket.

The initial Zabbix profile selects `hbfplhpved*` hosts and CPU/NVMe temperature
items. The initial Influx profile models the Kuk-Boiler Home Assistant entities,
derives office L1 load, and evaluates its four existing consistency rules.
Windows are bounded to 1–168 hours and host/item/signal/rule results have
explicit limits. CI uses Gitea's `ubuntu-latest`; the moving runner image may
change over time. Offline tests use mocked API data: they do not prove live
connectivity or validate deployed sensor/entity names.

Profile fields:

- Zabbix `hosts` and metric `item_keys` accept literal selectors with `*`
  wildcards; metrics may define numeric `thresholds`. Alert severity and
  availability-name matching are profile fields.
- Influx profiles choose `credentials` by bucket suffix and define `signals`
  from an exact `_field`, optional `_measurement`, and exact `tags` map. Each
  signal may set `range_hours` and a bounded `aggregate_window`/function.
  `derived_signals` supports add, subtract, multiply, and divide. Rules use the
  built-in generic types `threshold`, `sustained_delta`,
  `sustained_range_mismatch`, `sustained_pair`, and `paired_window_delta`.
- Without `--hours`, per-signal ranges and the profile's evaluation window
  apply; an explicit `--hours` overrides them for the requested report.
- A missing or stale required signal yields `UNKNOWN`; it is not reported as
  healthy merely because no alert rule matched.
