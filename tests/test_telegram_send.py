from __future__ import annotations

import importlib.util
import asyncio
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_SEND = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
                 / "surfaces" / "telegram-send.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("telegram_send_under_test", TELEGRAM_SEND)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TelegramSendAuthorizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-telegram-send-")
        self.root = Path(self.temp.name)
        (self.root / "state" / "quests" / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _quest(self, *, allow_send: bool) -> str:
        quest_id = "quest-policy-test"
        quest = self.root / "state" / "quests" / "active" / quest_id
        quest.mkdir()
        (quest / "meta.json").write_text(json.dumps({
            "id": quest_id,
            "status": "active",
            "allow_send": allow_send,
        }))
        (quest / "watch.json").write_text(json.dumps({"watches": []}))
        (quest / "timeline.ndjson").touch()
        return quest_id

    def _run(self, payload: dict, *, target: str | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {} if target is None else {"SIDEQUESTOR_DISPATCH_TARGET": target}
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod, "_save_draft", AsyncMock(return_value={
                 "peer": payload.get("peer"), "draft_saved": True,
             })), \
             patch.object(self.mod, "_send_message", AsyncMock(return_value={
                 "peer": payload.get("peer"), "delivered": True, "message_id": "99",
             })), \
             patch.object(self.mod.sys, "argv", ["telegram-send.py", json.dumps(payload)]), \
             patch.dict(self.mod.os.environ, env, clear=True), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                result = self.mod.main()
            except SystemExit as exc:
                result = int(exc.code)
        return int(result or 0), stdout.getvalue(), stderr.getvalue()

    def test_matching_dispatch_saves_and_logs_draft(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, out, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        payload = json.loads(out)
        self.assertTrue(payload["draft_saved"])
        self.assertFalse(payload["delivered"])
        timeline = (self.root / "state" / "quests" / "active" / quest_id / "timeline.ndjson").read_text()
        self.assertIn('"event": "draft_posted"', timeline)
        self.assertIn('"surface": "telegram"', timeline)
        self.assertIn('"channel_id": "@chat"', timeline)
        self.assertIn('"peer": "@chat"', timeline)

    def test_reply_draft_logs_draft_posted_with_reply_target(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, _, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
            "reply_to_message_id": "7",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        timeline = (self.root / "state" / "quests" / "active" / quest_id / "timeline.ndjson").read_text()
        self.assertIn('"event": "draft_posted"', timeline)
        self.assertIn('"reply_to_message_id": "7"', timeline)

    def test_allow_send_false_still_allows_a_draft(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
        }, target=quest_id)
        self.assertEqual(code, 0, error)

    def test_allow_send_true_can_deliver_and_log_a_message(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, out, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
            "send": True,
            "idempotency_key": "send-1",
        }, target=quest_id)
        self.assertEqual(code, 0, error)
        payload = json.loads(out)
        self.assertTrue(payload["delivered"])
        self.assertFalse(payload["draft_saved"])
        self.assertEqual("99", payload["message_id"])
        timeline = (self.root / "state" / "quests" / "active" / quest_id / "timeline.ndjson").read_text()
        self.assertIn('"event": "message_sent"', timeline)
        self.assertIn('"message_id": "99"', timeline)

    def test_allow_send_false_denies_a_direct_send(self) -> None:
        quest_id = self._quest(allow_send=False)
        code, _, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
            "send": True,
            "idempotency_key": "send-1",
        }, target=quest_id)
        self.assertEqual(code, 1)
        self.assertIn("allow_send false", error)

    def test_dispatched_send_requires_idempotency_key(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, _, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
            "send": True,
        }, target=quest_id)
        self.assertEqual(code, 1)
        self.assertIn("idempotency_key", error)

    def test_claimed_approval_binds_peer_reply_and_message(self) -> None:
        quest_id = self._quest(allow_send=False)
        approval = {
            "id": "approval-1",
            "quest_id": quest_id,
            "status": "executing",
            "action_type": "remote_request",
            "target": {"surface": "telegram", "action": "send", "peer": "@chat",
                       "reply_to_message_id": "7"},
            "message_text": "hello",
            "lease_expires_at": "2999-01-01T00:00:00Z",
        }
        payload = {
            "approval_id": "approval-1", "quest_id": quest_id, "peer": "@chat",
            "reply_to_message_id": "7", "message": "hello", "send": True,
            "idempotency_key": "approved-send",
        }
        with patch.object(self.mod.approval_store, "read_queue",
                          return_value={"items": [approval]}):
            code, _, error = self._run(payload, target=quest_id)
        self.assertEqual(code, 0, error)

    def test_dispatched_draft_requires_matching_quest(self) -> None:
        quest_id = self._quest(allow_send=True)
        code, _, error = self._run({
            "quest_id": quest_id,
            "peer": "@chat",
            "message": "hello",
        }, target="quest-someone-else")
        self.assertEqual(code, 1)
        self.assertIn("dispatch target", error)

    def test_dispatched_draft_requires_quest_id(self) -> None:
        code, _, error = self._run({
            "peer": "@chat",
            "message": "hello",
        }, target="quest-policy-test")
        self.assertEqual(code, 1)
        self.assertIn("quest_id is required", error)

    def test_manual_draft_without_dispatch_target_is_allowed(self) -> None:
        code, out, error = self._run({
            "peer": "@chat",
            "message": "hello",
        })
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(out)["logged"])

    def test_draft_over_telegram_limit_is_rejected_before_transport(self) -> None:
        code, _, error = self._run({
            "peer": "@chat",
            "message": "x" * (self.mod.MAX_DRAFT_LENGTH + 1),
        })
        self.assertEqual(code, 1)
        self.assertIn("4096-character", error)

    def test_send_requires_a_json_boolean(self) -> None:
        code, _, error = self._run({
            "peer": "@chat", "message": "hello", "send": "false",
        })
        self.assertEqual(code, 1)
        self.assertIn("send must be true or false", error)

    def test_exact_dialog_title_can_resolve_when_it_is_unique(self) -> None:
        class FakeClient:
            async def get_entity(self, _value):
                raise ValueError("not a username")

            async def iter_dialogs(self):
                yield types.SimpleNamespace(
                    name="Bruncle's Brunch Bunch",
                    entity=types.SimpleNamespace(peer_id=7),
                )

        utils = types.ModuleType("telethon.utils")
        utils.get_peer_id = lambda entity: entity.peer_id
        telethon = types.ModuleType("telethon")
        telethon.utils = utils
        with patch.dict(sys.modules, {"telethon": telethon, "telethon.utils": utils}):
            peer = asyncio.run(self.mod._resolve_peer(
                FakeClient(), "Bruncle's Brunch Bunch"))
        self.assertEqual(7, peer.peer_id)

    def test_native_draft_uses_save_draft_request(self) -> None:
        class FakeSaveDraftRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeInputReplyToMessage:
            def __init__(self, reply_to_msg_id):
                self.reply_to_msg_id = reply_to_msg_id

        class FakeClient:
            def __init__(self, *args):
                self.request = None

            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_entity(self, value):
                self.requested = value
                return types.SimpleNamespace(peer_id=7)

            async def iter_dialogs(self):
                yield types.SimpleNamespace(
                    entity=types.SimpleNamespace(peer_id=7, name="existing-dialog"),
                )

            async def __call__(self, request):
                self.request = request
                return True

            async def disconnect(self):
                return None

        client = FakeClient()
        modules = {
            "telethon": types.ModuleType("telethon"),
            "telethon.sessions": types.ModuleType("telethon.sessions"),
            "telethon.tl": types.ModuleType("telethon.tl"),
            "telethon.tl.functions": types.ModuleType("telethon.tl.functions"),
            "telethon.tl.functions.messages": types.ModuleType("telethon.tl.functions.messages"),
            "telethon.tl.types": types.ModuleType("telethon.tl.types"),
            "telethon.utils": types.ModuleType("telethon.utils"),
        }
        modules["telethon"].TelegramClient = lambda *args: client
        modules["telethon"].utils = modules["telethon.utils"]
        modules["telethon.sessions"].StringSession = lambda value: value
        modules["telethon.tl.functions.messages"].SaveDraftRequest = FakeSaveDraftRequest
        modules["telethon.tl.types"].InputReplyToMessage = FakeInputReplyToMessage
        modules["telethon.utils"].get_peer_id = lambda entity: entity.peer_id

        with patch.dict(sys.modules, modules), \
             patch.object(self.mod, "load_bundle", return_value={
                 "session": "session", "api_id": "1", "api_hash": "hash",
             }):
            result = asyncio.run(self.mod._save_draft({
                "peer": "@chat", "message": "hello", "reply_to_message_id": "7",
            }))

        self.assertTrue(result["draft_saved"])
        self.assertIsInstance(client.request, FakeSaveDraftRequest)
        self.assertEqual(client.request.peer.name, "existing-dialog")
        self.assertEqual(client.request.message, "hello")
        self.assertEqual(client.request.reply_to.reply_to_msg_id, 7)

    def test_native_send_delivers_to_the_resolved_dialog(self) -> None:
        class FakeClient:
            def __init__(self, *args):
                self.sent = None

            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_entity(self, value):
                return types.SimpleNamespace(peer_id=7)

            async def iter_dialogs(self):
                yield types.SimpleNamespace(
                    entity=types.SimpleNamespace(peer_id=7, name="existing-dialog"),
                )

            async def send_message(self, peer, message, reply_to=None):
                self.sent = (peer, message, reply_to)
                return types.SimpleNamespace(id=99)

            async def disconnect(self):
                return None

        client = FakeClient()
        modules = {
            "telethon": types.ModuleType("telethon"),
            "telethon.sessions": types.ModuleType("telethon.sessions"),
            "telethon.utils": types.ModuleType("telethon.utils"),
        }
        modules["telethon"].TelegramClient = lambda *args: client
        modules["telethon"].utils = modules["telethon.utils"]
        modules["telethon.sessions"].StringSession = lambda value: value
        modules["telethon.utils"].get_peer_id = lambda entity: entity.peer_id
        with patch.dict(sys.modules, modules), \
             patch.object(self.mod, "load_bundle", return_value={
                 "session": "session", "api_id": "1", "api_hash": "hash",
             }):
            result = asyncio.run(self.mod._send_message({
                "peer": "@chat", "message": "hello", "reply_to_message_id": "7",
            }))
        self.assertTrue(result["delivered"])
        self.assertEqual("99", result["message_id"])
        self.assertEqual("existing-dialog", client.sent[0].name)
        self.assertEqual(("hello", 7), client.sent[1:])

    def test_idempotency_replays_a_completed_send_without_resending(self) -> None:
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod, "_send_message", AsyncMock(return_value={
                 "peer": "@chat", "delivered": True, "message_id": "99",
             })) as send:
            payload = {"peer": "@chat", "message": "hello", "send": True,
                       "idempotency_key": "same"}
            first = self.mod._send_with_idempotency(payload)
            second = self.mod._send_with_idempotency(payload)
        self.assertEqual(first, second)
        send.assert_awaited_once()

    def test_preflight_failure_does_not_poison_idempotency_key(self) -> None:
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod, "_send_message", AsyncMock(
                 side_effect=self.mod.CredentialError("Keychain unavailable"))):
            payload = {"peer": "@chat", "message": "hello", "send": True,
                       "idempotency_key": "retryable"}
            with self.assertRaises(self.mod.CredentialError):
                self.mod._send_with_idempotency(payload)
        records = json.loads((self.root / "state" / "telegram-idempotency.json").read_text())
        self.assertNotIn("retryable", records)


if __name__ == "__main__":
    unittest.main()
