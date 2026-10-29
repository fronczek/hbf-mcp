"""Compact, read-only health checks for GPT's own MCP01 control plane."""
from __future__ import annotations

import os
import hashlib
import socket
import subprocess
from pathlib import Path
from typing import Any
from mcp.types import ToolAnnotations

ROOT=Path(__file__).resolve().parent
HOME=Path(os.getenv('HBF_MCP_AGENT_HOME', '/home/gptagent'))
SERVICE_NAME=os.getenv('HBF_MCP_SERVICE_NAME', 'hbf-mcp')

SOURCE_ROOT = Path(__file__).resolve().parent
SOURCE_FILES = ('server.py', 'gitea_tools.py', 'youtrack_tools.py',
                'git_workflow_tools.py', 'agent_tools.py', 'diagnostics_tools.py', 'zabbix_tools.py', 'emby_tools.py',
                'auth0_resource.py', 'pyproject.toml', 'requirements.txt') + tuple(
                    str(path.relative_to(SOURCE_ROOT))
                    for directory in ('auth', 'services', 'tools', 'src/hbf_api_tools')
                    for path in sorted((SOURCE_ROOT / directory).rglob('*.py'))
                ) + ('src/hbf_api_tools/monitoring.yml',)


def source_fingerprint(root: Path) -> str:
    """Hash only public runtime source; never configuration or credentials."""
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


# Snapshot at import, not at each health call: detect a deployment without restart.
LOADED_SOURCE_FINGERPRINT = source_fingerprint(SOURCE_ROOT)



def register_tools(mcp:Any)->None:
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)

    @mcp.tool(annotations=read)
    def mcp_health() -> dict[str, Any]:
        """Inspect local MCP01 service and optional helper readiness (without secrets)."""
        from importlib.util import find_spec
        try:
            proc=subprocess.run(['/usr/bin/systemctl','is-active',SERVICE_NAME+'.service'],
                capture_output=True,text=True,timeout=5)
            service=proc.stdout.strip() if proc.returncode==0 else 'not-active'
        except (OSError,subprocess.TimeoutExpired):
            service='unknown'
        modules={name:(ROOT/(name+'.py')).is_file() for name in
                 ('gitea_tools','youtrack_tools','git_workflow_tools',
                  'agent_tools','diagnostics_tools','zabbix_tools','emby_tools')}
        try:
            disk_fingerprint = source_fingerprint(SOURCE_ROOT)
            source_readable = True
        except OSError:
            disk_fingerprint = None
            source_readable = False
        return {'host':socket.gethostname(),'service':service,'service_name':SERVICE_NAME,
                'loaded_source_sha256':LOADED_SOURCE_FINGERPRINT,
                'disk_source_sha256':disk_fingerprint,
                'restart_required':disk_fingerprint != LOADED_SOURCE_FINGERPRINT,
                'runtime_sources_readable':source_readable,
                'modules_on_disk':modules,
                'credentials_configured':{
                    'gitea':(HOME/'.credentials/gitea.yml').is_file(),
                    'youtrack':(HOME/'.credentials/youtrack.yml').is_file(),
                    'ese_browser':(HOME/'.credentials/ese-browser.json').is_file(),
                    'zabbix':(HOME/'.credentials/zabbix.yml').is_file(),
                    'influxdb':(HOME/'.credentials/influxdb.yml').is_file(),
                    'emby':bool(list((HOME/'.credentials').glob('emby*.yml')))},
                'helpers_present':{
                    'dsh_api':(HOME/'bin/dsh-remote').is_file(),
                    'zabbix_watch':(HOME/'bin/zabbix-watch').is_file(),
                    'boiler_watch':Path(os.getenv('HBF_MCP_BOILER_HELPER', str(HOME/'bin/kuk-boiler-watch'))).is_file(),
                    'playwright':(HOME/'projects/esekretariat/ese-agent/tools/browser/node_modules').is_dir()},
                'shell_enabled_in_service':True,
                'notes':'Disk presence does not prove registered tools are active; refresh tools after service restart.'}

    @mcp.tool(annotations=read)
    def mcp_api_health() -> dict[str, Any]:
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
