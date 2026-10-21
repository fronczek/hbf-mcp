"""Compact, read-only health checks for GPT's own MCP01 control plane."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from mcp.types import ToolAnnotations

ROOT=Path('/opt/hbf-mcp')
HOME=Path('/home/gptagent')


def register_tools(mcp:Any)->None:
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)

    @mcp.tool(annotations=read)
    def mcp_health() -> dict:
        """Inspect local MCP01 service and optional helper readiness (without secrets)."""
        from importlib.util import find_spec
        try:
            proc=subprocess.run(['/usr/bin/systemctl','is-active','hbf-mcp.service'],
                capture_output=True,text=True,timeout=5)
            service=proc.stdout.strip() if proc.returncode==0 else 'not-active'
        except (OSError,subprocess.TimeoutExpired):
            service='unknown'
        modules={name:(ROOT/(name+'.py')).is_file() for name in
                 ('gitea_tools','youtrack_tools','git_workflow_tools',
                  'agent_tools','diagnostics_tools','zabbix_tools')}
        return {'host':'hbfplhmcpu01','service':service,
                'modules_on_disk':modules,
                'credentials_configured':{
                    'gitea':(HOME/'credentials/gitea.yml').is_file(),
                    'youtrack':(HOME/'credentials/youtrack.yml').is_file(),
                    'ese_browser':(HOME/'credentials/ese-browser.json').is_file(),
                    'zabbix':(HOME/'credentials/zabbix.yml').is_file()},
                'helpers_present':{
                    'dsh_api':(HOME/'bin/dsh-remote').is_file(),
                    'zabbix_watch':(HOME/'bin/zabbix-watch').is_file(),
                    'playwright':(HOME/'esekretariat/ese-agent/tools/browser/node_modules').is_dir()},
                'shell_enabled_in_service':True,
                'notes':'Disk presence does not prove registered tools are active; refresh tools after service restart.'}

    @mcp.tool(annotations=read)
    def mcp_api_health() -> dict:
        """Check GPT's fixed Gitea/YouTrack API identities without disclosing tokens."""
        checks={}
        try:
            import gitea_tools
            identity=gitea_tools._api('GET','/user')
            checks['gitea']={'reachable':True,'login':identity.get('login'),
                            'correct_account':identity.get('login')=='gpt'}
        except Exception:
            checks['gitea']={'reachable':False,'reason':'API unreachable or identity mismatch'}
        try:
            import youtrack_tools
            identity=youtrack_tools._call('GET','/api/users/me?fields=login')
            checks['youtrack']={'reachable':True,'login':identity.get('login'),
                               'correct_account':identity.get('login')=='gpt'}
        except Exception:
            checks['youtrack']={'reachable':False,'reason':'API unreachable or identity mismatch'}
        return checks
