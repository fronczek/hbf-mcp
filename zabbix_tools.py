"""Restricted read-only Zabbix health checks for the MCP service.

This module never handles credentials, URLs or arbitrary API requests. The
local helper owns credentials and returns bounded, sanitised JSON reports.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

HELPER = Path('/home/gptagent/bin/zabbix-watch')


def health_report(lookback_hours: int = 24) -> dict[str, Any]:
    """Read a fixed health summary from the locally configured Zabbix server."""
    if type(lookback_hours) is not int or not 1 <= lookback_hours <= 168:
        raise ValueError('lookback_hours must be an integer between 1 and 168')
    try:
        proc = subprocess.run(
            ['/usr/bin/python3', str(HELPER), '--hours', str(lookback_hours)],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Zabbix health helper unavailable') from None
    if proc.returncode != 0:
        raise RuntimeError('Zabbix health helper failed')
    if len(proc.stdout) > 128 * 1024:
        raise RuntimeError('Zabbix health response exceeded size limit')
    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, UnicodeError):
        raise RuntimeError('Zabbix health helper returned invalid JSON') from None
    if not isinstance(result, dict) or not isinstance(result.get('hosts'), dict):
        raise RuntimeError('Zabbix health helper returned an invalid report')
    return result


def register_tools(mcp: Any) -> None:
    readonly = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False,
        idempotentHint=True, openWorldHint=False,
    )

    @mcp.tool(annotations=readonly)
    def zabbix_health(lookback_hours: int = 24) -> dict:
        """Read current Zabbix High/Disaster incidents, availability issues and
        CPU/NVMe temperature trends on hbfplhpved01 and hbfplhpved02.

        Calls one fixed local helper; uses local Zabbix credentials privately;
        does not expose arbitrary queries or modify monitoring configuration.
        lookback_hours: historical period (1-168 hours, default 24).
        """
        return health_report(lookback_hours)
