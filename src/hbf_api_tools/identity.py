"""Agent-identity boundary for API writes. No cross-agent identity delegation.

This enforces the local hostname+Unix login+private agent credential binding.
Tokens also undergo a service-side /me check before every write. Local checks
are not a substitute for distinct Unix accounts/keyring ACLs on the DSH host.
"""
from __future__ import annotations

import os
import pwd
import socket

from .config import ConfigurationError, load_scalar_yaml

# Executing on an unrecognised host or via a different Unix user FAILS CLOSED.
# Adding more execution hosts requires a reviewed code change.
HOST_ACTORS = {
    "hbfplhmcpu01": ("gpt", "gptagent"),
    # The 2026-10-24 DSH migration moved the workbench from the owner's
    # `afronczek` account to the dedicated `dsh` account; `agent.yml` was
    # provisioned with that binding. The old `afronczek` value is deliberately
    # not accepted any more: one host, one agent Unix account.
    "hbfplhdshu01": ("dsh", "dsh"),
}


def acting_agent() -> str:
    host = socket.gethostname().split(".", 1)[0].lower()
    username = pwd.getpwuid(os.geteuid()).pw_name
    expected = HOST_ACTORS.get(host)
    if expected is None or expected[1] != username:
        raise ConfigurationError("Agent API mutation refused on this host/Unix identity")
    data = load_scalar_yaml("agent.yml")
    if (data.get("agent_login") != expected[0]
            or data.get("agent_host") != host
            or data.get("agent_unix_user") != username):
        raise ConfigurationError("Local agent identity does not match trusted host binding")
    return expected[0]


def add_attribution(body: str, agent: str) -> str:
    """Attach an authenticated account marker; demand explicit DSH model tier.

    An API token proves dsh, not which model ran. The DSH session runner must
    supply the correct tier; human-readable tier remains self-reported.
    """
    body = body.strip()
    if agent == "dsh":
        if not body.endswith(("added by dsh-pro", "added by dsh-flash")):
            raise ConfigurationError("DSH write requires an explicit dsh-pro or dsh-flash attribution")
        return body
    if agent != "gpt":
        raise ConfigurationError("Unknown agent attribution")
    signature = "added by gpt-lindir"
    if body.endswith(signature):
        return body
    return body + "\n\n" + signature
