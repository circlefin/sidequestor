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
             approvals: list[dict] | None = None,
             response_channel_id: str = "C123",
             response_body: str | None = None) -> tuple[int, str, str, MagicMock]:
        stdout, stderr = io.StringIO(), io.StringIO()
        call = MagicMock(return_value=response_body or json.dumps({
            "message_context": {
                "message_ts": "2.000001",
                "channel_id": response_channel_id,
            },
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

    def test_user_id_send_records_resolved_dm_channel_id(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, output, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "U123",
            "message": "hello",
        }, target=quest_id, response_channel_id="D456")

        self.assertEqual(code, 0, error)
        call.assert_called_once()
        self.assertEqual(json.loads(output)["channel_id"], "D456")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        self.assertEqual(json.loads(timeline.read_text())["channel_id"], "D456")

    def test_draft_resolves_the_dm_channel_from_the_real_response_shape(self) -> None:
        """Verbatim shape of a real slack_send_message_draft response (captured
        2026-09-07): a draft addressed to the member id U0A0SA0UQNQ came back resolved to
        the DM D0A0LMEFWBY, in channel_info and in the link, with no message_context."""
        quest_id = self._quest(allow_send=False)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "U0A0SA0UQNQ",
            "message": "hello",
            "draft": True,
        }, target=quest_id, response_body=json.dumps({
            "channel_link": "https://acme.slack.com/archives/D0A0LMEFWBY",
            "widget_id": "96ce482a-976b-4785-95ed-e7412c3c7e74",
            "channel_info": {"channel_id": "D0A0LMEFWBY",
                             "name": "Guangmian Kung", "is_dm": True},
            "result": "Draft message is created. They can edit it before sending.",
        }))

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["channel_id"], "D0A0LMEFWBY")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        self.assertEqual(json.loads(timeline.read_text())["channel_id"], "D0A0LMEFWBY")

    def test_draft_falls_back_to_the_link_without_channel_info(self) -> None:
        """channel_info is preferred, but the link alone must still resolve it."""
        quest_id = self._quest(allow_send=False)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "U123",
            "message": "hello",
            "draft": True,
        }, target=quest_id, response_body=json.dumps({
            "channel_link": "https://acme.slack.com/client/T0AAAA1/D0BBBB2",
        }))

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["channel_id"], "D0BBBB2")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        self.assertEqual(json.loads(timeline.read_text())["channel_id"], "D0BBBB2")

    def test_draft_keeps_the_requested_channel_when_the_link_holds_no_id(self) -> None:
        """The link fallback is opportunistic: an unrecognised shape must degrade to the
        old behaviour, never to a guessed id."""
        quest_id = self._quest(allow_send=False)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "C0123456789",
            "message": "hello",
            "draft": True,
        }, target=quest_id, response_body=json.dumps({
            "channel_link": "https://acme.slack.com/client",
        }))

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["channel_id"], "C0123456789")

    def test_malformed_response_fields_do_not_strand_a_delivered_message(self) -> None:
        """The parse runs AFTER Slack has the message. A non-string field must not raise
        past the JSONDecodeError handler, or the send lands with nothing logged and a
        retry duplicates it."""
        quest_id = self._quest(allow_send=True)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "C0123456789",
            "message": "hello",
        }, target=quest_id, response_body=json.dumps({
            "message_context": {"message_ts": 2.000001, "channel_id": 12345},
            "message_link": None,
        }))

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["channel_id"], "C0123456789")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        self.assertEqual(json.loads(timeline.read_text())["message_text"], "hello")

    def test_a_non_object_json_response_is_survivable(self) -> None:
        """Valid JSON that is not an object (a bare list or string) must not raise past the
        JSONDecodeError handler either — same delivered-but-unlogged hazard."""
        quest_id = self._quest(allow_send=True)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "C0123456789",
            "message": "hello",
        }, target=quest_id, response_body=json.dumps(["unexpected", "shape"]))

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["channel_id"], "C0123456789")
        self.assertEqual(json.loads(output)["response_ts"], "")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        self.assertEqual(json.loads(timeline.read_text())["message_text"], "hello")

    def test_unusable_response_channel_does_not_replace_the_requested_one(self) -> None:
        """A response value only wins if it is itself a conversation id; otherwise the
        requested id stands rather than the bug moving one step downstream."""
        quest_id = self._quest(allow_send=True)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "C0123456789",
            "message": "hello",
        }, target=quest_id, response_channel_id="not-an-id")

        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["channel_id"], "C0123456789")

    def test_link_fallback_takes_the_conversation_not_the_team_id(self) -> None:
        self.assertEqual(
            self.mod._channel_id_from_link(
                "https://acme.slack.com/client/T0AAAA1/D0BBBB2"), "D0BBBB2")
        self.assertEqual(
            self.mod._channel_id_from_link(
                "https://acme.slack.com/archives/C0123456789/p1700000000123456"),
            "C0123456789")
        self.assertEqual(
            self.mod._channel_id_from_link(
                "https://acme.slack.com/archives/C0123456789?thread_ts=1.2"),
            "C0123456789")
        for empty in ("https://acme.slack.com/client", "", None, 12345):
            with self.subTest(link=empty):
                self.assertEqual(self.mod._channel_id_from_link(empty), "")

    def test_enterprise_grid_member_id_is_refused_on_a_threaded_send(self) -> None:
        """W… is the Enterprise Grid member prefix and is just as wrong as U… here."""
        quest_id = self._quest(allow_send=True)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "W0AAAA1",
            "thread_ts": "1.000001",
            "message": "hello",
        }, target=quest_id)

        self.assertEqual(code, 1)
        self.assertIn("member id", error)
        call.assert_not_called()

    def test_threaded_send_refuses_a_member_id_destination(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "U123",
            "thread_ts": "1.000001",
            "message": "hello",
        }, target=quest_id)

        self.assertEqual(code, 1)
        self.assertIn("member id", error)
        call.assert_not_called()

    def test_unthreaded_dm_by_member_id_is_still_allowed(self) -> None:
        """Opening a DM by user ID is the normal way to start one; only threaded sends
        need the conversation named explicitly."""
        quest_id = self._quest(allow_send=True)
        code, output, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "U123",
            "message": "hello",
        }, target=quest_id, response_channel_id="D0BBBB2")

        self.assertEqual(code, 0, error)
        call.assert_called_once()
        self.assertEqual(json.loads(output)["channel_id"], "D0BBBB2")

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

    def test_claimed_slack_approval_overrides_allow_send(self) -> None:
        quest_id = self._quest(allow_send=False)
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

    def test_legacy_watch_mode_does_not_block_send(self) -> None:
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
        self.assertEqual(code, 0, error)
        call.assert_called_once()

    def test_watch_state_does_not_participate_in_send_authorization(self) -> None:
        quest_id = self._quest(allow_send=True)
        watch_path = (self.root / "state" / "quests" / "active" / quest_id
                      / "watch.json")
        watch_path.write_text("not json")
        code, _, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "message": "hello",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        call.assert_called_once()

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
