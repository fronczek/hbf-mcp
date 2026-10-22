from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from hbf_api_tools import config
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
            ("youtrack.yml", "youtrack_url: https://youtrack.hbf.pl\nyoutrack_token_value: secret\n"),
            ("gitea.yml", "gitea_url: https://git.hbf.pl\ntoken_value: secret\n"),
        ):
            path = self.cred / name
            path.write_text(contents)
            path.chmod(0o600)
        self.env = patch.dict(os.environ, {"HBF_CREDENTIALS_DIR": str(self.cred)}, clear=False)
        self.env.start()
        os.environ.pop("HBF_GITEA_REPOSITORIES", None)

    def tearDown(self):
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
            with self.assertRaisesRegex(config.ConfigurationError, "not the GPT identity"):
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
