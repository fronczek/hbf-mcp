"""Keep the MCP01 helper defaults aligned with the shared credentials contract."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "dsh-remote": ("DEFAULT_TOKEN_FILE", "dsh-launch-token.txt"),
    "deepseek-api": ("KEY_FILE", "deepseek.txt"),
    "dsh-budget-guard": ("KEY_FILE", "deepseek.txt"),
}


def load_helper(name: str, home: Path, credentials: Path | None = None):
    script = REPO / "scripts" / name
    loader = importlib.machinery.SourceFileLoader(f"helper_{name.replace('-', '_')}", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    env = {} if credentials is None else {"HBF_CREDENTIALS_DIR": str(credentials)}
    with patch.dict(os.environ, env, clear=True), patch.object(Path, "home", return_value=home):
        spec.loader.exec_module(module)
    return module


class HelperCredentialPathTests(unittest.TestCase):
    def test_default_is_private_home_credentials_for_every_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for name, (attribute, filename) in SCRIPTS.items():
                with self.subTest(helper=name):
                    helper = load_helper(name, home)
                    self.assertEqual(getattr(helper, attribute), home / ".credentials" / filename)

    def test_shared_override_is_honored_for_every_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            credentials = Path(tmp) / "shared-secret-dir"
            for name, (attribute, filename) in SCRIPTS.items():
                with self.subTest(helper=name):
                    helper = load_helper(name, home, credentials)
                    self.assertEqual(getattr(helper, attribute), credentials / filename)


if __name__ == "__main__":
    unittest.main()
