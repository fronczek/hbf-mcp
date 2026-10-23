"""Bounded local launcher for the established DSH API helper and ESE browser gates.

No arbitrary executable, cwd, remote shell, SSH target, or credential arguments.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal
from mcp.types import ToolAnnotations

HOME=Path(os.getenv('HBF_MCP_AGENT_HOME', '/home/gptagent'))
HELPER=HOME/'bin/dsh-remote'
BROWSER=HOME/'projects/esekretariat/ese-agent/tools/browser'
CONFIG=HOME/'credentials/ese-browser.json'
ESE=re.compile(r'^ESE-[1-9][0-9]{0,6}$')


def _error_category(stderr: str) -> str:
    """Classify helper failure without returning potentially sensitive stderr."""
    lower = stderr.lower()
    if any(term in lower for term in ("budget guard", "balance check failed", "insufficient balance")):
        return "budget_guard"
    if any(term in lower for term in ("401", "403", "unauthorized", "forbidden")):
        return "authentication"
    if any(term in lower for term in ("timeout", "timed out")):
        return "timeout"
    if any(term in lower for term in ("connection refused", "unreachable", "dns", "name resolution")):
        return "connectivity"
    if "404" in lower:
        return "api_not_found"
    return "unknown"


def _run(args:list[str], *, timeout:int=35, stdin:str|None=None, cwd:Path|None=None,
         env:dict|None=None) -> str:
    if not Path(args[0]).is_file():
        raise RuntimeError('Pinned helper is unavailable on MCP01')
    try:
        result=subprocess.run(args,input=stdin,text=True,capture_output=True,
                              timeout=timeout,cwd=cwd,env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Local helper timed out; inspect existing session before retry') from None
    if result.returncode:
        raise RuntimeError(f'Local helper returned status {result.returncode}; inspect existing state (stderr category: {_error_category(result.stderr)})')
    if len(result.stdout)>160_000:
        raise RuntimeError('Helper output exceeds bound; inspect report locally')
    return result.stdout


def register_tools(mcp:Any)->None:
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=False,openWorldHint=True)

    @mcp.tool(annotations=read)
    def deepseek_sessions(limit:int=10)->dict:
        """List running DSH sessions using preconfigured HTTPS Harness API helper."""
        if not 1<=limit<=25: raise ValueError('Limit must be 1..25')
        raw=_run([str(HELPER),'sessions','--brief-limit',str(limit)])
        obj=json.loads(raw)
        items=obj.get('items') if isinstance(obj,dict) else None
        if not isinstance(items,list) or len(items)>limit:
            raise RuntimeError('Unexpected bounded session list')
        return {'sessions':[{'session':x.get('sessionId'),'running':x.get('running'),
                'cwd':x.get('cwd'),'updated':x.get('updatedAt')}
                for x in items], 'count_shown':len(items),
                'has_more':bool(obj.get('has_more'))}

    @mcp.tool(annotations=write)
    def deepseek_flash_task(issue_id:str, task:str, max_usd:float=1.00,
                            timeout_seconds:int=180)->dict:
        """Delegate one ESE task via existing DSH Flash HTTPS API, with budget cap.

        Requires task authorization. No SSH, no DSH filesystem access from MCP01.
        No concurrent sessions. Never rerun blindly after uncertain timeout.
        """
        if not ESE.fullmatch(issue_id): raise ValueError('Expected ESE issue ID')
        if not 15<=len(task)<=8000: raise ValueError('Task length must be 15..8000')
        if not 0.001<=max_usd<=1.00: raise ValueError('Task budget must be USD 0.001..1.00')
        if not 30<=timeout_seconds<=600: raise ValueError('Timeout must be 30..600 s')
        prompt=(f'Work on {issue_id} only. Before editing, pull latest from configured remote. '
                'Follow AGENTS.md and existing repository/project rules. '
                'Do not use GPT credentials, SSH into MCP01 or other hosts, or access another agent identity. '
                'Report changed files, tests, commits, and blockers succinctly.\n\n'+task)
        try:
            raw=_run([str(HELPER),'flash','-','--cwd','/home/afronczek/esekretariat',
                      '--timeout',str(timeout_seconds),'--max-usd',str(max_usd),
                      '--effort','low'],stdin=prompt,timeout=timeout_seconds+65)
        except RuntimeError as exc:
            message=str(exc)
            lower=message.lower()
            definitely_not_started=any(term in lower for term in (
                'pinned helper is unavailable',
                'stderr category: budget_guard',
                'stderr category: authentication',
                'stderr category: connectivity',
                'stderr category: api_not_found',
            ))
            return {'issue':issue_id,
                    'status':'not_started' if definitely_not_started else 'unknown',
                    'error':message}
        session=re.search(r'^session:\s*(\S+)',raw,re.M)
        tokens=re.search(r'^tokens:\s*(.+)$',raw,re.M)
        usd=re.search(r'^estimated USD cost:\s*(.+)$',raw,re.M)
        marker='---- assistant result ----'
        answer=raw.split(marker,1)[-1].strip() if marker in raw else ''
        return {'issue':issue_id,'status':'completed',
                'session':session.group(1) if session else None,
                'tokens':tokens.group(1) if tokens else None,
                'cost_estimate':usd.group(1) if usd else None,
                'result_excerpt':answer[-3000:],
                'result_truncated':len(answer)>3000}

    browser_write=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=False,openWorldHint=False)

    @mcp.tool(annotations=browser_write)
    def ese_browser_gate(gate:Literal['smoke','admin-smoke'],
                         timeout_seconds:int=180)->dict:
        """Run the fixed ESE test menu gate; writes local reports, returns brief results.

        Browser target is restricted by ese-agent gate scripts to authorised TEST .org hosts.
        The gate can write local reports, and must be run only for authorised test systems.
        """
        if not 45<=timeout_seconds<=420: raise ValueError('Timeout must be 45..420')
        if not (BROWSER/'node_modules').is_dir():
            raise RuntimeError('Playwright npm dependencies missing; install from lock first')
        if not CONFIG.is_file() or CONFIG.stat().st_mode & 0o077:
            raise RuntimeError('Private 0600 ESE browser test config is unavailable')
        cmd=['/usr/bin/node',str(BROWSER/(gate+'.mjs'))]
        env=os.environ.copy()
        env['ESE_BROWSER_CONFIG']=str(CONFIG)
        env.pop('NODE_OPTIONS',None)
        try:
            result=subprocess.run(cmd,cwd=BROWSER,env=env,capture_output=True,
                                   text=True,timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return {'gate':gate,'status':'TIMEOUT','elapsed_limit':timeout_seconds}
        # Browser output is untrusted tenant data: even diagnostic lines may
        # include passwords, names, scanner URLs or private attachment content.
        # Summarise by fixed labels only. Full reports stay private on MCP01.
        output=(result.stdout+'\n'+result.stderr).splitlines()
        kinds={'PASS':0,'FAIL':0,'ERROR':0,'WARNING':0}
        for line in output:
            for match in set(re.findall(r'\b(PASS|FAIL|ERROR|WARNING)\b',line,re.I)):
                kinds[match.upper()]+=1
        return {'gate':gate,'status':'PASS' if result.returncode==0 else 'FAIL',
                'exit_code':result.returncode,'finding_class_counts':kinds,
                'private_output_lines':len(output)}
