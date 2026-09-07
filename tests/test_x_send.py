from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SURFACES = ROOT / "src/sidequestor/runtime/yaas-triage/surfaces"


def load(name, path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class XSendTests(unittest.TestCase):
    def setUp(self):
        self.module = load("x_send_test", SURFACES / "x-send.py")

    def test_post_uses_user_identity_and_post_endpoint(self):
        with mock.patch.object(self.module, "user_identity", return_value={"id": "42"}), \
                mock.patch.object(self.module, "x_call", return_value={
                    "data": {"id": "99", "text": "hello"}}) as call:
            result = self.module.execute({"action": "post", "text": "hello"})
        call.assert_called_once_with("POST", "/2/tweets", {}, {"text": "hello"}, "default")
        self.assertEqual("99", result["id"])

    def test_reply_and_social_actions_bind_to_the_authorized_user(self):
        cases = {
            "reply": ("POST", "/2/tweets", {"text": "yes", "reply": {"in_reply_to_tweet_id": "7"}}),
            "like": ("POST", "/2/users/42/likes", {"tweet_id": "7"}),
            "unlike": ("DELETE", "/2/users/42/likes/7", None),
            "repost": ("POST", "/2/users/42/retweets", {"tweet_id": "7"}),
            "bookmark": ("POST", "/2/users/42/bookmarks", {"tweet_id": "7"}),
            "follow": ("POST", "/2/users/42/following", {"target_user_id": "7"}),
            "mute": ("POST", "/2/users/42/muting", {"target_user_id": "7"}),
            "block": ("POST", "/2/users/42/blocking", {"target_user_id": "7"}),
        }
        for action, (method, path, body) in cases.items():
            payload = {"action": action, "text": "yes", "post_id": "7", "user_id": "7"}
            with self.subTest(action=action), \
                    mock.patch.object(self.module, "user_identity", return_value={"id": "42"}), \
                    mock.patch.object(self.module, "x_call", return_value={"data": {}}) as call:
                self.module.execute(payload)
                call.assert_called_once_with(method, path, {}, body, "default")

    def test_dm_can_target_a_participant_or_existing_conversation(self):
        with mock.patch.object(self.module, "user_identity", return_value={"id": "42"}), \
                mock.patch.object(self.module, "x_call", return_value={"data": {"dm_event_id": "9"}}) as call:
            self.module.execute({"action": "dm", "participant_id": "7", "text": "hello"})
        call.assert_called_once_with(
            "POST", "/2/dm_conversations/with/7/messages", {}, {"text": "hello"}, "default")

    def test_dispatched_write_requires_active_allow_send_quest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quest = root / "state/quests/active/quest-x"
            quest.mkdir(parents=True)
            (quest / "meta.json").write_text(json.dumps({"allow_send": False}))
            with mock.patch.object(self.module, "REPO_ROOT", root), \
                    mock.patch.dict(self.module.os.environ,
                                    {"SIDEQUESTOR_DISPATCH_TARGET": "quest-x"}, clear=False):
                reason = self.module.send_policy_reason({"quest_id": "quest-x", "action": "post"})
            self.assertIn("allow_send false", reason)

    def test_thread_posts_each_item_as_a_reply_to_the_previous_one(self):
        responses = [
            {"data": {"id": "10", "text": "one"}},
            {"data": {"id": "11", "text": "two"}},
        ]
        with mock.patch.object(self.module, "user_identity", return_value={"id": "42"}), \
                mock.patch.object(self.module, "x_call", side_effect=responses) as call:
            result = self.module.execute({"action": "thread", "messages": ["one", "two"]})
        self.assertEqual(["10", "11"], result["ids"])
        self.assertEqual(
            {"text": "two", "reply": {"in_reply_to_tweet_id": "10"}},
            call.call_args_list[1].args[3],
        )

    def test_approval_target_binds_quote_and_media_coordinates(self):
        target = self.module._approval_target({
            "action": "post", "quote_post_id": "8", "media_ids": ["m1", "m2"],
        })
        self.assertEqual({
            "surface": "x", "action": "post", "quote_post_id": "8",
            "media_ids": ["m1", "m2"],
        }, target)

    def test_idempotency_key_cannot_be_reused_for_another_payload(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(self.module, "REPO_ROOT", Path(tmp)), \
                mock.patch.object(self.module, "execute", return_value={"id": "1"}):
            self.module._with_idempotency({"action": "post", "text": "one",
                                           "idempotency_key": "same"})
            with self.assertRaisesRegex(RuntimeError, "different X action"):
                self.module._with_idempotency({"action": "post", "text": "two",
                                               "idempotency_key": "same"})

    def test_message_approval_must_bind_the_exact_text(self):
        item = {
            "id": "approval-1", "quest_id": "quest-x", "status": "executing",
            "action_type": "remote_request", "target": {"surface": "x", "action": "post"},
            "lease_expires_at": "2999-01-01T00:00:00Z",
        }
        with mock.patch.object(self.module.approval_store, "read_queue",
                               return_value={"items": [item]}):
            self.assertFalse(self.module._claimed_approval({
                "approval_id": "approval-1", "quest_id": "quest-x",
                "action": "post", "text": "changed text",
            }))


if __name__ == "__main__":
    unittest.main()
