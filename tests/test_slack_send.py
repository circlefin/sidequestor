from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
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
             response_body: str | None = None,
             response_error: Exception | None = None,
             stale_reason: str | None = None,
             queue_for_review=None) -> tuple[int, str, str, MagicMock]:
        stdout, stderr = io.StringIO(), io.StringIO()
        call = MagicMock(side_effect=response_error,
                         return_value=response_body if response_body is not None else json.dumps({
            "message_context": {
                "message_ts": "2.000001",
                "channel_id": response_channel_id,
            },
            "message_link": "https://example.test/message",
        }))
        env = {} if target is None else {"SIDEQUESTOR_DISPATCH_TARGET": target}
        queue = queue_for_review or MagicMock(return_value="appr-held")
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod, "_call_slack", call), \
             patch.object(self.mod, "_stale_reason", return_value=stale_reason), \
             patch.object(self.mod, "_queue_for_review", queue), \
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

    def test_send_channel_grant_authorizes_only_the_listed_channel(self) -> None:
        quest_id = self._quest(allow_send=False)
        meta_path = self.root / "state" / "quests" / "active" / quest_id / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["allow_send_channels"] = ["D0OWNER"]
        meta_path.write_text(json.dumps(meta))

        code, _, error, call = self._run({
            "quest_id": quest_id, "channel_id": "D0OWNER", "message": "hello",
        }, target=quest_id, response_channel_id="D0OWNER")
        self.assertEqual(code, 0, error)
        call.assert_called_once()

        code, _, error, call = self._run({
            "quest_id": quest_id, "channel_id": "C0ELSEWHERE", "message": "hello",
        }, target=quest_id)
        self.assertNotEqual(code, 0)
        self.assertIn("allow_send false", error)
        call.assert_not_called()

    def test_stale_held_draft_preserves_the_original_message_body(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, output, error, call = self._run({
            "quest_id": quest_id,
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "message": "Exact pre-review body",
        }, target=quest_id, stale_reason="thread is stale")

        self.assertEqual(code, 0, error)
        call.assert_not_called()
        self.assertTrue(json.loads(output)["held"])
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        event = json.loads(timeline.read_text())
        self.assertEqual(event["event"], "draft_posted")
        self.assertEqual(event["approval_id"], "appr-held")
        self.assertEqual(event["message_text"], "Exact pre-review body")

    def test_stale_reply_that_cannot_be_queued_fails_instead_of_reporting_held(self) -> None:
        # A worker treats `held` as handled. If the review queue write failed, the reply was
        # neither sent nor queued, so claiming it was held would drop it silently.
        quest_id = self._quest(allow_send=True)
        failed = MagicMock(returncode=2, stdout="", stderr="error:approvals file locked")
        with patch.object(self.mod.subprocess, "run", return_value=failed):
            code, output, error, call = self._run({
                "quest_id": quest_id,
                "channel_id": "C123",
                "thread_ts": "1.000001",
                "message": "Reply that must not vanish",
            }, target=quest_id, stale_reason="thread is stale",
                queue_for_review=self.mod._queue_for_review)

        self.assertEqual(code, 2, output)
        call.assert_not_called()
        self.assertEqual(output, "")
        self.assertIn("approvals file locked", error)

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

    def test_malformed_receipt_records_an_uncertain_attempt_without_success(self) -> None:
        """A write may have landed, so preserve its body but never claim delivery."""
        quest_id = self._quest(allow_send=True)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "C0123456789",
            "message": "hello",
        }, target=quest_id, response_body=json.dumps({
            "message_context": {"message_ts": 2.000001, "channel_id": 12345},
            "message_link": None,
        }))

        self.assertEqual(code, 3, error)
        self.assertTrue(json.loads(output)["delivery_uncertain"])
        self.assertEqual(json.loads(output)["channel_id"], "C0123456789")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        entry = json.loads(timeline.read_text())
        self.assertEqual(entry["message_text"], "hello")
        self.assertEqual(entry["event"], "message_delivery_uncertain")
        self.assertEqual(entry["blocker_kind"], "delivery_uncertain")
        self.assertFalse(entry["succeeded"])
        self.assertNotIn("response_ts", entry)

    def test_a_non_object_json_response_is_survivable(self) -> None:
        """Valid JSON that is not an object (a bare list or string) must not raise past the
        JSONDecodeError handler either — same delivered-but-unlogged hazard."""
        quest_id = self._quest(allow_send=True)
        code, output, error, _ = self._run({
            "quest_id": quest_id,
            "channel_id": "C0123456789",
            "message": "hello",
        }, target=quest_id, response_body=json.dumps(["unexpected", "shape"]))

        self.assertEqual(code, 3, error)
        self.assertTrue(json.loads(output)["delivery_uncertain"])
        self.assertEqual(json.loads(output)["channel_id"], "C0123456789")
        self.assertEqual(json.loads(output)["response_ts"], "")
        timeline = (self.root / "state" / "quests" / "active" / quest_id
                    / "timeline.ndjson")
        entry = json.loads(timeline.read_text())
        self.assertEqual(entry["message_text"], "hello")
        self.assertEqual(entry["event"], "message_delivery_uncertain")

    def test_unconfirmed_receipts_never_return_follow_up_coordinates(self) -> None:
        quest_id = self._quest(allow_send=True)
        timeline = self.root / "state/quests/active" / quest_id / "timeline.ndjson"
        for body in ("{}", "", "not JSON", "null", json.dumps({
                "message_context": {"message_ts": "not-a-timestamp", "channel_id": "D456"},
                "message_link": "https://example.test/message"})):
            with self.subTest(body=body):
                timeline.write_text("")
                code, output, error, call = self._run({
                    "quest_id": quest_id, "channel_id": "C123", "message": "attempted body",
                }, target=quest_id, response_body=body)
                self.assertEqual(code, 3, error)
                self.assertIn("inspect Slack before retrying", error)
                result = json.loads(output)
                self.assertTrue(result["delivery_uncertain"])
                self.assertEqual(result["response_ts"], "")
                self.assertEqual(result["permalink"], "")
                entry = json.loads(timeline.read_text())
                self.assertEqual(entry["event"], "message_delivery_uncertain")
                self.assertEqual(entry["message_text"], "attempted body")
                self.assertNotIn("response_ts", entry)
                self.assertNotIn("permalink", entry)
                call.assert_called_once()

    def test_explicit_rejections_do_not_log_a_sent_message_or_draft(self) -> None:
        quest_id = self._quest(allow_send=True)
        timeline = self.root / "state/quests/active" / quest_id / "timeline.ndjson"
        for draft in (False, True):
            for response in ({"delivered": False}, {"ok": False},
                             {"isError": True}, {"error": "not_authed"},
                             {"adapter": "isolated"}):
                if draft and response == {"delivered": False}:
                    continue  # A saved draft intentionally has not been delivered.
                with self.subTest(draft=draft, response=response):
                    code, output, error, call = self._run({
                        "quest_id": quest_id, "channel_id": "C123", "message": "hello",
                        "draft": draft,
                    }, target=quest_id, response_body=json.dumps(response))
                    self.assertEqual(code, 2, error)
                    self.assertEqual(output, "")
                    self.assertEqual(timeline.read_text(), "")
                    if isinstance(response.get("error"), str):
                        self.assertIn(response["error"], error)
                    call.assert_called_once()

    def test_sent_timestamp_confirms_delivery_without_optional_permalink(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, output, error, call = self._run({
            "quest_id": quest_id, "channel_id": "C123", "message": "hello",
        }, target=quest_id, response_body=json.dumps({
            "message_context": {"message_ts": "2.000001", "channel_id": "C123"},
        }))
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["response_ts"], "2.000001")
        self.assertNotIn("delivery_uncertain", json.loads(output))
        entry = json.loads((self.root / "state/quests/active" / quest_id / "timeline.ndjson").read_text())
        self.assertEqual(entry["event"], "message_sent")
        call.assert_called_once()

    def test_saved_draft_need_not_claim_delivery(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, output, error, call = self._run({
            "quest_id": quest_id, "channel_id": "C123", "message": "draft body",
            "draft": True,
        }, target=quest_id, response_body=json.dumps({
            "delivered": False, "channel_link": "https://example.test/channel",
        }))
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["response_ts"], "")
        entry = json.loads((self.root / "state/quests/active" / quest_id / "timeline.ndjson").read_text())
        self.assertEqual(entry["event"], "draft_posted")
        call.assert_called_once()

    def test_missing_receipts_after_timeout_are_uncertain_without_resending(self) -> None:
        quest_id = self._quest(allow_send=True)
        timeline = self.root / "state/quests/active" / quest_id / "timeline.ndjson"
        for error in (self.mod.ReceiptUnavailable("no receipt"),
                      subprocess.TimeoutExpired("mcp-call", 60)):
            with self.subTest(error=type(error).__name__):
                timeline.write_text("")
                code, output, stderr, call = self._run({
                    "quest_id": quest_id, "channel_id": "C123", "message": "hello",
                    "thread_ts": "1.000001",
                }, target=quest_id, response_error=error)
                self.assertEqual(code, 3, stderr)
                self.assertTrue(json.loads(output)["delivery_uncertain"])
                entry = json.loads(timeline.read_text())
                self.assertEqual(entry["event"], "message_delivery_uncertain")
                self.assertEqual(entry["thread_ts"], "1.000001")
                self.assertNotIn("response_ts", entry)
                call.assert_called_once()

    def test_unconfirmed_draft_is_not_logged_as_posted(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, output, error, call = self._run({
            "quest_id": quest_id, "channel_id": "C123", "message": "draft body",
            "draft": True,
        }, target=quest_id, response_body="{}")
        self.assertEqual(code, 3, error)
        self.assertTrue(json.loads(output)["delivery_uncertain"])
        entry = json.loads((self.root / "state/quests/active" / quest_id / "timeline.ndjson").read_text())
        self.assertEqual(entry["event"], "draft_delivery_uncertain")
        self.assertNotIn("response_ts", entry)
        call.assert_called_once()

    def test_uncertainty_audit_failure_does_not_turn_into_a_retryable_send(self) -> None:
        quest_id = self._quest(allow_send=True)
        with patch.object(self.mod, "append_timeline", side_effect=OSError("read only")):
            code, output, error, call = self._run({
                "quest_id": quest_id, "channel_id": "C123", "message": "hello",
            }, target=quest_id, response_body="{}")
        self.assertEqual(code, 3, error)
        self.assertTrue(json.loads(output)["delivery_uncertain"])
        self.assertFalse(json.loads(output)["logged"])
        self.assertIn("could not record uncertain delivery", error)
        call.assert_called_once()

    def test_reaction_uncertainty_is_recorded_without_message_coordinates(self) -> None:
        with patch("inbox.try_log_inbox", return_value=True) as audit:
            code, output, error, call = self._run({
                "channel_id": "C123", "message": "reaction reply", "thread_ts": "1.000001",
            }, target="reactions", response_body="{}")
        self.assertEqual(code, 3, error)
        self.assertTrue(json.loads(output)["logged"])
        audit.assert_called_once()
        entry = audit.call_args.args[2]
        self.assertEqual(entry["event"], "message_delivery_uncertain")
        self.assertFalse(entry["succeeded"])
        self.assertNotIn("response_ts", entry)
        self.assertNotIn("permalink", entry)
        call.assert_called_once()

    def test_uncertainty_diagnostics_redact_credentials_before_auditing(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, output, error, call = self._run({
            "quest_id": quest_id, "channel_id": "C123", "message": "attempted body",
        }, target=quest_id, response_body="unexpected receipt Bearer private-value token=private-token")
        self.assertEqual(code, 3, error)
        entry = json.loads((self.root / "state/quests/active" / quest_id / "timeline.ndjson").read_text())
        for rendered in (output, error, entry["note"]):
            self.assertNotIn("private-value", rendered)
            self.assertNotIn("private-token", rendered)
            self.assertIn("[redacted]", rendered)
        self.assertEqual(entry["message_text"], "attempted body")
        call.assert_called_once()

    def test_real_cli_exits_and_receipts_preserve_failure_and_uncertainty(self) -> None:
        """Exercise the entrypoint and adapter return codes, not only main()."""
        quest_id = self._quest(allow_send=True)
        timeline = self.root / "state/quests/active" / quest_id / "timeline.ndjson"
        stub = self.root / "mcp-stub.py"
        stub.write_text(f"#!{sys.executable}\n"
                        "import os, sys\n"
                        "sys.stdout.write(os.environ.get('TEST_MCP_BODY', ''))\n"
                        "sys.stderr.write(os.environ.get('TEST_MCP_ERROR', ''))\n"
                        "raise SystemExit(int(os.environ.get('TEST_MCP_EXIT', '0')))\n")
        stub.chmod(0o700)
        success = json.dumps({"message_context": {"message_ts": "2.000001", "channel_id": "C123"}})
        cases = [
            ("{}", 0, "", 3, "message_delivery_uncertain"),
            ('{"ok":false}', 0, "", 2, None),
            ("Error: mcp_externally_shared_channel_restricted", 0, "", 2, None),
            ("Error: restricted_action_read_only_channel", 0, "", 2, None),
            ("Error: restricted_action_thread_only_channel", 0, "", 2, None),
            ("Error: restricted_action_non_threadable_channel", 0, "", 2, None),
            ("not_authed", 0, "", 2, None),
            ("unrecognized receipt", 0, "", 3, "message_delivery_uncertain"),
            ("", 0, "", 3, "message_delivery_uncertain"),
            ("", 4, "TimeoutError: timed out", 3, "message_delivery_uncertain"),
            ("", 4, "slack mcp (HTTP 503): service_unavailable", 3, "message_delivery_uncertain"),
            ("", 4, "slack mcp (HTTP 429): ratelimited", 2, None),
            ("", 4, "ERROR: macOS Keychain is locked during Slack credential read", 2, None),
            ("", 4, "ERROR: macOS Keychain timed out during Slack credential write", 2, None),
            ("", 4, "ERROR: Slack credential refresh failed in transport", 2, None),
            ("", 4, "ERROR: Slack credential refresh returned HTTP 503", 2, None),
            ("", 1, "missing_scope", 2, None),
            ("", 3, "invalid arguments", 2, None),
            (success, 0, "", 0, "message_sent"),
        ]
        for body, adapter_exit, adapter_error, expected_exit, event in cases:
            with self.subTest(body=body, adapter_exit=adapter_exit, adapter_error=adapter_error):
                timeline.write_text("")
                environment = dict(os.environ, HOME=str(self.root),
                                   SIDEQUESTOR_WORKSPACE=str(self.root), MCP_CALL=str(stub),
                                   SIDEQUESTOR_CONFIG_HOME=str(self.root / "config"),
                                   YAAS_CONFIG_HOME=str(self.root / "config"),
                                   SIDEQUESTOR_DISPATCH_TARGET=quest_id,
                                   TEST_MCP_BODY=body, TEST_MCP_EXIT=str(adapter_exit),
                                   TEST_MCP_ERROR=adapter_error)
                result = subprocess.run([sys.executable, str(SLACK_SEND), json.dumps({
                    "quest_id": quest_id, "channel_id": "C123", "message": "CLI fixture",
                })], env=environment, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
                if event:
                    entry = json.loads(timeline.read_text())
                    self.assertEqual(entry["event"], event)
                else:
                    self.assertEqual(timeline.read_text(), "")
                if expected_exit == 3:
                    self.assertTrue(json.loads(result.stdout)["delivery_uncertain"])
                    self.assertIn("inspect Slack before retrying", result.stderr)
                    self.assertNotIn("response_ts", entry)
                    self.assertNotIn("permalink", entry)
                    self.assertEqual(entry["blocker_kind"], "delivery_uncertain")
                elif expected_exit == 2:
                    self.assertEqual(result.stdout, "")
                if "mcp_externally_shared_channel_restricted" in body:
                    self.assertIn("mcp_externally_shared_channel_restricted", result.stderr)

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
        code, output, error, call = self._run({
            "channel_id": "C123",
            "thread_ts": "1.000001",
            "message": "reaction response",
        }, target="reactions")
        self.assertEqual(code, 0, error)
        call.assert_called_once()
        self.assertTrue(json.loads(output)["logged"])
        timeline = self.root / "state/quests/active/quest-inbox/timeline.ndjson"
        self.assertEqual(json.loads(timeline.read_text())["message_text"], "reaction response")

    def test_reaction_history_failure_does_not_fail_or_repeat_a_successful_send(self) -> None:
        with patch("inbox.log_inbox", side_effect=OSError("disk unavailable")):
            code, output, error, call = self._run({
                "channel_id": "C123", "thread_ts": "1.000001", "message": "reaction response",
            }, target="reactions")
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["logged"])
        self.assertIn("do not repeat", error)
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
