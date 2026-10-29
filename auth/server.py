"""Single authorization boundary for all MCP tool calls.

Restricted roles can call only curated typed reads. Administrative legacy tools
remain available only to explicitly bound administrators. No caller-supplied
headers, tool arguments, initialize metadata or environment changes select roles.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import uuid

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

from .policy import Policy, Principal, correlation_id, current_policy, current_principal
from .audit import AuditWriter


class AuthorizedMCPServer(MCPServer):
    def __init__(self, *args, policy: Policy | None = None, stdio_identity: tuple[str, str] | None = None,
                 audit_writer=None, **kwargs):
        self.policy = policy
        self.stdio_identity = stdio_identity
        self.audit_writer = audit_writer or AuditWriter(os.getenv("HBF_MCP_AUDIT_FILE"))
        super().__init__(*args, **kwargs)

    def _principal(self) -> Principal:
        assert self.policy is not None
        token = get_access_token()
        if token is not None:
            if "mcp:access" not in token.scopes:
                raise PermissionError("Required delegated scope missing")
            if token.expires_at is None or token.expires_at <= time.time():
                raise PermissionError("Access token expired")
            return self.policy.principal(token.subject, token.client_id)
        # The identity is set by trusted startup configuration, not tool input.
        # Explicit transport check prevents HTTP requests receiving a local role.
        if os.getenv("HBF_MCP_TRANSPORT") == "stdio" and self.stdio_identity is not None:
            return self.policy.principal(*self.stdio_identity)
        raise PermissionError("Authenticated identity required")

    async def list_tools(self):
        if self.policy is None:
            return await super().list_tools()
        try:
            principal = self._principal()
        except PermissionError:
            return []
        return [tool for tool in await super().list_tools()
                if self.policy.can_list(principal, tool.name)]

    async def call_tool(self, name, arguments, context=None):
        if self.policy is None:
            return await super().call_tool(name, arguments, context)
        identifier = str(uuid.uuid4())
        event = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "correlation_id": identifier,
            "tool": name if isinstance(name, str) and len(name) < 128 else "invalid",
            "subject": None, "client_id": None, "agent": None,
            "technical_account": None, "resource": None, "roles": [],
            "target": self.policy.audit_target(name, arguments),
            "downstream_accounts": {service: account["login"]
                                    for service, account in self.policy.service_accounts.items()},
        }
        principal = None
        try:
            principal = self._principal()
            event.update(subject=principal.subject, client_id=principal.client_id,
                         agent=principal.agent, technical_account=principal.technical_account,
                         roles=list(principal.roles))
            event["resource"] = self.policy.resource(name, arguments)
            self.policy.authorize(principal, name, arguments)
        except (PermissionError, TypeError, ValueError):
            try:
                self.audit_writer({**event, "outcome": "denied"})
            except Exception:
                raise ToolError("Audit unavailable; operation not executed") from None
            raise ToolError("Operation not permitted") from None
        # A failed start record prevents operation execution. Production writer
        # should use an append-only persistent sink (e.g. system journal).
        try:
            self.audit_writer({**event, "outcome": "started"})
        except Exception:
            raise ToolError("Audit unavailable; operation not executed") from None
        handles = (
            current_policy.set(self.policy),
            current_principal.set(principal),
            correlation_id.set(identifier),
        )
        try:
            result = await super().call_tool(name, arguments, context)
            outcome = "error" if getattr(result, "is_error", False) else "success"
        except BaseException:
            self.audit_writer({**event, "outcome": "error"})
            raise
        else:
            self.audit_writer({**event, "outcome": outcome})
            return result
        finally:
            current_policy.reset(handles[0])
            current_principal.reset(handles[1])
            correlation_id.reset(handles[2])

    # No resource/prompt surface currently exists. Keep it fail-closed in policy
    # mode so adding one cannot silently bypass the tool policy.
    async def list_resources(self):
        if self.policy is not None:
            return []
        return await super().list_resources()

    async def list_resource_templates(self):
        if self.policy is not None:
            return []
        return await super().list_resource_templates()

    async def read_resource(self, uri, context=None):
        if self.policy is not None:
            raise ToolError("Resource access is not configured")
        return await super().read_resource(uri, context)

    async def list_prompts(self):
        if self.policy is not None:
            return []
        return await super().list_prompts()

    async def get_prompt(self, name, arguments=None):
        if self.policy is not None:
            raise ToolError("Prompt access is not configured")
        return await super().get_prompt(name, arguments)
