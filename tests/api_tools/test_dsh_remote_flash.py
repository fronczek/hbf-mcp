"""Offline unit tests of direct DSH Flash dispatch and Web settings restoration."""
import contextlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "dsh-remote"
loader = SourceFileLoader("dsh_remote_under_test", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

DEFAULT = {"provider": "deepseek-official", "model": "deepseek-v4-pro",
           "reasoningEffort": "high"}
FLASH = {"provider": "deepseek-official", "model": "deepseek-flash",
         "reasoningEffort": "low"}


class SettingsTests(unittest.TestCase):
    def test_read_default_and_revision_via_settings_api(self):
        sample = {"namespaces": [
            {"ns": "other-service", "value": {"model": "unrelated"}},
            {"ns": "agent-default-model", "value": {**DEFAULT},
             "revision": 7, "secrets": []},
        ]}
        with patch.object(mod, "rpc", return_value=sample) as rpc:
            self.assertEqual(mod.profile_default(), (DEFAULT, 7))
        rpc.assert_called_once_with("settings/describe", {})

    def test_api_rejects_incomplete_or_missing_settings(self):
        for payload in (None, {}, {"namespaces": []}, {"namespaces": {}},
                        {"namespaces": [{"ns": "agent-default-model", "value": {}}]},
                        {"namespaces": [{"ns": "agent-default-model", "value": DEFAULT,
                                         "revision": "7"}]}):
            with self.subTest(payload=payload), \
                 patch.object(mod, "rpc", return_value=payload):
                with self.assertRaises(RuntimeError):
                    mod.profile_default()

    def test_wait_for_new_revision_not_stale_matching_state(self):
        sequence = [(DEFAULT, 7), (FLASH, 8), (DEFAULT, 9), (DEFAULT, 9)]
        with patch.object(mod, "profile_default", side_effect=sequence) as read, \
             patch.object(mod.time, "sleep"):
            observed = mod.await_default(DEFAULT, after_revision=8)
        self.assertEqual(observed, 9)
        self.assertEqual(read.call_count, 4)

    def test_wait_fails_closed_when_save_stays_old(self):
        with patch.object(mod, "profile_default", return_value=(DEFAULT, 7)), \
             patch.object(mod.time, "monotonic", side_effect=[0, 0, 2]), \
             patch.object(mod.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "task NOT started"):
                mod.await_default(DEFAULT, after_revision=7, timeout=1)

    def test_cost_estimate(self):
        usage = {"uncachedInputTokens": 1_000_000,
                 "cacheReadTokens": 1_000_000, "outputTokens": 1_000_000}
        self.assertAlmostEqual(mod.flash_estimate(usage, mod.FLASH_OFFPEAK), 0.753)
        self.assertAlmostEqual(mod.flash_estimate(usage, mod.FLASH_PEAK), 1.506)

    def test_empty_task_fails_before_api(self):
        with patch.object(mod, "rpc") as rpc, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.flash_dispatch(" ", "/workspace", dispatch_id="ese-1-deadbeef")
        rpc.assert_not_called()


class DispatchTests(unittest.TestCase):
    def run_mocked(self, fail_stage=None):
        events = []
        def mocked_rpc(method, args):
            events.append((method, args))
            if method == "workspace/create":
                return {"workspace": {"workspaceId": "WORK"}}
            if method == "session/create":
                return {"sessionId": args["request"].get("sessionId", "FLASH-SESSION")}
            if method == "session/selectModel":
                return {"selected": {k: v for k, v in args["request"].items()
                                     if k != "sessionId"}}
            if method == "session/prompt":
                return {"accepted": True}
            raise AssertionError(f"unexpected remote API {method}")

        def mocked_item(_):
            return {"running": False, "projections": {"values": {
                "modelSelection": {"next": FLASH, "lastUsed": FLASH},
                "tokenUsage": {"uncachedInputTokens": 20, "cacheReadTokens": 30,
                               "outputTokens": 10},
            }}}

        item_calls = 0
        def item_after_absence(session_id):
            nonlocal item_calls
            item_calls += 1
            return None if item_calls == 1 else mocked_item(session_id)

        def wait(*args, **kwargs):
            events.append(("wait_default", (args, kwargs)))
            if (fail_stage == "flash" and args[0] == FLASH) or (
                    fail_stage == "restore" and args[0] == DEFAULT):
                raise RuntimeError("settings restore not confirmed")
            return 8 if args[0] == FLASH else 9

        with tempfile.TemporaryDirectory() as temp, \
             patch.object(mod, "CONFIG_HOME", Path(temp)), \
             patch.object(mod, "budget_preflight"), \
             patch.object(mod, "session_list", return_value={"items": []}), \
             patch.object(mod, "profile_default", return_value=(DEFAULT, 7)), \
             patch.object(mod, "rpc", side_effect=mocked_rpc), \
             patch.object(mod, "session_item", side_effect=item_after_absence), \
             patch.object(mod, "await_default", side_effect=wait), \
             patch.object(mod, "last_answer") as answer, \
             patch.object(mod.time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            if fail_stage is None:
                mod.flash_dispatch("Reply with ACK", "/workspace", timeout=12,
                                   dispatch_id="ese-1-0123456789abcdef")
            else:
                with self.assertRaisesRegex(RuntimeError, "settings restore not confirmed"):
                    mod.flash_dispatch("Reply with ACK", "/workspace", timeout=12,
                                       dispatch_id="ese-1-0123456789abcdef")
            result = output.getvalue()
        return events, answer, result

    def test_flash_selection_and_revision_verified_before_prompt(self):
        events, answer, result = self.run_mocked()
        calls = [x[0] for x in events]
        self.assertEqual(calls.count("session/selectModel"), 2)
        self.assertEqual(calls.count("wait_default"), 2)
        self.assertLess(calls.index("wait_default"), calls.index("session/prompt"))
        i = calls.index("session/prompt")
        self.assertEqual(calls[i-1], "wait_default")
        prompt = events[i][1]["request"]
        expected_request = str(uuid.uuid5(
            uuid.NAMESPACE_URL, "hbf-dsh:ese-1-0123456789abcdef"))
        self.assertEqual(prompt["requestId"], expected_request)
        selections = [args["request"] for key,args in events if key=="session/selectModel"]
        self.assertEqual(selections[0]["sessionId"],
                         "gpt-flash-ese-1-0123456789abcdef")
        self.assertEqual(selections[0]["model"], "deepseek-flash")
        self.assertEqual(selections[1]["sessionId"], "gpt-flash-default-restorer")
        self.assertEqual(selections[1]["model"], "deepseek-v4-pro")
        waits = [args for key,args in events if key=="wait_default"]
        self.assertEqual(waits[0][1]["after_revision"], 7)
        self.assertEqual(waits[1][1]["after_revision"], 8)
        answer.assert_called_once_with("gpt-flash-ese-1-0123456789abcdef")
        self.assertIn("model used: deepseek-official/deepseek-flash", result)

    def test_failed_flash_wait_still_restores_default_and_never_prompts(self):
        events, answer, _ = self.run_mocked(fail_stage="flash")
        self.assertNotIn("session/prompt", [m for m,_ in events])
        self.assertEqual(len([m for m,_ in events if m=="session/selectModel"]), 2)
        answer.assert_not_called()

    def test_failed_restore_never_prompts(self):
        events, answer, _ = self.run_mocked(fail_stage="restore")
        self.assertNotIn("session/prompt", [m for m,_ in events])
        answer.assert_not_called()

    def test_rejects_running_workspace(self):
        with patch.object(mod, "budget_preflight"), \
             patch.object(mod, "session_list", return_value={"items": [{
                 "sessionId": "pro-working", "running": True, "cwd": "/workspace"}]}), \
             patch.object(mod, "rpc") as rpc, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.flash_dispatch("task", "/workspace",
                                   dispatch_id="ese-1-0123456789abcdef")
        rpc.assert_not_called()

    def test_rejects_existing_stable_dispatch_before_model_change(self):
        existing = {"sessionId": "gpt-flash-ese-1-0123456789abcdef",
                    "running": False, "blank": False}
        with patch.object(mod, "budget_preflight"), \
             patch.object(mod, "session_item", return_value=existing), \
             patch.object(mod, "session_list") as sessions, \
             patch.object(mod, "rpc") as rpc, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.flash_dispatch("task", "/workspace",
                                   dispatch_id="ese-1-0123456789abcdef")
        sessions.assert_not_called()
        rpc.assert_not_called()

    def test_budget_above_normal_ceiling_requires_owner_override(self):
        with patch.dict(mod.os.environ, {}, clear=True), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mod.flash_dispatch("task", "/workspace", max_usd=0.51,
                                   dispatch_id="ese-1-0123456789abcdef")

    def test_dispatch_status_is_bounded_and_costed(self):
        item = {"sessionId": "gpt-flash-ese-1-0123456789abcdef",
                "running": False, "blank": False, "cwd": "/workspace",
                "updatedAt": "2026-10-29T10:00:00Z",
                "projections": {"values": {
                    "modelSelection": {"lastUsed": FLASH},
                    "tokenUsage": {"uncachedInputTokens": 1_000_000,
                                   "cacheReadTokens": 1_000_000,
                                   "outputTokens": 1_000_000},
                }}}
        with patch.object(mod, "session_item", return_value=item), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            mod.dispatch_status("ese-1-0123456789abcdef")
        status = mod.json.loads(output.getvalue())
        self.assertEqual(status["dispatch"], "ese-1-0123456789abcdef")
        self.assertTrue(status["found"])
        self.assertNotIn("prompt", status)
        self.assertEqual(status["estimated_usd"]["off_peak"], 0.753)
        self.assertEqual(status["estimated_usd"]["peak"], 1.506)

    def test_timeout_cancels_session(self):
        events=[]
        def rpc(method,args):
            events.append(method)
            if method=="workspace/create": return {"workspace":{"workspaceId":"WORK"}}
            if method=="session/create":
                return {"sessionId":args["request"].get("sessionId","SESSION")}
            if method=="session/selectModel":
                return {"selected":{k:v for k,v in args["request"].items()
                                    if k!="sessionId"}}
            if method=="session/prompt": return {"accepted":True}
            raise AssertionError(method)

        def item(_):
            return {"running":False,"projections":{"values":{
                "modelSelection":{"next":FLASH,"lastUsed":FLASH},
                "tokenUsage":{}}}}
        item_calls = 0
        def item_after_absence(session_id):
            nonlocal item_calls
            item_calls += 1
            return None if item_calls == 1 else item(session_id)
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(mod, "CONFIG_HOME", Path(temp)), \
             patch.object(mod, "budget_preflight"), \
             patch.object(mod, "session_list", return_value={"items":[]}), \
             patch.object(mod, "profile_default", return_value=(DEFAULT,7)), \
             patch.object(mod, "await_default", side_effect=[8,9]), \
             patch.object(mod, "rpc", side_effect=rpc), \
             patch.object(mod, "session_item", side_effect=item_after_absence), \
             patch.object(mod, "cancel_session") as cancel, \
             patch.object(mod, "last_answer") as answer, \
             patch.object(mod.time,"monotonic",side_effect=[0, 30]), \
             patch.object(mod.time,"sleep"), \
             contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as outcome:
                mod.flash_dispatch("task", "/workspace", timeout=10,
                                   dispatch_id="ese-1-0123456789abcdef")
        self.assertEqual(outcome.exception.code, 124)
        cancel.assert_called_once_with("gpt-flash-ese-1-0123456789abcdef")
        answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
