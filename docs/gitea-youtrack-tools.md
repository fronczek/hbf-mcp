# HBF MCP - Gitea and YouTrack API tools

Source: https://github.com/fronczek/hbf-mcp
Existing MCP service: /opt/hbf-mcp/server.py on hbfplhmcpu01

## Tools

YouTrack (7): `youtrack_issue_overview`, `youtrack_issue_detail`, `youtrack_search_issues`, `youtrack_issue_comments`, `youtrack_add_comment`, `youtrack_append_decision`, `youtrack_apply_state`.

Gitea (29):
- Connection: gitea_connection, gitea_get_repository
- Code and refs: gitea_list_branches, gitea_get_branch, gitea_read_path, gitea_list_commits, gitea_get_commit, gitea_compare_refs, gitea_get_tree, gitea_search_code
- Issues: gitea_list_issues, gitea_get_issue
- PR: gitea_list_pull_requests, gitea_get_pull_request, gitea_get_pull_diff, gitea_list_pull_files, gitea_list_reviews, gitea_list_comments
- CI: gitea_commit_status, gitea_list_action_runs, gitea_list_action_jobs, gitea_read_action_log, gitea_ci_summary
- Audit: gitea_operation_status
- Mutations: gitea_add_comment, gitea_create_issue, gitea_create_pull_request, gitea_submit_review, gitea_create_branch

## Safety

These tools are typed HTTP REST operations; they never use the shell, SSH or the DSH host.
GPT credentials are kept under /home/gptagent/credentials and never exposed to tool callers.
Gitea HTTPS origin is fixed to git.hbf.pl, no redirects, with exact repository allowlist.
Default repositories: secnet/ese-agent, secnet/ese-backend, secnet/ese-frontend,
secnet/ese-ansible, hbf/hbf-gpt-gitea, hbf/hbf-gpt-mailer,
hbf/hbf-azure-ocr, hbf/hbf-runner-u26. Override with HBF_GITEA_MCP_REPOSITORIES
as a comma-separated exact allowlist.

Mutations verify the active Gitea account login is gpt and use a
required idempotency key. SQLite reservations are stored locally under
/home/gptagent/.local/state/hbf-mcp-gitea/operations.sqlite3.
Ambiguous outcomes are not automatically retried. PR reviews require
the current PR head SHA to match the reviewed commit.
Do not expose arbitrary API URL, unsigned Git commits, force-push,
force-merge, repository deletion, or shell execution in this module.
Signed Git commits and merges use separate trusted Git/GPG workflows.

## Installation

The current server uses MCPServer from MCP SDK 2. Import each module
from the server.py file and register against the existing mcp instance:

    from gitea_tools import register_tools as register_gitea_tools
    from youtrack_tools import register_tools as register_youtrack_tools
    register_gitea_tools(mcp)
    register_youtrack_tools(mcp)
    from git_workflow_tools import register_tools as register_git_workflow_tools
    from agent_tools import register_tools as register_agent_tools
    from diagnostics_tools import register_tools as register_diagnostics_tools
    register_git_workflow_tools(mcp)
    register_agent_tools(mcp)
    register_diagnostics_tools(mcp)

Copy all integration modules with `install.sh`, then restart `hbf-mcp.service`.
Copying and syntax validation alone DO NOT activate tools; the old
process serves until a controlled service restart.
After restart refresh ChatGPT's MCP tool list, then test
gitea_connection and youtrack_issue_overview on ESE-801.

## Tests

    cd <checkout-of-hbf-mcp>
    .venv/bin/python -m pytest -q

Gitea live smoke covers READ-ONLY operations and confirms login gpt.
No live write/mutation smoke is performed without a concrete task.
Known limitation: YouTrack description updates have no CAS revision
guard; avoid concurrent manual edits of a description.

Source of truth: existing GitHub repository fronczek/hbf-mcp.

## Bounded MCP01 workflows

**Git (6 tools)**: `git_workflow_status`, `git_workflow_sync`, `git_workflow_branch`,
`git_workflow_stage`, `git_workflow_signed_commit`, `git_workflow_push`.
Each command uses a fixed allowlist of **local MCP01** working copies; no arbitrary
path/SSH host/executable can be passed. Network operations use an allowlisted
`origin` (github.com or git.hbf.pl). Git hooks are disabled for these typed
workflows. Signed commits/pushes require the configured GPT identity, current
expected HEAD, and verified GPG fingerprint. Topic branches only; merging to
master stays a deliberate separately reviewed Git/GPG operation.

**DSH (2 tools)**: `deepseek_sessions` and `deepseek_flash_task`. These invoke
only the locally pinned `~/bin/dsh-remote` helper, which uses the authenticated
DSH Harness **HTTPS API**. The second tool requires a specific `ESE-xxx` issue,
owner-authorized task and budget (default USD 0.05, maximum USD 0.20 per task),
refuses concurrent Flash sessions by default, and outputs a bounded result. No
new SSH login or remote filesystem access. A timed-out request may have started;
inspect the DSH session before retrying rather than dispatching twice.

**Browser (1 tool)**: `ese_browser_gate` runs the already versioned
`ese-agent/tools/browser` Playwright runner. It needs the existing private ESE
browser test config and `npm ci` installed locally; no automatic dependency
installation. The test scripts strictly restrict test domains and target IPs.
It returns only pass/fail and selected finding lines; screenshots/reports remain
private on MCP01.

**Diagnostics (2 tools)**: `mcp_health` checks local service/readiness without
reading secrets, while `mcp_api_health` checks GPT account identities on the
configured Gitea and YouTrack APIs; does not run SSH or probe arbitrary URLs.

**Gitea CI summary (1 extra tool)**: `gitea_ci_summary` pulls recent run statuses
and the first failed run's failing job names without dumping full logs.

### SSH policy

The normal `shell` tool remains part of MCP01. SSH is available when appropriate,
but **remote shell/file/credential access to any other host needs the owner's
prior approval and a justified scope**. Git-over-SSH to the fixed repository
remotes is distinct from interactive remote shell access. Never use other
agents' identity, tokens or home directories. Prefer typed API methods when
that avoids repeated shell calls and token-heavy logs; this is a usability
preference, not a blanket SSH prohibition.

### Activation and verification

PR publication does not activate runtime tools. Administrator should review
and merge, deploy from source to `/opt/hbf-mcp` with the existing installer or
copy source modules, then do a controlled `systemctl restart hbf-mcp.service`.
The installed `/etc/hbf-mcp.env` must remain private and untouched. Refresh
ChatGPT's MCP tool list and verify `mcp_health`, `git_workflow_status`,
`gitea_connection`, `youtrack_issue_overview` with read-only operations.

The existing GitHub CI/fixture suites run without access to local GPT secrets.
Some convenience tools require live local credentials and are validated using
mocked offline tests; no DeepSeek Flash or live Playwright gate is run as part
of the normal unit suite.
