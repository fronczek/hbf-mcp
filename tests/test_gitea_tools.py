"""Offline regression tests for the HBF MCP Gitea module."""
from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1]/"gitea_tools.py"
spec = importlib.util.spec_from_file_location("gitea_tools_under_test", MODULE_PATH)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


class PolicyTests(unittest.TestCase):
    def test_reject_repo_outside_allowlist(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            g._repo("other/unknown")

    def test_reject_path_traversal(self):
        for name in ("../secret","/etc/passwd","abc//def","abc/./def"):
            with self.subTest(name=name),self.assertRaises(ValueError):
                g._file_path(name)

    def test_segment_encoding(self):
        self.assertEqual(g._segment("x/y"), "x%2Fy")
        self.assertEqual(g._file_path("docs/a b.md"), "docs/a%20b.md")

    def test_unknown_operation_key_rejected(self):
        with self.assertRaises(ValueError):
            g._operation_status("secnet/ese-agent","x")

    def test_disallowed_redirect(self):
        request=object()
        with self.assertRaisesRegex(RuntimeError,"redirect refused"):
            g.NoRedirect().redirect_request(request,None,302,"Found",{},"https://evil.example/")

    def test_configuration_is_fixed_to_gitea(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"gitea.yml"
            p.write_text("gitea_url: https://evil.example\ntoken_value: random-secret\n")
            with patch.object(g,"CREDENTIAL_FILE",p):
                with self.assertRaisesRegex(RuntimeError,"Untrusted"):
                    g._credentials()


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db=Path(self.temp.name)/"state/operations.sqlite3"
        self._patch=patch.object(g,"DB_FILE",db)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.repo="secnet/ese-agent"
        self.path="/repos/secnet/ese-agent/issues/12/comments"
        self.body={"body":"test"}
        self.key="fe8d8c16-6aa7-42f3-80e9-e10bdd72ae7a"

    def test_durable_idempotency_and_reuse(self):
        with patch.object(g,"_gpt_account"),patch.object(g,"_api",return_value={"id":123}) as api:
            one=g._mutation(self.repo,self.key,"POST",self.path,self.body)
            two=g._mutation(self.repo,self.key,"POST",self.path,self.body)
        self.assertEqual(one["id"],123)
        self.assertFalse(one["reused_result"])
        self.assertTrue(two["reused_result"])
        api.assert_called_once()
        self.assertEqual(g._operation_status(self.repo,self.key)["status"],"done")
        self.assertEqual(g.DB_FILE.stat().st_mode & 0o777,0o600)

    def test_cannot_change_mutation_under_same_key(self):
        with patch.object(g,"_gpt_account"),patch.object(g,"_api",return_value={"id":123}):
            g._mutation(self.repo,self.key,"POST",self.path,self.body)
            with self.assertRaisesRegex(ValueError,"different operation"):
                g._mutation(self.repo,self.key,"POST",self.path,{"body":"different"})

    def test_ambiguous_failure_not_retried(self):
        with patch.object(g,"_gpt_account"),patch.object(g,"_api",side_effect=RuntimeError("timeout")) as api:
            with self.assertRaisesRegex(RuntimeError,"timeout"):
                g._mutation(self.repo,self.key,"POST",self.path,self.body)
            with self.assertRaisesRegex(RuntimeError,"already attempted"):
                g._mutation(self.repo,self.key,"POST",self.path,self.body)
        api.assert_called_once()
        self.assertEqual(g._operation_status(self.repo,self.key)["status"],"uncertain")

    def test_http_4xx_is_rejected_and_never_retried(self):
        with patch.object(g,"_gpt_account"),patch.object(g,"_api",side_effect=g.UpstreamError(403)) as api:
            with self.assertRaises(g.UpstreamError):
                g._mutation(self.repo,self.key,"POST",self.path,self.body)
            self.assertEqual(g._operation_status(self.repo,self.key)["status"],"rejected")
            with self.assertRaisesRegex(RuntimeError,"already attempted"):
                g._mutation(self.repo,self.key,"POST",self.path,self.body)
        api.assert_called_once()

    def test_account_must_be_gpt_before_mutation(self):
        with patch.object(g,"_api",return_value={"login":"dsh"}) as api:
            with self.assertRaisesRegex(RuntimeError,"not GPT"):
                g._mutation(self.repo,self.key,"POST",self.path,self.body)
        api.assert_called_once_with("GET","/user")
        self.assertEqual(g._operation_status(self.repo,self.key)["status"],"not_found")


class MCPRegistrationTests(unittest.TestCase):
    def test_registers_named_tools(self):
        from mcp.server import MCPServer
        s=MCPServer("HBF Gitea Integration Test")
        g.register_tools(s)
        async def names():
            return [tool.name for tool in await s.list_tools()]
        tools=asyncio.run(names())
        self.assertEqual(len(tools),28)
        self.assertIn("gitea_submit_review",tools)
        self.assertIn("gitea_get_pull_diff",tools)
        self.assertNotIn("gitea_commit_files",tools)


if __name__=="__main__":
    unittest.main()
