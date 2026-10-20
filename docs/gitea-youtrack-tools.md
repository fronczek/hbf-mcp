# HBF MCP - Gitea and YouTrack API tools

Source: https://github.com/fronczek/hbf-mcp
Existing MCP service: /opt/hbf-mcp/server.py on hbfplhmcpu01

## Tools

YouTrack (3): youtrack_issue_overview, youtrack_add_comment, youtrack_append_decision.

Gitea (28):
- Connection: gitea_connection, gitea_get_repository
- Code and refs: gitea_list_branches, gitea_get_branch, gitea_read_path, gitea_list_commits, gitea_get_commit, gitea_compare_refs, gitea_get_tree, gitea_search_code
- Issues: gitea_list_issues, gitea_get_issue
- PR: gitea_list_pull_requests, gitea_get_pull_request, gitea_get_pull_diff, gitea_list_pull_files, gitea_list_reviews, gitea_list_comments
- CI: gitea_commit_status, gitea_list_action_runs, gitea_list_action_jobs, gitea_read_action_log
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

Copy the two modules to /opt/hbf-mcp, then restart the hbf-mcp.service.
Copying and syntax validation alone DO NOT activate tools; the old
process serves until a controlled service restart.
After restart refresh ChatGPT's MCP tool list, then test
gitea_connection and youtrack_issue_overview on ESE-801.

## Tests

    cd /home/gptagent/projects/hbf-mcp
    /opt/hbf-mcp/.venv/bin/python -m unittest discover -s tests -v

Gitea live smoke covers READ-ONLY operations and confirms login gpt.
No live write/mutation smoke is performed without a concrete task.
Known limitation: YouTrack description updates have no CAS revision
guard; avoid concurrent manual edits of a description.

Source of truth: existing GitHub repository fronczek/hbf-mcp.
