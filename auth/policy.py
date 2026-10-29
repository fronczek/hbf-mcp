"""Trusted local policy: exact user/client bindings and deny-by-default grants."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any

ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ISSUE = re.compile(r"^([A-Z][A-Z0-9_]*)-[1-9][0-9]{0,9}$")
PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
# Legacy shell, files, Git, generic API/search and delegation are intentionally
# absent. Such tools can exercise the service account's full authority.
RESOURCE_PARAMETERS = {
    "api_gitea_branches": ("repo", "repo"),
    "api_gitea_pulls": ("repo", "repo"),
    "api_gitea_pull": ("repo", "repo"),
    "api_gitea_status": ("repo", "repo"),
    "api_youtrack_issue": ("issue_id", "project"),
    "api_youtrack_comments": ("issue_id", "project"),
    "api_monitor_zabbix": ("profile", "profile"),
    "api_monitor_influx": ("profile", "profile"),
    "api_monitor_list": (None, "global"),
}

@dataclass(frozen=True)
class Principal:
    subject: str
    client_id: str
    roles: tuple[str, ...]
    agent: str | None = None
    technical_account: str | None = None

current_policy: ContextVar["Policy | None"] = ContextVar("hbf_policy", default=None)
current_principal: ContextVar[Principal | None] = ContextVar("hbf_principal", default=None)
correlation_id: ContextVar[str | None] = ContextVar("hbf_correlation_id", default=None)

def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(x) < 32 for x in value):
        raise ValueError("Invalid policy " + label)
    return value

class Policy:
    def __init__(self, data: dict):
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported policy version")
        self.roles = data.get("roles")
        if not isinstance(self.roles, dict):
            raise ValueError("Policy roles must be an object")
        for role, definition in self.roles.items():
            if not isinstance(role, str) or not ROLE.fullmatch(role) or not isinstance(definition, dict):
                raise ValueError("Invalid policy role")
            tools = definition.get("tools")
            if not isinstance(tools, dict):
                raise ValueError("Role tools must be an object")
            for tool, resources in tools.items():
                _string(tool, "tool")
                if not isinstance(resources, list) or not resources or any(not isinstance(x, str) or not x for x in resources):
                    raise ValueError("Tool resources must be a nonempty string list")
                if role != "admin" and (tool not in RESOURCE_PARAMETERS or "*" in resources):
                    # A single global selector is necessary for monitor-list only.
                    if not (tool == "api_monitor_list" and resources == ["*"]):
                        raise ValueError("Nonadmin role grants unsafe or wildcard tool/resource")
                if role == "admin" and (tool != "*" or resources != ["*"]):
                    raise ValueError("Admin role must explicitly grant '*': ['*']")
        bindings = data.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            raise ValueError("Policy requires explicit bindings")
        self._bindings: dict[tuple[str, str], Principal] = {}
        for item in bindings:
            if not isinstance(item, dict):
                raise ValueError("Invalid policy binding")
            subject = _string(item.get("subject"), "subject")
            client = _string(item.get("client_id"), "client_id")
            roles = item.get("roles")
            if not isinstance(roles, list) or not roles or any(not isinstance(x, str) or x not in self.roles for x in roles):
                raise ValueError("Binding contains unknown role")
            key = (subject, client)
            if key in self._bindings:
                raise ValueError("Duplicate policy binding")
            extras = {name: _string(item[name], name) if name in item else None
                      for name in ("agent", "technical_account")}
            self._bindings[key] = Principal(subject, client, tuple(roles), **extras)
        self.stdio_bindings = {}
        stdio = data.get("stdio_bindings", [])
        if not isinstance(stdio, list):
            raise ValueError("stdio_bindings must be a list")
        for binding in stdio:
            if not isinstance(binding, dict):
                raise ValueError("Invalid stdio binding")
            username = _string(binding.get("unix_user"), "unix_user")
            pair = (_string(binding.get("subject"), "subject"),
                    _string(binding.get("client_id"), "client_id"))
            if pair not in self._bindings or username in self.stdio_bindings:
                raise ValueError("Duplicate or unknown stdio identity")
            self.stdio_bindings[username] = pair
        accounts = data.get("service_accounts", {})
        if not isinstance(accounts, dict):
            raise ValueError("service_accounts must be an object")
        self.service_accounts = {}
        for service, account in accounts.items():
            if service not in ("gitea", "youtrack") or not isinstance(account, dict):
                raise ValueError("Unknown service account")
            if set(account) != {"login", "host", "unix_user"}:
                raise ValueError("Service account requires login, host and unix_user")
            self.service_accounts[service] = {name: _string(account[name], name) for name in account}

    @property
    def bindings(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._bindings)

    def principal(self, subject: str, client_id: str) -> Principal:
        try:
            return self._bindings[(subject, client_id)]
        except (KeyError, TypeError):
            raise PermissionError("Identity has no policy binding") from None

    def is_admin(self, principal: Principal) -> bool:
        return "admin" in principal.roles and self.roles.get("admin", {}).get("tools", {}).get("*") == ["*"]

    def can_list(self, principal: Principal, tool: str) -> bool:
        return self.is_admin(principal) or (
            tool in RESOURCE_PARAMETERS and any(tool in self.roles[r]["tools"] for r in principal.roles)
        )

    def resource(self, tool: str, arguments: dict) -> str:
        spec = RESOURCE_PARAMETERS.get(tool)
        if spec is None:
            return "administrative"
        param, kind = spec
        if kind == "global":
            return "*"
        value = arguments.get(param)
        if not isinstance(value, str):
            raise PermissionError("Resource is missing or invalid")
        if kind == "repo":
            if not REPO.fullmatch(value) or any(x in (".", "..") for x in value.split("/")):
                raise PermissionError("Invalid repository resource")
        elif kind == "project":
            match = ISSUE.fullmatch(value)
            if not match:
                raise PermissionError("Invalid issue resource")
            value = match[1]
        elif not PROFILE.fullmatch(value):
            raise PermissionError("Invalid monitoring resource")
        return value

    def audit_target(self, tool: str, arguments: dict) -> dict:
        """Bounded typed targets only; arbitrary arguments never enter the log."""
        if not isinstance(arguments, dict):
            return {}
        target = {}
        for key, expression in (("issue_id", ISSUE), ("profile", PROFILE),
                                ("instance", PROFILE), ("environment", PROFILE),
                                ("repo", REPO)):
            value = arguments.get(key)
            if isinstance(value, str) and len(value) <= 256 and expression.fullmatch(value):
                target[key] = value
        # Filesystem values may themselves contain credentials or secret URL
        # query strings. Hash them rather than serializing unvalidated input.
        for key in ("path", "cwd"):
            value = arguments.get(key)
            if isinstance(value, str) and len(value) <= 4096:
                target[key + "_sha256"] = hashlib.sha256(value.encode()).hexdigest()
        return target

    def authorize(self, principal: Principal, tool: str, arguments: dict) -> str:
        resource = self.resource(tool, arguments)
        if self.is_admin(principal):
            return resource
        if tool not in RESOURCE_PARAMETERS:
            raise PermissionError("Administrative tool denied")
        if not any(resource in self.roles[r]["tools"].get(tool, []) for r in principal.roles):
            raise PermissionError("Tool/resource denied")
        return resource

def load_policy(path: str) -> Policy:
    target = Path(path)
    # The server account (and shell tool) must not be able to grant new roles.
    # Require root-owned file and parents, without symlinks or writable parents.
    if not target.is_absolute():
        raise ValueError("Policy path must be absolute")
    for part in (target, *target.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Policy and parents must be root owned and not group/world writable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 1_000_000:
            raise ValueError("Unsafe policy file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return Policy(json.load(stream))
    finally:
        if descriptor != -1:
            os.close(descriptor)
