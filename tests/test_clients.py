from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from hbf_api_tools import config
from hbf_api_tools.identity import acting_agent
from hbf_api_tools.gitea import Gitea
from hbf_api_tools.youtrack import YouTrack
from hbf_api_tools.dsh import flash_task
from hbf_api_tools.cli import main
from hbf_api_tools import youtrack as youtrack_module


class ClientsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cred = Path(self.temp.name) / ".credentials"
        self.cred.mkdir(mode=0o700)
        for name, contents in (
            ("agent.yml", "agent_login: gpt\nagent_host: hbfplhmcpu01\nagent_unix_user: gptagent\n"),
            ("youtrack.yml", "youtrack_url: https://youtrack.hbf.pl\nyoutrack_token_value: secret\n"),
            ("gitea.yml", "gitea_url: https://git.hbf.pl\ntoken_value: secret\n"),
        ):
            path = self.cred / name
            path.write_text(contents)
            path.chmod(0o600)
        self.env = patch.dict(os.environ, {"HBF_CREDENTIALS_DIR": str(self.cred)}, clear=False)
        self.env.start()
        self.host = patch("hbf_api_tools.identity.socket.gethostname", return_value="hbfplhmcpu01")
        self.user = patch("hbf_api_tools.identity.pwd.getpwuid", return_value=SimpleNamespace(pw_name="gptagent"))
        self.host.start()
        self.user.start()
        os.environ.pop("HBF_GITEA_REPOSITORIES", None)

    def tearDown(self):
        self.user.stop()
        self.host.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_credentials_reject_group_or_world_access(self):
        path = self.cred / "youtrack.yml"
        path.chmod(0o640)
        with self.assertRaisesRegex(config.ConfigurationError, "private"):
            config.load_scalar_yaml("youtrack.yml")

    def test_credentials_directory_must_be_private(self):
        self.cred.chmod(0o755)
        with self.assertRaisesRegex(config.ConfigurationError, "private"):
            config.load_scalar_yaml("youtrack.yml")

    def test_credentials_symlink_is_rejected(self):
        target = self.cred / "real.yml"
        target.write_text("key: value\n")
        target.chmod(0o600)
        (self.cred / "alias.yml").symlink_to(target)
        with self.assertRaisesRegex(config.ConfigurationError, "private"):
            config.load_scalar_yaml("alias.yml")

    def test_credentials_duplicate_key_is_rejected(self):
        path = self.cred / "youtrack.yml"
        path.write_text("token: first\ntoken: second\n")
        with self.assertRaisesRegex(config.ConfigurationError, "repeated key"):
            config.load_scalar_yaml("youtrack.yml")

    def test_youtrack_restricts_origin(self):
        (self.cred / "youtrack.yml").write_text(
            "youtrack_url: https://attacker.example\nyoutrack_token_value: secret\n")
        with self.assertRaisesRegex(config.ConfigurationError, "trusted HTTPS"):
            YouTrack()

    def test_dsh_write_requires_tier_attribution(self):
        from hbf_api_tools.identity import add_attribution
        with self.assertRaisesRegex(config.ConfigurationError, "explicit dsh-pro or dsh-flash"):
            add_attribution("work completed", "dsh")
        self.assertEqual(add_attribution("work completed\n\nadded by dsh-flash", "dsh"),
                         "work completed\n\nadded by dsh-flash")

    def test_gitea_read_refuses_owner_token(self):
        from hbf_api_tools import gitea as mod
        api = Gitea()
        with patch.object(mod, "request_json", return_value={"login": "artur"}) as call:
            with self.assertRaisesRegex(config.ConfigurationError, "does not match local agent"):
                api.branches("secnet/ese-agent")
        self.assertEqual(call.call_count, 1)

    def test_youtrack_read_refuses_owner_token(self):
        api = YouTrack()
        with patch.object(youtrack_module, "request_json", return_value={"login": "artur"}) as call:
            with self.assertRaisesRegex(config.ConfigurationError, "token does not match"):
                api.search("project: ESE")
        self.assertEqual(call.call_count, 1)

    def test_profile_from_separate_agent_refused(self):
        (self.cred / "agent.yml").write_text(
            "agent_login: dsh\nagent_host: hbfplhdshu01\nagent_unix_user: afronczek\n")
        with self.assertRaisesRegex(config.ConfigurationError, "does not match trusted host"):
            acting_agent()

    def test_other_host_is_refused_even_with_gpt_credentials(self):
        with patch("hbf_api_tools.identity.socket.gethostname", return_value="random-host"):
            with self.assertRaisesRegex(config.ConfigurationError, "refused on this host"):
                acting_agent()

    def test_other_unix_user_is_refused_even_with_gpt_credentials(self):
        with patch("hbf_api_tools.identity.pwd.getpwuid", return_value=SimpleNamespace(pw_name="afronczek")):
            with self.assertRaisesRegex(config.ConfigurationError, "refused on this host"):
                acting_agent()

    def test_dsh_host_and_actor_are_allowed_when_bound(self):
        (self.cred / "agent.yml").write_text(
            "agent_login: dsh\nagent_host: hbfplhdshu01\nagent_unix_user: afronczek\n")
        with patch("hbf_api_tools.identity.socket.gethostname", return_value="hbfplhdshu01"), \
             patch("hbf_api_tools.identity.pwd.getpwuid", return_value=SimpleNamespace(pw_name="afronczek")):
            self.assertEqual(acting_agent(), "dsh")

    def test_gitea_refuses_wrong_token_before_pr_create(self):
        api = Gitea()
        with patch.object(api, "_call", return_value={"login": "dsh"}) as calls:
            with self.assertRaisesRegex(config.ConfigurationError, "token does not match"):
                api.create_pull_request("hbf/hbf-api-tools", "title", "head", "master", "body")
        self.assertEqual(calls.call_count, 1)

    def test_manually_merged_pr_checks_head_master_parents_and_ci(self):
        api = Gitea()
        head, merge = "a" * 40, "b" * 40
        pr = {"state": "open", "merged": False,
              "head": {"sha": head}, "base": {"ref": "master"}}
        closed = {"state": "closed", "merged": True}
        checks = [
            {"login": "gpt"}, pr, {"commit": {"id": merge}},
            {"parents": [{"sha": "c" * 40}, {"sha": head}]},
            {"state": "success", "sha": head, "statuses": []},
            {}, closed,
        ]
        with patch.object(api, "_call", side_effect=checks) as mocked:
            result = api.mark_manually_merged("hbf/hbf-api-tools", 3,
                                             expected_head_sha=head, merge_commit_sha=merge)
        self.assertTrue(result["merged"])
        post = mocked.call_args_list[5]
        self.assertEqual(post.kwargs["method"], "POST")
        self.assertEqual(post.kwargs["payload"]["Do"], "manually-merged")
        self.assertEqual(post.kwargs["payload"]["MergeCommitID"], merge)

    def test_manual_merge_rejects_wrong_parent_before_write(self):
        api = Gitea()
        head, merge = "a" * 40, "b" * 40
        with patch.object(api, "_call", side_effect=[
            {"login": "gpt"},
            {"state": "open", "merged": False,
             "head": {"sha": head}, "base": {"ref": "master"}},
            {"commit": {"id": merge}},
            {"parents": [{"sha": "c" * 40}, {"sha": "d" * 40}]},
        ]) as mocked:
            with self.assertRaisesRegex(RuntimeError, "second parent"):
                api.mark_manually_merged("hbf/hbf-api-tools", 3,
                                         expected_head_sha=head, merge_commit_sha=merge)
        self.assertEqual(mocked.call_count, 4)

    def test_comment_and_pr_have_agent_footer(self):
        api = YouTrack()
        with patch.object(youtrack_module, "request_json", side_effect=[
                {"login": "gpt"}, {"id": "c1", "author": {"login": "gpt"}}]) as request:
            api.add_comment("ESE-1", "Checked in CI")
        self.assertEqual(request.call_args.kwargs["payload"]["text"],
                         "Checked in CI\n\nadded by gpt-lindir")
        git = Gitea()
        with patch.object(git, "_call", side_effect=[
                {"login": "gpt"}, [], {"number": 10, "state": "open", "user": {"login": "gpt"}}]) as call:
            git.create_pull_request("hbf/hbf-api-tools", "title", "head", "master", "description")
        self.assertEqual(call.call_args.kwargs["payload"]["body"],
                         "description\n\nadded by gpt-lindir")

    def test_credential_lookup_defaults_only_to_home_not_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            forbidden = root / "checkout" / ".credentials"
            forbidden.mkdir(parents=True, mode=0o700)
            with patch.dict(os.environ, {}, clear=True), \
                 patch("hbf_api_tools.config.Path.home", return_value=home):
                with self.assertRaisesRegex(config.ConfigurationError, "~/.credentials"):
                    config.credentials_dir()
                (home / ".credentials").mkdir(mode=0o700)
                self.assertEqual(config.credentials_dir(), home / ".credentials")

    def test_gitea_default_repo_allowlist(self):
        api = Gitea()
        self.assertEqual(api._repo("secnet/ese-agent"), "/repos/secnet/ese-agent")
        with self.assertRaisesRegex(ValueError, "allowlist"):
            api._repo("other/repo")

    def test_gitea_rejects_invalid_custom_allowlist(self):
        with patch.dict(os.environ, {"HBF_GITEA_REPOSITORIES": "secnet/../secret"}):
            with self.assertRaisesRegex(config.ConfigurationError, "allowlist"):
                Gitea()

    def test_youtrack_rejects_invalid_issue_ids(self):
        api = YouTrack()
        for issue in ("../admin", "ESE-0", "ESE-1?fields=x", " bad"):
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                api.issue(issue)

    def test_search_bounds(self):
        with self.assertRaisesRegex(ValueError, "pagination"):
            YouTrack().search(limit=101)

    def test_pr_bounds(self):
        with self.assertRaisesRegex(ValueError, "state or pagination"):
            Gitea().pull_requests("secnet/ese-agent", state="wat")

    def test_pr_files_bounds(self):
        with self.assertRaisesRegex(ValueError, "PR or pagination"):
            Gitea().pull_files("secnet/ese-agent", 0)

    def test_commit_status_returns_bounded_summary(self):
        api = Gitea()
        payload = {"state": "success", "sha": "abc", "total_count": 51,
                   "repository": {"permissions": {"admin": True}},
                   "statuses": [{"id": n, "status": "success", "context": f"job-{n}",
                                 "description": "ok", "private_field": "drop"}
                                for n in range(51)]}
        with patch.object(api, "_call", return_value=payload):
            result = api.commit_status("secnet/ese-backend", "master")
        self.assertEqual(result["total_count"], 51)
        self.assertEqual(len(result["statuses"]), 50)
        self.assertTrue(result["statuses_truncated"])
        self.assertNotIn("repository", result)
        self.assertNotIn("private_field", result["statuses"][0])

    def test_create_pr_checks_identity_and_duplicate_before_write(self):
        api = Gitea()
        with patch.object(api, "_call", side_effect=[
                {"login": "gpt"}, [], {"number": 42, "state": "open", "html_url": "https://git.hbf.pl/pr/42",
                                       "user": {"login": "gpt"}, "head": {"sha": "a" * 40}}]) as call:
            result = api.create_pull_request("hbf/hbf-api-tools", "Tests", "feat/portable-api-tools",
                                             "master", "CI-ready body")
        self.assertTrue(result["created"])
        self.assertEqual(result["number"], 42)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(call.call_args.kwargs["method"], "POST")

    def test_create_pr_does_not_duplicate_matching_head_and_base(self):
        api = Gitea()
        existing = [{"number": 42, "state": "open", "html_url": "https://git.hbf.pl/pr/42",
                     "head": {"ref": "feat/portable-api-tools", "sha": "a" * 40,
                              "repo": {"full_name": "hbf/hbf-api-tools"}},
                     "base": {"ref": "master"}}]
        with patch.object(api, "_call", side_effect=[{"login": "gpt"}, existing]) as call:
            result = api.create_pull_request("hbf/hbf-api-tools", "Tests", "feat/portable-api-tools",
                                             "master", "body")
        self.assertFalse(result["created"])
        self.assertTrue(result["existing"])
        self.assertEqual(call.call_count, 2)

    def test_cli_pr_without_apply_does_not_load_credentials(self):
        with patch("hbf_api_tools.cli.Gitea", side_effect=AssertionError("must not load")):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["gitea", "create-pr", "hbf/hbf-api-tools",
                                       "--title", "Tests", "--head", "branch"]), 2)

    def test_dsh_requires_valid_issue_and_budget(self):
        with self.assertRaises(ValueError):
            flash_task("../ESE-1", "A bounded task description long enough", workdir="/tmp")
        with self.assertRaises(ValueError):
            flash_task("ESE-1", "A bounded task description long enough", workdir="/tmp", max_usd=2)
        with self.assertRaisesRegex(ValueError, "outside HBF_DSH_ALLOWED_WORKDIRS"):
            flash_task("ESE-1", "A bounded task description long enough", workdir="/tmp")
        with self.assertRaisesRegex(ValueError, "outside HBF_DSH_ALLOWED_WORKDIRS"):
            flash_task("ESE-1", "A bounded task description long enough",
                       workdir="/home/afronczek/esekretariat/../../etc")

    def test_cli_write_without_apply_does_not_load_credentials(self):
        with patch("hbf_api_tools.cli.YouTrack", side_effect=AssertionError("must not load")):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["youtrack", "add-comment", "ESE-1"]), 2)

    def test_youtrack_write_requires_gpt_identity(self):
        api = YouTrack()
        with patch.object(youtrack_module, "request_json", return_value={"login": "owner"}) as call:
            with self.assertRaisesRegex(config.ConfigurationError, "token does not match local agent identity"):
                api.add_comment("ESE-1", "a comment")
        self.assertEqual(call.call_count, 1)

    def test_youtrack_write_confirms_comment_author(self):
        api = YouTrack()
        with patch.object(youtrack_module, "request_json", side_effect=[
                {"login": "gpt"}, {"id": "comment-1", "author": {"login": "gpt"}}]) as call:
            self.assertEqual(api.add_comment("ESE-1", "a comment"),
                             {"id": "comment-1", "author": "gpt"})
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
