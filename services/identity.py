"""Trusted downstream service identity, separate from the OAuth end user.

CLI identity defaults remain unchanged. Only the authenticated server request
context can select a service binding; arguments cannot choose credentials.
"""
import os
import pwd
import socket

from auth.policy import current_policy, current_principal, correlation_id


def service_actor(service: str) -> str:
    policy = current_policy.get()
    principal = current_principal.get()
    if policy is None or principal is None:
        raise PermissionError("Authenticated service context required")
    binding = policy.service_accounts.get(service)
    if not binding:
        raise PermissionError("Downstream service identity is not provisioned")
    host = socket.gethostname().split(".", 1)[0].lower()
    user = pwd.getpwuid(os.geteuid()).pw_name
    if binding["host"] != host or binding["unix_user"] != user:
        raise PermissionError("Downstream service host/Unix binding mismatch")
    return binding["login"]


def attribution(body: str, actor: str) -> str:
    # The remote API account and local audit correlation are verifiable. Model
    # tiers and arbitrary caller-provided agent names are deliberately excluded.
    return body.strip() + "\n\nvia hbf-mcp; service=" + actor + "; correlation=" + correlation_id.get()


def gitea_client():
    from hbf_api_tools.gitea import Gitea
    return Gitea(identity_provider=lambda: service_actor("gitea"),
                 attribution_provider=attribution)


def youtrack_client():
    from hbf_api_tools.youtrack import YouTrack
    return YouTrack(identity_provider=lambda: service_actor("youtrack"),
                    attribution_provider=attribution)
