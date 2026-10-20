"""Behavior and safety tests for high-level MCP tools; no network writes."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from mcp.server import MCPServer
import git_workflow_tools as git
import agent_tools as agent
import diagnostics_tools as diagnostics
import youtrack_tools as yt


def find(name,module):
    server=MCPServer('test')
    module.register_tools(server)
    return server._tool_manager._tools[name].fn


class GitWorkflowTests(unittest.TestCase):
    def test_disallow_arbitrary_repo(self):
        with self.assertRaises(ValueError):git._path('/etc')
    def test_branch_validation(self):
        for b in ('../master','master.lock','a..b','-push','a\\nb'):
            with self.subTest(b=b),self.assertRaises(ValueError):git._branch_name(b)
    def test_remote_host_restriction(self):
        with patch.object(git,'_git',return_value='git@evil.example:owner/repo'):
            with self.assertRaisesRegex(RuntimeError,'Untrusted'):git._remote('hbf-mcp')
    def test_stage_rejects_secret_before_running_git(self):
        fn=find('git_workflow_stage',git)
        with patch.object(git,'_git') as call:
            for candidate in ('../keys','credentials/token.yml','.env.production','a/.ssh/id_rsa','a//b'):
                with self.subTest(path=candidate),self.assertRaises(ValueError):
                    fn('hbf-mcp',[candidate])
            call.assert_not_called()
    def test_master_commit_refused(self):
        fn=find('git_workflow_signed_commit',git)
        with patch.object(git,'_identity'), patch.object(git,'_git',return_value='master'):
            with self.assertRaisesRegex(RuntimeError,'protected branch'):
                fn('hbf-mcp','A valid signed commit message','a'*40)
    def test_master_push_refused(self):
        fn=find('git_workflow_push',git)
        with patch.object(git,'_identity'),patch.object(git,'_clean'),patch.object(git,'_remote'):
            with patch.object(git,'_git',return_value='master'):
                with self.assertRaisesRegex(RuntimeError,'protected branch'):
                    fn('hbf-mcp','a'*40)


class DeepSeekToolsTests(unittest.TestCase):
    def test_invalid_task_cannot_launch(self):
        fn=find('deepseek_flash_task',agent)
        with patch.object(agent,'_run') as runner:
            with self.assertRaises(ValueError):fn('OTHER-123','Do a thing on ESE-123')
            with self.assertRaises(ValueError):fn('ESE-123','Do a thing on ESE-123',max_usd=4)
            runner.assert_not_called()
    def test_flash_uses_pinned_helper_api_and_budget(self):
        fn=find('deepseek_flash_task',agent)
        with patch.object(agent,'_run',return_value='session: 99\ntokens: uncached=1\nestimated USD cost: 0.001\n---- assistant result ----\nOK') as runner:
            result=fn('ESE-801','Analyze OCR wiring and report missing tests',max_usd=0.03)
            self.assertEqual(result['session'],'99')
            args=runner.call_args.args[0]
            self.assertEqual(args[:3],[str(agent.HELPER),'flash','-'])
            self.assertEqual(args[args.index('--max-usd')+1],'0.03')
            self.assertNotIn('ssh',args)
    def test_browser_rejects_missing_dependencies(self):
        fn=find('ese_browser_gate',agent)
        with patch.object(agent,'BROWSER',Path('/tmp/ese-test-absent-192839')):
            with self.assertRaisesRegex(RuntimeError,'dependencies missing'):
                fn('smoke')


class OtherToolsTests(unittest.TestCase):
    def test_youtrack_search_forces_ese_project(self):
        fn=find('youtrack_search_issues',yt)
        with patch.object(yt,'_call',return_value=[{'idReadable':'ESE-801','summary':'a'}]) as api:
            out=fn('Azure OCR',3)
        self.assertEqual(out['issues'][0]['id'],'ESE-801')
        self.assertIn('project%3A+ESE',api.call_args.args[1])
    def test_youtrack_state_rejects_arbitrary_commands(self):
        fn=find('youtrack_apply_state',yt)
        with patch.object(yt,'_call') as api:
            with self.assertRaises(ValueError):fn('ESE-801','delete all issues')
            api.assert_not_called()
    def test_health_no_credentials_returned(self):
        result=find('mcp_health',diagnostics)()
        self.assertIn('service',result)
        self.assertNotIn('token',repr(result).lower())

if __name__=='__main__':unittest.main()
