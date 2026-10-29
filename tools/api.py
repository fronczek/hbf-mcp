"""Thin adapters for the imported CLI services.

Legacy MCP interfaces are kept until their contracts have equivalence tests.
The policy gate permits only explicitly reviewed read tools to restricted roles.
"""
from typing import Any, Literal
from mcp.types import ToolAnnotations
from hbf_api_tools import monitoring, dsh
from services.identity import gitea_client, youtrack_client


def register_tools(mcp):
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                           idempotent_hint=True, open_world_hint=True)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=True,
                            idempotent_hint=False, open_world_hint=True)

    @mcp.tool(annotations=read)
    def api_gitea_branches(repo: str, page: int = 1, limit: int = 30) -> dict[str, Any]:
        """List branches in one authorized repository."""
        return {"branches": gitea_client().branches(repo, page, limit)}

    @mcp.tool(annotations=read)
    def api_gitea_pulls(repo: str, state: Literal["open", "closed", "all"] = "open",
                        page: int = 1, limit: int = 20) -> dict[str, Any]:
        """List pull requests in one authorized repository."""
        return {"pull_requests": gitea_client().pull_requests(repo, state, page, limit)}

    @mcp.tool(annotations=read)
    def api_gitea_pull(repo: str, number: int) -> dict[str, Any]:
        """Read one pull request in an authorized repository."""
        return gitea_client().pull_request(repo, number)

    @mcp.tool(annotations=read)
    def api_gitea_status(repo: str, ref: str) -> dict[str, Any]:
        """Read CI status for a ref in one authorized repository."""
        return gitea_client().commit_status(repo, ref)

    @mcp.tool(annotations=read)
    def api_gitea_diff(repo: str, number: int) -> str:
        """Read a pull diff. Administrator only pending restricted-role review."""
        return gitea_client().pull_diff(repo, number)

    @mcp.tool(annotations=read)
    def api_gitea_files(repo: str, number: int, page: int = 1, limit: int = 30) -> dict[str, Any]:
        """Read pull files. Administrator only pending restricted-role review."""
        return {"files": gitea_client().pull_files(repo, number, page, limit)}

    @mcp.tool(annotations=write)
    def api_gitea_create_pr(repo: str, title: str, head: str, body: str,
                             base: str = "master") -> dict[str, Any]:
        """Create a PR using shared CLI duplicate/identity checks. Administrator only."""
        return gitea_client().create_pull_request(repo, title, head, base, body)

    @mcp.tool(annotations=write)
    def api_gitea_merge_pr(repo: str, number: int, expected_head_sha: str,
                            delete_branch: bool = True) -> dict[str, Any]:
        """Fast-forward merge with shared exact-head CI checks. Administrator only."""
        return gitea_client().merge_pull_request(repo, number,
                    expected_head_sha=expected_head_sha, delete_branch=delete_branch)

    @mcp.tool(annotations=read)
    def api_youtrack_issue(issue_id: str) -> dict[str, Any]:
        """Read an issue from an authorized project."""
        return youtrack_client().issue(issue_id)

    @mcp.tool(annotations=read)
    def api_youtrack_comments(issue_id: str, limit: int = 20) -> dict[str, Any]:
        """Read comments from an authorized project."""
        return {"comments": youtrack_client().comments(issue_id, limit)}

    @mcp.tool(annotations=write)
    def api_youtrack_add_comment(issue_id: str, text: str) -> dict[str, Any]:
        """Add a comment with shared identity checks. Administrator only."""
        return youtrack_client().add_comment(issue_id, text)

    @mcp.tool(annotations=write)
    def api_youtrack_set_state(issue_id: str, state: str) -> dict[str, Any]:
        """Change a directly writable issue state using shared CLI checks. Administrator only."""
        return youtrack_client().set_state(issue_id, state)

    @mcp.tool(annotations=read)
    def api_monitor_list() -> dict[str, Any]:
        """List packaged monitoring profile descriptions."""
        return monitoring.list_profiles(monitoring.load_monitoring_config())

    @mcp.tool(annotations=read)
    def api_monitor_zabbix(profile: str, hours: int = 24) -> dict[str, Any]:
        """Run one authorized packaged Zabbix profile; no custom paths or queries."""
        return monitoring.zabbix_report(profile, hours)

    @mcp.tool(annotations=read)
    def api_monitor_influx(profile: str, hours: int | None = None) -> dict[str, Any]:
        """Run one authorized packaged Influx profile; no custom paths or queries."""
        return monitoring.influx_report(profile, hours)

    @mcp.tool(annotations=read)
    def api_dsh_status(dispatch_id: str) -> dict[str, Any]:
        """Inspect a bounded DSH dispatch. Administrator only."""
        return dsh.dispatch_status(dispatch_id)
