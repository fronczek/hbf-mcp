"""Behavior and safety tests for high-level MCP tools; no network writes."""
from __future__ import annotations

import asyncio
import json
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
    def test_valid_origin_requires_exact_repo_and_fetch_and_push(self):
        cases = [
            ('hbf-mcp','git@github.com:fronczek/hbf-mcp.git'),
            ('ese-agent','git@git.hbf.pl:secnet/ese-agent.git'),
        ]
        for repo,url in cases:
            with self.subTest(repo=repo),patch.object(git,'_git',return_value=url) as command:
                self.assertEqual(git._remote(repo),url)
                self.assertEqual(command.call_count,2)
                self.assertIn('--push',command.call_args_list[1].args)

    def test_valid_fetch_malicious_pushurl_rejected(self):
        good='git@github.com:fronczek/hbf-mcp.git'
        for bad in ('git@evil.example:fronczek/hbf-mcp.git',
                    'git@github.com:fronczek/other-repository.git',
                    'git@github.com:fronczek/hbf-mcp.git\ngit@evil.example:fronczek/hbf-mcp.git',
                    'https://github.com/evil/hbf-mcp.git'):
            with self.subTest(pushurl=bad),patch.object(git,'_git',side_effect=[good,bad]):
                with self.assertRaisesRegex(RuntimeError,'Untrusted'):
                    git._remote('hbf-mcp')

    def test_foreign_fetch_url_rejected_even_with_good_pushurl(self):
        with patch.object(git,'_git',side_effect=[
                'git@git.hbf.pl:secnet/ese-agent-copy.git',
                'git@git.hbf.pl:secnet/ese-agent.git']):
            with self.assertRaisesRegex(RuntimeError,'Untrusted'):
                git._remote('ese-agent')

    def test_git_remote_blocks_invalid_push_before_git_push(self):
        fn=find('git_workflow_push',git)
        with patch.object(git,'_identity'),patch.object(git,'_clean'):
            with patch.object(git,'_git',side_effect=[
                    'git@github.com:fronczek/hbf-mcp.git',
                    'git@evil.example:fronczek/hbf-mcp.git']) as command:
                with self.assertRaisesRegex(RuntimeError,'Untrusted'):
                    fn('hbf-mcp','a'*40)
                self.assertEqual(command.call_count,2)

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
    def test_browser_result_never_returns_raw_private_logs(self):
        fn=find('ese_browser_gate',agent)
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as d:
            browser=Path(d)
            (browser/'node_modules').mkdir()
            config=browser/'private.json'
            config.write_text('{}')
            config.chmod(0o600)
            with patch.object(agent,'BROWSER',browser),patch.object(agent,'CONFIG',config):
                with patch.object(agent.subprocess,'run',return_value=CompletedProcess(
                      args=[],returncode=1,stdout='ERROR token=very-private\nFAIL tenant@example.test',
                      stderr='WARNING password=classified\n')):
                    result=fn('smoke')
            self.assertEqual(result['status'],'FAIL')
            self.assertEqual(result['finding_class_counts']['ERROR'],1)
            self.assertNotIn('very-private',str(result))
            self.assertNotIn('classified',str(result))
            self.assertNotIn('tenant@example.test',str(result))

    def test_sessions_use_compact_helper(self):
        fn=find('deepseek_sessions',agent)
        data={'items':[{'sessionId':'active-1','running':True,'cwd':'/work','updatedAt':'now'}],
              'total':236,'has_more':True}
        with patch.object(agent,'_run',return_value=json.dumps(data)) as call:
            out=fn(10)
        self.assertEqual(call.call_args.args[0],
                         [str(agent.HELPER),'sessions','--brief-limit','10'])
        self.assertEqual(out['count_shown'],1)
        self.assertTrue(out['has_more'])
        self.assertEqual(out['sessions'][0]['session'],'active-1')

    def test_sessions_reject_oversized_response(self):
        fn=find('deepseek_sessions',agent)
        with patch.object(agent,'_run',return_value=json.dumps({'items':[{},{}]})):
            with self.assertRaisesRegex(RuntimeError,'bounded'):fn(1)

    def test_flash_default_is_one_dollar_per_task(self):
        fn=find('deepseek_flash_task',agent)
        with patch.object(agent,'_run',return_value='session: 123\n---- assistant result ----\nOK') as runner:
            out=fn('ESE-801','Review the synthetic queue safely')
        self.assertEqual(out['session'],'123')
        args=runner.call_args.args[0]
        self.assertEqual(args[args.index('--max-usd')+1],'1.0')

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
