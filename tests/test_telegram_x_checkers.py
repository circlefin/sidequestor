import asyncio
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CHECKERS = ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "checkers"
SURFACES = ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "surfaces"


def load(name, path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class CheckerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.telegram = load("telegram_checker_test", CHECKERS / "telegram.py")
        cls.x = load("x_checker_test", CHECKERS / "x.py")

    def emitted(self, function):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function()
        return json.loads(output.getvalue())

    def test_telegram_filters_and_advances_complete_window(self):
        page = {
            "messages": [
                {"id": "3", "ts": 180, "sender_id": "42", "text": "not this",
                 "kind": "text", "outgoing": False},
                {"id": "2", "ts": 170, "sender_id": "42", "text": "Ship Sidequestor",
                 "kind": "text", "outgoing": False},
                {"id": "1", "ts": 100, "sender_id": "42", "text": "boundary",
                 "kind": "text", "outgoing": False},
            ],
            "complete": True,
            "advance_to": 200,
        }
        entry = {"peer": "-1001", "last_checked_ts": "100",
                 "filter_keywords": ["sidequestor"]}
        with mock.patch.object(self.telegram, "_call", return_value=page):
            result = self.emitted(lambda: self.telegram.run(entry, now=200))
        self.assertEqual("dirty", result["outcome"])
        self.assertEqual(1, result["count"])
        self.assertEqual("200.000000", result["advance_to"])

    def test_telegram_holds_when_surface_cannot_prove_a_complete_prefix(self):
        entry = {"peer": "-1001", "last_checked_ts": "100"}
        with mock.patch.object(self.telegram, "_call", return_value={
                "messages": [], "complete": False, "reason": "dense second"}):
            result = self.emitted(lambda: self.telegram.run(entry, now=200))
        self.assertEqual("hold", result["outcome"])
        self.assertFalse(result["complete"])
        self.assertNotIn("advance_to", result)

    def test_telegram_rejects_an_unsafe_surface_watermark(self):
        entry = {"peer": "-1001", "last_checked_ts": "100"}
        with mock.patch.object(self.telegram, "_call", return_value={
                "messages": [], "complete": True, "advance_to": 10 ** 20}):
            result = self.emitted(lambda: self.telegram.cli(entry))
        self.assertEqual("error", result["outcome"])
        self.assertFalse(result["complete"])

    def test_telegram_validates_filter_shapes_before_calling_surface(self):
        entry = {"peer": "-1001", "last_checked_ts": "100", "filter_keywords": "word"}
        with mock.patch.object(self.telegram, "_call") as call:
            result = self.emitted(lambda: self.telegram.cli(entry))
        self.assertEqual("misconfig", result["outcome"])
        call.assert_not_called()

    def test_telegram_lag_produces_exact_bounded_query(self):
        entry = {"peer": "@chat", "last_checked_ts": "100", "credential_id": "work"}
        page = {"messages": [], "complete": True, "advance_to": 170}
        with mock.patch.object(self.telegram, "_call", return_value=page) as call:
            result = self.emitted(lambda: self.telegram.run(entry, now=200, lag=30))
        self.assertEqual("clean", result["outcome"])
        self.assertEqual({"credential_id": "work", "peer": "@chat", "after_ts": 100.0,
                          "before_ts": 170.0, "limit": 100}, call.call_args.args[0])

    def test_telegram_floors_ceiling_to_a_settled_second(self):
        entry = {"peer": "@chat", "last_checked_ts": "100"}
        page = {"messages": [], "complete": True, "advance_to": 198}
        with mock.patch.object(self.telegram, "_call", return_value=page) as call:
            result = self.emitted(lambda: self.telegram.run(entry, now=200.9, lag=2))
        self.assertEqual("198.000000", result["advance_to"])
        self.assertEqual(198, call.call_args.args[0]["before_ts"])

    def test_telegram_caps_each_catch_up_window(self):
        entry = {"peer": "@chat", "last_checked_ts": "100"}
        advance_to = 100 + self.telegram.MAX_WINDOW_SECONDS
        page = {"messages": [], "complete": True, "advance_to": advance_to}
        with mock.patch.object(self.telegram, "_call", return_value=page) as call:
            result = self.emitted(lambda: self.telegram.run(entry, now=10 ** 9))
        self.assertEqual(f"{advance_to:.6f}", result["advance_to"])
        self.assertEqual(advance_to, call.call_args.args[0]["before_ts"])

    def test_telegram_permanent_density_is_misconfiguration(self):
        entry = {"peer": "@chat", "last_checked_ts": "100"}
        page = {"messages": [], "complete": False, "permanent": True,
                "reason": "more than 500 messages share one second"}
        with mock.patch.object(self.telegram, "_call", return_value=page):
            result = self.emitted(lambda: self.telegram.cli(entry, lag=2))
        self.assertEqual("misconfig", result["outcome"])

    def test_telegram_transient_holds_watermark(self):
        with mock.patch.object(self.telegram, "run", side_effect=self.telegram.Transient("flood wait")):
            result = self.emitted(lambda: self.telegram.cli({"peer": "x"}))
        self.assertEqual("ratelimited", result["outcome"])
        self.assertFalse(result["complete"])
        self.assertNotIn("advance_to", result)

    def test_x_paginates_to_exhaustion_and_reapplies_boundary(self):
        calls = []

        def fake(_path, params, _auth, timeout=20):
            calls.append(params)
            if "pagination_token" not in params:
                return {"data": [{"id": "11", "created_at": "1970-01-01T00:02:30Z",
                                   "author_id": "9", "text": "new"}],
                        "meta": {"next_token": "next"}}
            return {"data": [{"id": "10", "created_at": "1970-01-01T00:01:40Z",
                               "author_id": "9", "text": "boundary"}], "meta": {}}

        entry = {"last_checked_ts": "100"}
        with mock.patch.object(self.x, "_call", side_effect=fake):
            result = self.emitted(lambda: self.x.run(
                entry, "/2/test", fixed={"query": "from:test"}, now=200))
        self.assertEqual("dirty", result["outcome"])
        self.assertEqual(1, result["count"])
        self.assertEqual("200.000000", result["advance_to"])
        self.assertEqual("next", calls[1]["pagination_token"])
        self.assertEqual("from:test", calls[0]["query"])
        self.assertEqual("1970-01-01T00:03:21Z", calls[0]["end_time"])

    def test_x_slices_after_page_budget_is_exhausted(self):
        end_times = []

        def fake(_path, params, _auth, timeout=20):
            end_times.append(self.x._epoch(params["end_time"]))
            if self.x._epoch(params["end_time"]) > 151:
                return {"data": [], "meta": {"next_token": "more"}}
            return {"data": [], "meta": {}}

        with mock.patch.object(self.x, "_call", side_effect=fake):
            result = self.emitted(lambda: self.x.run(
                {"last_checked_ts": "100"}, "/2/test", now=200))
        self.assertEqual("clean", result["outcome"])
        self.assertEqual("150.000000", result["advance_to"])
        self.assertEqual(6, len(end_times))

    def test_x_deduplicates_pages_and_applies_filters(self):
        def fake(_path, params, _auth, timeout=20):
            row = {"id": "11", "created_at": "1970-01-01T00:02:30Z",
                   "author_id": "9", "text": "release ready"}
            if "pagination_token" not in params:
                return {"data": [row], "meta": {"next_token": "next"}}
            return {"data": [row], "meta": {}}

        entry = {"last_checked_ts": "100", "filter_keywords": ["release"]}
        with mock.patch.object(self.x, "_call", side_effect=fake):
            result = self.emitted(lambda: self.x.run(entry, "/2/test", now=200))
        self.assertEqual(1, result["count"])

    def test_x_partial_errors_fail_closed(self):
        proc = mock.Mock(returncode=0, stdout='{"data":[{"id":"1"}],"errors":[{}]}', stderr="")
        with mock.patch.object(self.x.subprocess, "run", return_value=proc):
            with self.assertRaises(RuntimeError):
                self.x._call("/2/test", {}, "app:default")

    def test_x_local_timeout_returns_an_incomplete_window(self):
        with mock.patch.object(self.x.subprocess, "run",
                               side_effect=self.x.subprocess.TimeoutExpired("x", 1)):
            rows, complete = self.x._fetch_window(
                "/2/test", {}, "app:default", 100, 200,
                self.x.time.monotonic() + 20,
            )
        self.assertEqual([], rows)
        self.assertFalse(complete)

    def test_x_recent_search_clamps_an_unrecoverable_gap_instead_of_parking(self):
        """A watermark behind X's retention floor must clamp forward and say so.

        It used to raise Misconfig, which is terminal: nothing advances a misconfigured
        watch's cursor, so an `x` connector left disabled for eight days (or a machine
        asleep for a week) came back to a watch that only a human could repair.
        """
        with mock.patch.object(self.x, "_call", return_value={"data": [], "meta": {}}) as call:
            result = self.emitted(lambda: self.x.cli(
                {"last_checked_ts": "100"}, path="/2/tweets/search/recent",
                lag=30, now=10 ** 9, max_age=7 * 24 * 60 * 60))
        self.assertEqual("clean", result["outcome"])
        self.assertTrue(result["complete"])
        self.assertEqual(f"{10 ** 9 - 30:.6f}", result["advance_to"])
        self.assertIn("unrecoverable history", result["reason"])
        # The query itself starts at the retention floor, never before it.
        self.assertEqual("1970-01-01T00:00:00Z", self.x._iso(0)[:20])
        self.assertGreaterEqual(self.x._epoch(call.call_args.args[1]["start_time"]),
                                10 ** 9 - 7 * 24 * 60 * 60)

    def test_x_leaves_a_healthy_watermark_untouched(self):
        """The clamp must not perturb the ordinary path: no reason, no moved start_time."""
        with mock.patch.object(self.x, "_call", return_value={"data": [], "meta": {}}) as call:
            result = self.emitted(lambda: self.x.cli(
                {"last_checked_ts": str(10 ** 9 - 3600)}, path="/2/tweets/search/recent",
                lag=30, now=10 ** 9, max_age=7 * 24 * 60 * 60))
        self.assertEqual("clean", result["outcome"])
        self.assertNotIn("reason", result)
        self.assertEqual(10 ** 9 - 3600, self.x._epoch(call.call_args.args[1]["start_time"]))


class CredentialStoreTest(unittest.TestCase):
    def test_namespaced_bundle_round_trip(self):
        module = load("credential_store_test", SURFACES / "credential_store.py")

        class FakeKeychain:
            def __init__(self):
                self.values = {}

            def read(self, service, account):
                return self.values.get((service, account))

            def write(self, service, account, value):
                self.values[(service, account)] = value

        keychain = FakeKeychain()
        store = module.CredentialStore("test-service", "work", keychain)
        store.save({"secret": "value"})
        self.assertEqual({"secret": "value"}, store.load())
        self.assertEqual({("test-service", "yaas:work")}, set(keychain.values))

    def test_credential_id_rejects_keychain_namespace_injection(self):
        module = load("credential_store_invalid_test", SURFACES / "credential_store.py")
        with self.assertRaises(Exception):
            module.account_name("../../other")

    def test_telegram_authorize_prompts_for_phone_instead_of_accepting_argv(self):
        module = load("telegram_credentials_prompt_test", SURFACES / "telegram_credentials.py")
        authorized = mock.AsyncMock(return_value={
            "credential_id": "work", "user_id": "42", "username": "tester",
        })
        output = io.StringIO()
        with mock.patch.object(module.getpass, "getpass", side_effect=["+6591234567", "hash"]), \
                mock.patch.object(module, "_authorize", authorized), \
                contextlib.redirect_stdout(output):
            code = module.main(["authorize", "12345", "work"])
        self.assertEqual(0, code)
        authorized.assert_awaited_once_with("12345", "+6591234567", "work", "hash")
        self.assertNotIn("+6591234567", output.getvalue())


class SurfaceCredentialClassificationTest(unittest.TestCase):
    """A locked Keychain is transient machine state, not a bad credential.

    TransientCredentialError subclasses CredentialError, so catching only the base class
    returned AUTH (exit 1), which both checkers map to Misconfig -- parking every Telegram
    and X watch for a human over something that clears on the next unlock.
    """

    TRANSIENT = 4
    AUTH = 1

    def _assert_split(self, module, patched, call):
        # Both classes are read off the surface module, so they are the same objects its
        # except-clauses test against.
        with mock.patch.object(module, patched, side_effect=(
                module.TransientCredentialError("macOS Keychain is locked"))):
            self.assertEqual(self.TRANSIENT, call(module))
        with mock.patch.object(module, patched, side_effect=(
                module.CredentialError("credential is missing"))):
            self.assertEqual(self.AUTH, call(module))

    def test_telegram_surface_separates_locked_keychain_from_bad_credentials(self):
        if importlib.util.find_spec("telethon") is None:
            self.skipTest("Telethon optional extra is not installed")
        params = json.dumps({"peer": "@chat", "after_ts": 1, "before_ts": 2})
        self._assert_split(
            load("telegram_call_classify_test", SURFACES / "telegram-call.py"),
            "load_bundle", lambda module: module.main([params]))

    def test_x_surface_separates_locked_keychain_from_bad_credentials(self):
        argv = ["GET", "/2/tweets/search/recent", "{}", "app:default"]
        self._assert_split(
            load("x_call_classify_test", SURFACES / "x-call.py"),
            "get_access_token", lambda module: module.main(argv))


@unittest.skipUnless(importlib.util.find_spec("telethon"),
                     "Telethon optional extra is not installed")
class TelegramPeerCacheTest(unittest.TestCase):
    """A numeric peer has no username, so resolving it means enumerating every dialog.
    Paying that per watch per tick is the shape most likely to earn a FloodWait."""

    def setUp(self):
        self.module = load("telegram_call_cache_test", SURFACES / "telegram-call.py")
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-telegram-peer-")
        root = Path(self.temp.name)
        (root / "state").mkdir()
        self.patch = mock.patch.object(
            self.module, "_cache_file", lambda: root / "state" / "telegram-peers.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    class FakeClient:
        """Counts dialog enumerations; that count is the whole point of the cache."""

        def __init__(self, entity):
            self.entity = entity
            self.scans = 0

        async def iter_dialogs(self):
            self.scans += 1
            yield type("Dialog", (), {"entity": self.entity})()

        async def get_entity(self, value):  # pragma: no cover - username path
            raise AssertionError(f"numeric peer must not resolve by name: {value!r}")

    def _channel(self):
        from telethon.tl.types import Channel
        return Channel(id=1234567890, title="t", photo=None, date=None,
                       creator=True, access_hash=987654321)

    def test_second_resolution_of_a_numeric_peer_skips_the_dialog_scan(self):
        from telethon import utils
        from telethon.tl.types import InputPeerChannel

        entity = self._channel()
        marked = str(utils.get_peer_id(entity))
        client = self.FakeClient(entity)

        first = asyncio.run(self.module._resolve_peer(client, marked, "work"))
        self.assertIs(entity, first)
        self.assertEqual(1, client.scans)

        second = asyncio.run(self.module._resolve_peer(client, marked, "work"))
        self.assertEqual(1, client.scans, "cache hit must not re-enumerate dialogs")
        self.assertEqual(InputPeerChannel(channel_id=entity.id,
                                          access_hash=entity.access_hash), second)
        self.assertEqual(0o600, self.module._cache_file().stat().st_mode & 0o777)

    def test_cache_is_scoped_per_credential(self):
        from telethon import utils

        entity = self._channel()
        marked = str(utils.get_peer_id(entity))
        client = self.FakeClient(entity)
        asyncio.run(self.module._resolve_peer(client, marked, "work"))
        asyncio.run(self.module._resolve_peer(client, marked, "personal"))
        self.assertEqual(2, client.scans, "a different credential sees different dialogs")

    def test_a_corrupt_cache_falls_back_to_the_dialog_scan(self):
        from telethon import utils

        entity = self._channel()
        marked = str(utils.get_peer_id(entity))
        self.module._cache_file().write_text("{ not json")
        client = self.FakeClient(entity)
        self.assertIs(entity, asyncio.run(self.module._resolve_peer(client, marked, "work")))
        self.assertEqual(1, client.scans)

    def test_an_unwritable_cache_never_fails_the_poll(self):
        from telethon import utils

        entity = self._channel()
        marked = str(utils.get_peer_id(entity))
        client = self.FakeClient(entity)
        with mock.patch.object(self.module.os, "replace", side_effect=OSError("read-only")):
            self.assertIs(entity, asyncio.run(
                self.module._resolve_peer(client, marked, "work")))
            self.assertIs(entity, asyncio.run(
                self.module._resolve_peer(client, marked, "work")))
        self.assertEqual(2, client.scans, "an unwritable cache degrades, it does not break")


class OperatorScriptValidationTest(unittest.TestCase):
    """doctor.sh and setup.sh must reject a connector list that makes tick.py exit 2.

    Reporting it green sent the operator looking anywhere but at the actual cause.
    """

    SCRIPTS = {
        "doctor.sh": ROOT / "src/sidequestor/runtime/yaas-triage/ops/doctor.sh",
        "setup.sh": ROOT / "src/sidequestor/runtime/yaas-triage/setup/setup.sh",
    }

    def test_both_scripts_validate_through_the_runtime_loader(self):
        for name, path in self.SCRIPTS.items():
            body = path.read_text()
            self.assertIn("load_checker_connectors", body, name)
            self.assertNotIn('  ok "SIDEQUESTOR_CHECKER_CONNECTORS=$CHECKER_CONNECTORS"\n'
                             "  case", body, name)

    def test_each_script_acts_on_the_verdict_rather_than_only_printing_it(self):
        self.assertIn('fail "$CONNECTOR_ERROR', self.SCRIPTS["doctor.sh"].read_text())
        setup = self.SCRIPTS["setup.sh"].read_text()
        self.assertIn('echo "ERROR: $CONNECTOR_ERROR" >&2', setup)
        self.assertIn('ERROR: could not validate SIDEQUESTOR_CHECKER_CONNECTORS=', setup)

    def test_both_scripts_still_parse(self):
        for name, path in self.SCRIPTS.items():
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, f"{name}: {proc.stderr}")


class RegistrationTest(unittest.TestCase):
    def test_new_checkers_are_executable_and_manifested(self):
        expected = {"telegram_chat", "telegram_search", "x_search"}
        for name in expected:
            self.assertTrue(os.access(CHECKERS / f"{name}.py", os.X_OK), name)
            self.assertTrue((CHECKERS / f"{name}.watch.json").is_file(), name)
        self.assertEqual("2", (CHECKERS / "telegram_chat.lag").read_text().strip())

    def test_every_manifest_has_a_checker(self):
        for manifest in CHECKERS.glob("*.watch.json"):
            self.assertTrue(manifest.with_name(manifest.name.removesuffix(".watch.json") + ".py").is_file(),
                            manifest.name)


if __name__ == "__main__":
    unittest.main()
