"""The daily release check: quiet on failure, once per version, never stale after upgrade."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from sidequestor import cli
from sidequestor import update_check as uc
from sidequestor.workspace import Workspace

TRIAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sidequestor" / "runtime" / "yaas-triage"


class VersionCompareTest(unittest.TestCase):
    def test_only_plain_releases_compare(self):
        self.assertTrue(uc.is_newer("0.1.52", "0.1.51"))
        self.assertTrue(uc.is_newer("0.2", "0.1.99"))
        self.assertFalse(uc.is_newer("0.1.51", "0.1.51"))
        self.assertFalse(uc.is_newer("0.1.51.0", "0.1.51"))
        self.assertFalse(uc.is_newer("0.1.50", "0.1.51"))
        self.assertFalse(uc.is_newer("0.2.0rc1", "0.1.51"))
        self.assertFalse(uc.is_newer("0.1.52", "0.1.52.dev3+gabc"))
        self.assertFalse(uc.is_newer(None, "0.1.51"))
        self.assertFalse(uc.is_newer("9" * 5000, "0.1.51"))


class FetchLatestTest(unittest.TestCase):
    def test_fetch_uses_native_trust_with_certificate_verification(self):
        with patch.object(uc.urllib.request, "urlopen") as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = (
                b'{"info": {"version": "0.2.3"}}')
            self.assertEqual(uc.fetch_latest(), "0.2.3")
        context = fetch.call_args.kwargs["context"]
        self.assertIsInstance(context, uc.truststore.SSLContext)
        self.assertEqual(context.verify_mode, uc.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(fetch.call_args.kwargs["timeout"], uc.FETCH_TIMEOUT_SEC)


class RunCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-update-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notified = []
        self.fetches = 0

    def _fetch(self, version="0.1.52", error=None):
        def fetch(url):
            self.fetches += 1
            if error:
                raise error
            return version
        return fetch

    def _run(self, now=1_000_000.0, installed="0.1.51", env=None, **kw):
        return uc.run_check(self.root, env or {}, now=now, installed=installed,
                            notify=self.notified.append, **kw)

    def test_new_release_is_recorded_and_notified_once(self):
        status = self._run(fetch=self._fetch())
        self.assertEqual(status["latest"], "0.1.52")
        self.assertIsNone(status["error"])
        self.assertEqual([n["latest"] for n in self.notified], ["0.1.52"])
        # Not due again for a day; once due, the same version does not notify twice.
        self.assertIsNone(self._run(now=1_000_000.0 + 3600, fetch=self._fetch()))
        self._run(now=1_000_000.0 + uc.CHECK_INTERVAL_SEC, fetch=self._fetch())
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.fetches, 2)
        self._run(now=1_000_000.0 + 2 * uc.CHECK_INTERVAL_SEC, fetch=self._fetch("0.1.53"))
        self.assertEqual([n["latest"] for n in self.notified], ["0.1.52", "0.1.53"])

    def test_failed_notification_is_retried_at_the_next_check(self):
        def refuse(update):
            raise OSError("osascript exited 1")
        status = uc.run_check(self.root, {}, now=1_000_000.0, installed="0.1.51",
                              fetch=self._fetch(), notify=refuse)
        self.assertNotIn("notified_version", status)
        self._run(now=1_000_000.0 + uc.CHECK_INTERVAL_SEC, fetch=self._fetch())
        self.assertEqual([n["latest"] for n in self.notified], ["0.1.52"])

    def test_up_to_date_does_not_notify(self):
        self._run(installed="0.1.52", fetch=self._fetch("0.1.52"))
        self.assertEqual(self.notified, [])

    def test_network_failure_is_quiet_keeps_last_known_and_retries_sooner(self):
        self._run(fetch=self._fetch("0.1.52"))
        later = 1_000_000.0 + uc.CHECK_INTERVAL_SEC
        status = self._run(now=later, fetch=self._fetch(error=OSError("proxy refused")))
        self.assertIn("proxy refused", status["error"])
        self.assertEqual(status["latest"], "0.1.52")
        self.assertEqual(status["next_check_ts"], later + uc.RETRY_AFTER_ERROR_SEC)

    def test_opt_out_never_fetches_or_writes(self):
        self.assertIsNone(self._run(env={"SIDEQUESTOR_UPDATE_CHECK": "0"},
                                    fetch=self._fetch()))
        self.assertEqual(self.fetches, 0)
        self.assertFalse(uc.status_path(self.root).exists())

    def test_finished_upgrade_clears_notice_without_a_new_check(self):
        self._run(fetch=self._fetch("0.1.52"))
        status = uc.read_status(self.root)
        self.assertIsNotNone(uc.available_update(status, "0.1.51"))
        self.assertIsNone(uc.available_update(status, "0.1.52"))

    def test_corrupt_status_file_is_replaced(self):
        path = uc.status_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text("{oops")
        self._run(fetch=self._fetch())
        self.assertEqual(json.loads(path.read_text())["latest"], "0.1.52")

    def test_manual_check_bypasses_daily_cache_but_is_throttled(self):
        self._run(fetch=self._fetch())
        self.assertIsNone(self._run(now=1_000_030, fetch=self._fetch("0.1.53"), force=True))
        status = self._run(now=1_000_061, fetch=self._fetch("0.1.53"), force=True)
        self.assertEqual(status["latest"], "0.1.53")
        self.assertEqual(self.fetches, 2)


class UpdateSurfacesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-update-surfaces-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = uc.status_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"latest": "99.0.0", "error": None}))

    def _dashboard(self):
        env = patch.dict(os.environ, {"YAAS_WORKSPACE": str(self.root),
                                      "YAAS_RUNTIME_ROOT": str(TRIAGE_ROOT.parent),
                                      "SIDEQUESTOR_VERSION": "0.1.51"})
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(TRIAGE_ROOT))
        self.addCleanup(lambda: sys.path.remove(str(TRIAGE_ROOT)))
        spec = importlib.util.spec_from_file_location(
            "sidequestor_dashboard_update_test", TRIAGE_ROOT / "ops" / "dashboard-server.py")
        server = importlib.util.module_from_spec(spec)
        with patch.object(sys, "argv", ["dashboard-server.py"]):
            spec.loader.exec_module(server)
        return server

    def test_dashboard_build_info_carries_cached_update(self):
        update = self._dashboard().build_build_info()["update"]
        from sidequestor import __version__
        self.assertEqual((update["installed"], update["latest"]), (__version__, "99.0.0"))

    def test_dashboard_package_identity_wins_over_stale_launch_environment(self):
        identity = {"version": "0.2.3", "commit_full": "", "ref": "",
                    "source": "unknown", "engine": "0.2.3"}
        with patch.dict(os.environ, {"SIDEQUESTOR_COMMIT": "old-commit",
                                    "SIDEQUESTOR_REF": "old-branch"}), \
                patch("sidequestor.build_info.build_info", return_value=identity):
            server = self._dashboard()
            info = server.build_build_info()
        self.assertEqual(info["version"], "0.2.3")
        self.assertEqual(info["update"]["installed"], "0.2.3")
        self.assertEqual(info["commit_full"], "")
        self.assertEqual(info["ref"], "")

    def test_dashboard_keeps_startup_identity_when_first_polled_after_upgrade(self):
        old = {"version": "0.2.2", "commit_full": "", "ref": "", "engine": "0.2.2"}
        with patch("sidequestor.build_info.build_info", return_value=old):
            server = self._dashboard()
        with patch("sidequestor.build_info.build_info", return_value={"version": "0.2.3"}):
            self.assertEqual(server.build_build_info()["version"], "0.2.2")

    def test_dashboard_recovery_action_targets_the_viewed_workspace(self):
        import shlex
        from sidequestor.workspace import init_workspace
        init_workspace(self.root)
        server = self._dashboard()
        with patch("sidequestor.worker_readiness.current_state", return_value={"slack": "Run sq setup."}):
            control = server.build_control()
        item = next(row for row in control["attention"] if row["id"] == "worker-readiness:slack")
        command = item["action"].removeprefix("Run ").removesuffix(" in your terminal.")
        self.assertEqual(shlex.split(command), ["sq", "--workspace", str(self.root.resolve()), "setup"])

    def test_dashboard_hides_update_when_check_is_turned_off(self):
        (self.root / ".env").write_text("SIDEQUESTOR_UPDATE_CHECK=0\n")
        self.assertIsNone(self._dashboard().build_build_info()["update"])

    def test_dashboard_still_shows_reviews_with_invalid_tick_configuration(self):
        (self.root / ".env").write_text("SIDEQUESTOR_TRIAGE_MAX_PARALLEL=broken\n")
        server = self._dashboard()
        (self.root / "state/pending-approvals.json").write_text(json.dumps({"items": [{
            "id": "config-review", "quest_id": "quest-inbox", "status": "pending_review",
            "action_type": "remote_request", "message_text": "Fix configuration", "target": {}}]}))
        self.assertIsNotNone(server.build_config()["error"])
        with patch.object(server, "_read_approvals", return_value=json.loads((self.root / "state/pending-approvals.json").read_text())):
            self.assertEqual(server.build_dashboard()["pending_review"][0]["id"], "config-review")

    def test_manual_update_endpoint_uses_background_check_and_honors_opt_out(self):
        server = self._dashboard()
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/updates/check"
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        handler._host_ok = lambda: True
        handler._authed = lambda: True
        handler._send_json = Mock()
        with patch.object(server.subprocess, "Popen") as launch:
            handler.do_POST()
        self.assertIn("--force", launch.call_args.args[0])
        handler._send_json.assert_called_with({"status": "checking", "last_attempt_ts": 0}, 202)
        (self.root / ".env").write_text("SIDEQUESTOR_UPDATE_CHECK=0\n")
        handler.rfile = io.BytesIO(b"{}")
        with patch.object(server.subprocess, "Popen") as launch:
            handler.do_POST()
        launch.assert_not_called()
        self.assertEqual(handler._send_json.call_args.args[1], 409)

    def test_manual_update_rate_limit_reports_cached_result_without_spawning(self):
        import time
        (self.root / 'state/update-status.json').write_text(json.dumps({'latest': '99.0', 'last_attempt_ts': time.time()}))
        server = self._dashboard()
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/updates/check'; handler.headers = {'Content-Length': '2'}
        handler.rfile = io.BytesIO(b'{}'); handler._host_ok = lambda: True; handler._authed = lambda: True
        handler._send_json = Mock()
        with patch.object(server.subprocess, 'Popen') as launch:
            handler.do_POST()
        launch.assert_not_called()
        result = handler._send_json.call_args.args[0]
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(result['update']['latest'], '99.0')

    def test_feedback_distinguishes_failure_and_current_without_raw_error(self):
        (self.root / 'state/update-status.json').write_text(json.dumps({'latest': '99.0', 'error': 'private proxy URL'}))
        result = uc.check_status(self.root)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private', json.dumps(result))
        (self.root / 'state/update-status.json').write_text(json.dumps({'latest': '0.0.1', 'error': None}))
        self.assertEqual(uc.check_status(self.root)['status'], 'current')

    def test_doctor_prints_cached_update(self):
        workspace = Workspace(self.root, "test-instance", "test")
        out = io.StringIO()
        with patch.object(cli, "validate_workspace", return_value=[]), \
                patch.object(cli, "production_agent_status", return_value=None), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli._cmd_doctor(workspace), 0)
        self.assertIn("update: 99.0.0 available (run 'sq upgrade')", out.getvalue())


if __name__ == "__main__":
    unittest.main()
