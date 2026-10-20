"""Narrow, signed local Git workflows on MCP01. No remote shell access."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from mcp.types import ToolAnnotations

HOME = Path('/home/gptagent')
REPOSITORIES = {
    'hbf-mcp': HOME/'projects/hbf-mcp-upstream-review',
    'ese-agent': HOME/'esekretariat/ese-agent',
    'hbf-gpt-gitea': HOME/'projects/hbf-gpt-gitea',
    'hbf-gpt-mailer': HOME/'repos/hbf-gpt-mailer',
    'hbf-azure-ocr': HOME/'repos/hbf-azure-ocr',
}
SIGNER = 'F4B21C66B3C84DBE114CD6FE764C5FCF43B57DBA'
EMAIL = 'artur.fronczek+gitea_gpt@gmail.com'
BRANCH = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_./-]{0,110}$')
SECRET = re.compile(r'(^|/)(\.env(?:\.|$)|credentials?(?:/|$)|secrets?(?:/|$)|\.ssh/|\.gnupg/)|(?:\.pem|\.key|\.p12|\.pfx)$',re.I)


def _path(repo: str) -> Path:
    if repo not in REPOSITORIES:
        raise ValueError('Repository is outside configured MCP01 allowlist')
    p = REPOSITORIES[repo]
    if not (p/'.git').exists():
        raise RuntimeError('Local repository is unavailable')
    return p


def _git(repo: str, *args: str, timeout: int = 35) -> str:
    p = _path(repo)
    cmd = ['git','-c','core.hooksPath=/dev/null','-C',str(p),*args]
    env = os.environ.copy()
    env.update({'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_NOSYSTEM':'1'})
    try:
        r = subprocess.run(cmd,capture_output=True,text=True,timeout=timeout,env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Git timed out; inspect repository state before retry') from None
    if r.returncode:
        # Do not surface arbitrary hook output, remote secrets or potentially huge diffs.
        raise RuntimeError(f'Git {args[0]} failed (exit {r.returncode}); inspect local status')
    return r.stdout.strip()


def _remote(repo: str) -> str:
    url = _git(repo,'remote','get-url','origin')
    if not (url.startswith('git@github.com:') or url.startswith('git@git.hbf.pl:')
            or url.startswith('https://github.com/') or url.startswith('https://git.hbf.pl/')):
        raise RuntimeError('Untrusted Git remote; refusing network operation')
    if any(x in url for x in ('?', '#','@github.com/')) or url.count('@') > 1:
        raise RuntimeError('Unexpected Git remote URL')
    return url


def _branch_name(name: str) -> str:
    if (not BRANCH.fullmatch(name) or name.endswith('/') or '..' in name or
            '@{' in name or name.endswith('.lock') or '// ' in name):
        raise ValueError('Invalid branch name')
    return name


def _identity(repo: str) -> None:
    if _git(repo,'config','user.email') != EMAIL:
        raise RuntimeError('Git user email differs from GPT')
    if _git(repo,'config','user.signingkey').upper() != SIGNER:
        raise RuntimeError('GPT signing key not configured')


def _clean(repo: str) -> None:
    if _git(repo,'status','--porcelain','--untracked-files=normal'):
        raise RuntimeError('Repository has uncommitted/untracked files')


def register_tools(mcp: Any) -> None:
    read = ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=False,openWorldHint=True)

    @mcp.tool(annotations=read)
    def git_workflow_status(repo: str) -> dict:
        """Compact local branch, dirty state, remote host and signed HEAD status."""
        branch=_git(repo,'branch','--show-current')
        status=_git(repo,'status','--porcelain','--untracked-files=normal')
        log=_git(repo,'log','-1','--format=%h|%G?|%s').split('|',2)
        origin=_remote(repo)
        return {'repo':repo,'branch':branch,'changed_files':len(status.splitlines()) if status else 0,
                'head':log[0],'signature':log[1], 'subject':log[2][:140],
                'remote_host':'Gitea' if 'git.hbf.pl' in origin else 'GitHub'}

    @mcp.tool(annotations=write)
    def git_workflow_sync(repo: str) -> dict:
        """Fetch and fast-forward current upstream branch only. Refuses dirty worktree."""
        _clean(repo); _remote(repo)
        branch=_branch_name(_git(repo,'branch','--show-current'))
        upstream=_git(repo,'rev-parse','--abbrev-ref','--symbolic-full-name','@{upstream}')
        if not upstream.startswith('origin/'):
            raise RuntimeError('Tracked upstream must be origin')
        before=_git(repo,'rev-parse','HEAD')
        _git(repo,'fetch','--no-tags','origin',timeout=90)
        if not _git(repo,'merge-base','--is-ancestor',before,upstream) == '':
            raise RuntimeError('Diverged upstream; fast-forward refused')
        _git(repo,'merge','--ff-only',upstream)
        return {'repo':repo,'branch':branch,'before':before[:12],
                'after':_git(repo,'rev-parse','HEAD')[:12]}

    @mcp.tool(annotations=write)
    def git_workflow_branch(repo: str, name: str, expected_head: str) -> dict:
        """Create a topic branch at verified current HEAD in a clean local checkout."""
        _clean(repo); _branch_name(name)
        if not name.startswith(('feat/','fix/','docs/','test/','chore/')):
            raise ValueError('Use a scoped topic branch (feat/, fix/, docs/, test/, chore/)')
        current=_git(repo,'rev-parse','HEAD')
        if current != expected_head:
            raise RuntimeError('Checkout HEAD changed')
        _git(repo,'switch','-c',name)
        return {'repo':repo,'branch':name,'at':current}

    @mcp.tool(annotations=write)
    def git_workflow_stage(repo: str, paths: list[str]) -> dict:
        """Stage only specified safe relative paths. Never stage credential-looking files."""
        if not 1 <= len(paths) <= 30:
            raise ValueError('Select 1-30 paths')
        for path in paths:
            parts=path.split('/')
            if (not path or path.startswith(('/', '-')) or any(x in ('','.','..') for x in parts)
                    or SECRET.search(path) or '\\' in path):
                raise ValueError('Unsafe Git path')
        _git(repo,'add','--',*paths)
        staged=_git(repo,'diff','--cached','--name-only','-z').split('\0')
        for name in staged:
            if name and SECRET.search(name):
                raise RuntimeError('Sensitive staged filename detected: unstage before commit')
        return {'repo':repo,'staged_count':len([x for x in staged if x]),
                'staged_names':[x for x in staged if x][:30]}

    @mcp.tool(annotations=write)
    def git_workflow_signed_commit(repo: str, message: str, expected_head: str) -> dict:
        """Sign staged content with GPT GPG key and verify signature; refuses stale HEAD."""
        if not 8 <= len(message) <= 160 or '\n' in message:
            raise ValueError('Commit message must be a short single line')
        _identity(repo)
        branch=_branch_name(_git(repo,'branch','--show-current'))
        if branch in ('master','main'):
            raise RuntimeError('Commit on protected branch refused; create topic branch')
        if _git(repo,'rev-parse','HEAD') != expected_head:
            raise RuntimeError('HEAD changed')
        names=_git(repo,'diff','--cached','--name-only','-z').split('\0')
        if not names or names==['']:
            raise RuntimeError('No staged changes')
        if any(SECRET.search(name) for name in names):
            raise RuntimeError('Sensitive filename in staged changes')
        _git(repo,'diff','--cached','--check')
        _git(repo,'-c','user.name=gpt','-c',f'user.email={EMAIL}', '-c',f'user.signingkey={SIGNER}',
             '-c','commit.gpgsign=true','commit','-S','-m',message,timeout=60)
        signature=_git(repo,'log','-1','--format=%G?|%GF')
        if signature != f'G|{SIGNER}':
            raise RuntimeError('Created commit signature failed verification')
        return {'repo':repo,'branch':branch,'sha':_git(repo,'rev-parse','HEAD'),'signature':'verified',
                'files':len([x for x in names if x])}

    @mcp.tool(annotations=write)
    def git_workflow_push(repo: str, expected_head: str) -> dict:
        """Push a signed topic branch to the allowlisted origin; never force-push/master."""
        _identity(repo); _clean(repo); _remote(repo)
        branch=_branch_name(_git(repo,'branch','--show-current'))
        if branch in ('master','main'):
            raise RuntimeError('Direct push to protected branch refused')
        if _git(repo,'rev-parse','HEAD') != expected_head:
            raise RuntimeError('HEAD changed since review')
        if _git(repo,'log','-1','--format=%G?|%GF') != f'G|{SIGNER}':
            raise RuntimeError('HEAD is not a verified GPT-signed commit')
        _git(repo,'push','-u','origin',f'HEAD:refs/heads/{branch}',timeout=90)
        return {'repo':repo,'branch':branch,'sha':expected_head,'pushed':True}
