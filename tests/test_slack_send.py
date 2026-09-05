from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SLACK_SEND = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
              / "surfaces" / "slack-send.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("slack_send_under_test", SLACK_SEND)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SlackSendStaleGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    @staticmethod
    def _approval(reviewed_at: str, *, thread_ts: str = "parent-ts") -> dict:
        return {
            "id": "appr-1",
            "quest_id": "quest-1",
            "status": "executing",
            "action_type": "slack_message",
            "target": {"channel_id": "C123", "thread_ts": thread_ts},
            "lease_expires_at": "2999-01-01T00:00:00+00:00",
            "reviewed_at": reviewed_at,
        }

    def test_reviewed_at_refreshes_an_old_thread(self) -> None:
        reviewed_at = "2026-12-08T00:00:00+00:00"
        reviewed_epoch = self.mod.datetime.fromisoformat(reviewed_at).timestamp()
        now = reviewed_epoch + 6 * 3600
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=reviewed_epoch - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={
                 "items": [self._approval(reviewed_at)],
             }):
            self.assertIsNone(
                self.mod._stale_reason(
                    "C123", "parent-ts", now=now, approval_id="appr-1",
                    quest_id="quest-1",
                )
            )

    def test_old_review_does_not_refresh_an_old_thread_forever(self) -> None:
        now = 1_800_000_000.0
        reviewed_at = "2026-11-20T00:00:00+00:00"
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=now - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={
                 "items": [self._approval(reviewed_at)],
             }):
            reason = self.mod._stale_reason(
                "C123", "parent-ts", now=now, approval_id="appr-1",
                quest_id="quest-1",
            )
            self.assertIn("48.0h old", reason)

    def test_missing_approval_timestamp_preserves_existing_guard(self) -> None:
        now = 1_800_000_000.0
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=now - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={"items": []}):
            reason = self.mod._stale_reason(
                "C123", "parent-ts", now=now, approval_id="missing",
            )
            self.assertIsNotNone(reason)

    def test_review_for_another_thread_does_not_refresh_this_thread(self) -> None:
        now = 1_800_000_000.0
        reviewed_at = datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat()
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=now - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={
                 "items": [self._approval(reviewed_at, thread_ts="another-thread")],
             }):
            reason = self.mod._stale_reason(
                "C123", "parent-ts", now=now, approval_id="appr-1",
                quest_id="quest-1",
            )
        self.assertIn("48.0h old", reason)


class SlackSendAuthorizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-slack-send-")
        self.root = Path(self.temp.name)
        (self.root / "state" / "quests" / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _quest(self, *, allow_send: bool, watches: list[dict] | None = None) -> str:
        quest_id = "quest-policy-test"
        quest = self.root / "state" / "quests" / "active" / quest_id
        quest.mkdir()
        (quest / "meta.json").write_text(json.dumps({
            "id": quest_id,
            "status": "active",
            "allow_send": allow_send,
        }))
        (quest / "watch.json").write_text(json.dumps({"watches": watches or []}))
        (quest / "timeline.ndjson").touch()
        return quest_id

    def _run(self, payload: dict, *, target: str | None = None,
             approvals: list[dict] | None = None) -> tuple[int, str, str, MagicMock]:
        stdout, stderr = io.StringIO(), io.StringIO()
        call = MagicMock(return_value=json.dumps({
            "message_context": {"message_ts": "2.000001", "channel_id": "C123"},
            "message_link": "https://example.test/message",
        }))
        env = {} if target is None else {"SIDEQUESTOR_DISPATCH_TARGET": target}
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod, "_call_slack", call), \
             patch.object(self.mod, "_stale_reason", return_value=None), \
             patch.object(self.mod.approval_store, "read_queue", return_value={
                 "items": approvals or [],
             }), \
             patch.object(self.mod.sys, "argv", ["slack-send.py", json.dumps(payload)]), \
             patch.dict(self.mod.os.environ, env, clear=True), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                result = self.mod.main()
            except SystemExit as exc:
                result = int(exc.code)
        return int(result or 0), stdout.getvalue(), stderr.getvalue(), call

    def test_allow_send_true_can_send_for_matching_dispatch(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "message": "hello",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_allow_send_false_fails_before_slack_call(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "message": "hello",
        }, target=quest_id)
        self.assertEqual(code, 1)
        self.assertIn("allow_send", error)
        call.assert_not_called()

    def test_claimed_slack_approval_overrides_allow_send_and_read_only(self) -> None:
        quest_id = self._quest(allow_send=False, watches=[{
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "watch_mode": "read_only",
        }])
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-1",
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "message": "approved",
        }, target=quest_id, approvals=[{
            "id": "appr-1",
            "quest_id": quest_id,
            "status": "executing",
            "action_type": "slack_message",
            "target": {"channel_id": "C123", "thread_ts": "1.000001"},
            "lease_expires_at": "2999-01-01T00:00:00+00:00",
        }])
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_read_only_watch_fails_without_specific_approval(self) -> None:
        quest_id = self._quest(allow_send=True, watches=[{
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "watch_mode": "read_only",
        }])
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "message": "hello",
        }, target=quest_id)
        self.assertEqual(code, 1)
        self.assertIn("read_only", error)
        call.assert_not_called()

    def test_manual_instruction_does_not_override_allow_send(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-manual",
            "channel_id": "C123",
            "message": "hello",
        }, target=quest_id, approvals=[{
            "id": "appr-manual",
            "quest_id": quest_id,
            "status": "executing",
            "action_type": "manual_instruction",
            "lease_expires_at": "2999-01-01T00:00:00+00:00",
        }])
        self.assertEqual(code, 1)
        self.assertIn("allow_send", error)
        call.assert_not_called()

    def test_expired_slack_approval_does_not_override_allow_send(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-expired",
            "channel_id": "C123",
            "message": "hello",
        }, target=quest_id, approvals=[{
            "id": "appr-expired",
            "quest_id": quest_id,
            "status": "executing",
            "action_type": "slack_message",
            "lease_expires_at": "2000-01-01T00:00:00+00:00",
        }])
        self.assertEqual(code, 1)
        self.assertIn("allow_send", error)
        call.assert_not_called()

    def test_quest_dispatch_cannot_send_as_another_quest(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "message": "hello",
        }, target="quest-someone-else")
        self.assertEqual(code, 1)
        self.assertIn("dispatch target", error)
        call.assert_not_called()

    def test_reactions_dispatch_preserves_unscoped_send(self) -> None:
        code, _, error, call = self._run({
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "message": "reaction response",
        }, target="reactions")
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_draft_cannot_escape_its_dispatch_quest(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "message": "draft",
            "draft": True,
        }, target="quest-someone-else")
        self.assertEqual(code, 1)
        self.assertIn("dispatch target", error)
        call.assert_not_called()

    def _claimed(self, quest_id: str, target: dict | None) -> dict:
        item = {
            "id": "appr-1",
            "quest_id": quest_id,
            "status": "executing",
            "action_type": "slack_message",
            "lease_expires_at": "2999-01-01T00:00:00+00:00",
        }
        if target is not None:
            item["target"] = target
        return item

    def test_claimed_approval_does_not_authorize_another_thread(self) -> None:
        """The reviewer approved one message to one place; the claim ends there."""
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-1",
            "channel_id": "C123",
            "thread_ts": "9.999999",
            "message": "somewhere else",
        }, target=quest_id, approvals=[
            self._claimed(quest_id, {"channel_id": "C123", "thread_ts": "1.000001"}),
        ])
        self.assertEqual(code, 1)
        self.assertIn("allow_send", error)
        call.assert_not_called()

    def test_claimed_approval_does_not_authorize_another_channel(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-1",
            "channel_id": "C999",
            "message": "wrong channel",
        }, target=quest_id, approvals=[
            self._claimed(quest_id, {"channel_id": "C123", "thread_ts": None}),
        ])
        self.assertEqual(code, 1)
        call.assert_not_called()

    def test_targetless_approval_authorizes_nothing(self) -> None:
        """approval-helper defaults target to {}, so None must not match None."""
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-1",
            "channel_id": "C123",
            "message": "no reviewed target",
        }, target=quest_id, approvals=[self._claimed(quest_id, {})])
        self.assertEqual(code, 1)
        call.assert_not_called()

    def test_claimed_approval_matches_a_top_level_target(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-1",
            "channel_id": "C123",
            "message": "approved top-level post",
        }, target=quest_id, approvals=[
            self._claimed(quest_id, {"channel_id": "C123", "thread_ts": None}),
        ])
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_legacy_approval_without_action_type_is_treated_as_slack(self) -> None:
        """approval_store defaults the field on read, so the guard stays strict."""
        quest_id = self._quest(allow_send=False)
        legacy = self._claimed(quest_id, {"channel_id": "C123", "thread_ts": None})
        del legacy["action_type"]
        self.mod.approval_store._validate_queue({"items": [legacy]})
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "approval_id": "appr-1",
            "channel_id": "C123",
            "message": "legacy item",
        }, target=quest_id, approvals=[legacy])
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_read_only_channel_watch_does_not_gag_the_whole_channel(self) -> None:
        """read_only is a per-thread rule; a channel watch must not block every send."""
        quest_id = self._quest(allow_send=True, watches=[{
            "type": "slack_channel",
            "channel_id": "C123",
            "watch_mode": "read_only",
        }])
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "message": "unrelated thread",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_read_only_watch_does_not_block_a_different_thread(self) -> None:
        quest_id = self._quest(allow_send=True, watches=[{
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "watch_mode": "read_only",
        }])
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "thread_ts": "7.000007",
            "message": "a different thread",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_a_quest_outside_active_may_not_send(self) -> None:
        quest_id = self._quest(allow_send=True)
        completed = self.root / "state" / "quests" / "completed"
        completed.mkdir(parents=True)
        (self.root / "state" / "quests" / "active" / quest_id).rename(completed / quest_id)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "message": "after the move",
        }, target=quest_id)
        self.assertEqual(code, 1)
        self.assertIn("is not in state/quests/active", error)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
