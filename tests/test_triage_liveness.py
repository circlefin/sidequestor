from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


TRIAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sidequestor" / "runtime" / "yaas-triage"
RUNTIME_ROOT = TRIAGE_ROOT.parent


class TriageLivenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-triage-liveness-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "state" / "triage").mkdir(parents=True)
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        sys.path.insert(0, str(TRIAGE_ROOT))
        self.addCleanup(lambda: sys.path.remove(str(TRIAGE_ROOT)))
        spec = importlib.util.spec_from_file_location(
            "sidequestor_health_liveness_test", TRIAGE_ROOT / "ops" / "health-monitor.py")
        self.monitor = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.dict(os.environ, {"YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT)}):
            spec.loader.exec_module(self.monitor)
        self.clock = patch.object(self.monitor, "_now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def stamp(self, minutes=0, seconds=0) -> str:
        return (self.now - timedelta(minutes=minutes, seconds=seconds)).isoformat()

    def write_state(self, tick_minutes=20, completed_minutes=30, worker=None) -> None:
        triage = self.workspace / "state" / "triage"
        (triage / "last-run.json").write_text(json.dumps({
            "tick_started_utc": self.stamp(tick_minutes),
            "last_triage_completed_utc": self.stamp(completed_minutes),
        }))
        if worker is not None:
            (triage / "worker-current.json").write_text(json.dumps(worker))

    def worker(self, started_minutes=15, heartbeat_seconds=15, **changes) -> dict:
        record = {"schema": 1, "state": "running", "run_ref": "run-1",
                  "targets": ["quest-one"], "started_at": self.stamp(started_minutes),
                  "heartbeat_at": self.stamp(seconds=heartbeat_seconds),
                  "timeout_s": 1800}
        record.update(changes)
        return record

    def problems(self) -> list[dict]:
        health = self.monitor.Health(self.workspace)
        health.check_triage_liveness()
        return health.problems

    def test_fresh_worker_suppresses_completion_age_alert(self) -> None:
        self.write_state(worker=self.worker())
        self.assertEqual(self.problems(), [])

    def test_recent_exit_between_workers_is_still_in_progress(self) -> None:
        self.write_state(worker=self.worker(state="exited", ended_at=self.stamp(2)))
        self.assertEqual(self.problems(), [])

    def test_stale_heartbeat_raises_specific_worker_alert(self) -> None:
        self.write_state(worker=self.worker(heartbeat_seconds=90))
        problems = self.problems()
        self.assertEqual([p["key"] for p in problems], ["worker_unhealthy"])
        self.assertEqual(problems[0]["signature"], "run-1:stale_heartbeat")

    def test_worker_past_its_timeout_raises_specific_alert(self) -> None:
        self.write_state(tick_minutes=40, completed_minutes=50,
                         worker=self.worker(started_minutes=34))
        problems = self.problems()
        self.assertEqual([p["key"] for p in problems], ["worker_unhealthy"])
        self.assertEqual(problems[0]["signature"], "run-1:overdue")

    def test_orphan_worker_cannot_hide_stalled_triage(self) -> None:
        self.write_state(worker=self.worker(started_minutes=25))
        problems = self.problems()
        self.assertIn("triage_stalled", [p["key"] for p in problems])
        stalled = next(p for p in problems if p["key"] == "triage_stalled")
        self.assertEqual(stalled["signature"], f"since:{self.stamp(30)}")
        self.assertFalse(self.monitor._should_notify({"triage_stalled": {
            "signature": stalled["signature"], "at": self.stamp(5)}}, stalled))

    def test_hard_cap_applies_even_with_fresh_sequential_worker(self) -> None:
        self.write_state(tick_minutes=110, completed_minutes=120,
                         worker=self.worker(started_minutes=2))
        problems = self.problems()
        self.assertEqual([p["key"] for p in problems], ["tick_hung"])
        self.assertEqual(problems[0]["signature"], f"tick:{self.stamp(110)}")

    def test_tick_hung_does_not_flicker_between_worker_states(self) -> None:
        self.write_state(tick_minutes=80, completed_minutes=90)
        self.assertEqual([p["key"] for p in self.problems()], ["triage_stalled"])
        self.write_state(tick_minutes=80, completed_minutes=90,
                         worker=self.worker(started_minutes=2))
        self.assertEqual(self.problems(), [])

    def test_configured_heartbeat_interval_and_recorded_timeout(self) -> None:
        self.monitor.configure({"YAAS_WORKER_HEARTBEAT_SECONDS": "60"})
        self.write_state(tick_minutes=40, completed_minutes=50,
                         worker=self.worker(started_minutes=34, heartbeat_seconds=90,
                                            timeout_s=2400))
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.monitor.WORKER_HEARTBEAT_GRACE_S, 135)

    def test_naive_timestamps_are_interpreted_as_utc(self) -> None:
        self.assertEqual(self.monitor._parse("2026-09-30T12:00:00").tzinfo, timezone.utc)

    def test_notification_history_survives_a_healthy_sample(self) -> None:
        self.write_state()
        alerts_path = self.workspace / "state" / "triage" / "health-alerts.json"
        with patch.object(sys, "argv", ["health-monitor.py", "--repo",
                                             str(self.workspace), "--notify"]), \
                patch.object(self.monitor, "_notify") as notify:
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)
            self.write_state(worker=self.worker())
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)
            (self.workspace / "state" / "triage" / "worker-current.json").unlink()
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)
        self.assertIn("triage_stalled", json.loads(alerts_path.read_text()))

    def test_worker_notification_requires_a_second_sample(self) -> None:
        self.write_state(worker=self.worker(heartbeat_seconds=90))
        alerts_path = self.workspace / "state" / "triage" / "health-alerts.json"
        with patch.object(sys, "argv", ["health-monitor.py", "--repo",
                                             str(self.workspace), "--notify"]), \
                patch.object(self.monitor, "_notify") as notify:
            self.monitor.main()
            self.assertEqual(notify.call_count, 0)
            alerts = json.loads(alerts_path.read_text())
            self.assertIn("pending_since", alerts["worker_unhealthy"])
            alerts["worker_unhealthy"]["pending_since"] = self.stamp(2)
            alerts_path.write_text(json.dumps(alerts))
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)

    def test_recovered_heartbeat_clears_pending_notification(self) -> None:
        self.write_state(worker=self.worker(heartbeat_seconds=90))
        alerts_path = self.workspace / "state" / "triage" / "health-alerts.json"
        with patch.object(sys, "argv", ["health-monitor.py", "--repo",
                                             str(self.workspace), "--notify"]), \
                patch.object(self.monitor, "_notify") as notify:
            self.monitor.main()
            self.write_state(worker=self.worker())
            self.monitor.main()
            self.assertNotIn("worker_unhealthy", json.loads(alerts_path.read_text()))
            self.write_state(worker=self.worker(heartbeat_seconds=90))
            self.monitor.main()
            self.assertEqual(notify.call_count, 0)
            self.assertIn("pending_since", json.loads(alerts_path.read_text())["worker_unhealthy"])

    def test_long_gap_after_worker_exit_needs_confirmation(self) -> None:
        self.write_state(worker=self.worker(state="exited", ended_at=self.stamp(7)))
        alerts_path = self.workspace / "state" / "triage" / "health-alerts.json"
        with patch.object(sys, "argv", ["health-monitor.py", "--repo",
                                             str(self.workspace), "--notify"]), \
                patch.object(self.monitor, "_notify") as notify:
            self.monitor.main()
            self.assertEqual(notify.call_count, 0)
            alerts = json.loads(alerts_path.read_text())
            self.assertIn("pending_since", alerts["triage_stalled"])
            alerts["triage_stalled"]["pending_since"] = self.stamp(2)
            alerts_path.write_text(json.dumps(alerts))
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)

    def test_changed_worker_problem_keeps_previous_cooldown(self) -> None:
        self.write_state(worker=self.worker(heartbeat_seconds=90))
        alerts_path = self.workspace / "state" / "triage" / "health-alerts.json"
        with patch.object(sys, "argv", ["health-monitor.py", "--repo",
                                             str(self.workspace), "--notify"]), \
                patch.object(self.monitor, "_notify") as notify:
            self.monitor.main()
            alerts = json.loads(alerts_path.read_text())
            alerts["worker_unhealthy"]["pending_since"] = self.stamp(2)
            alerts_path.write_text(json.dumps(alerts))
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)
            self.write_state(tick_minutes=40, completed_minutes=50,
                             worker=self.worker(started_minutes=34))
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)
            alerts = json.loads(alerts_path.read_text())
            self.assertEqual(alerts["worker_unhealthy"]["previous"]["signature"],
                             "run-1:stale_heartbeat")
            self.write_state(worker=self.worker(heartbeat_seconds=90))
            self.monitor.main()
            self.assertEqual(notify.call_count, 1)
            alerts = json.loads(alerts_path.read_text())
            self.assertEqual(alerts["worker_unhealthy"]["signature"],
                             "run-1:stale_heartbeat")
            self.assertNotIn("pending_since", alerts["worker_unhealthy"])

    def test_dashboard_marks_fresh_but_overdue_worker(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "sidequestor_dashboard_liveness_test", TRIAGE_ROOT / "ops" / "dashboard-server.py")
        dashboard = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.dict(os.environ, {"YAAS_WORKSPACE": str(self.workspace),
                                  "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT)}), \
                patch.object(sys, "argv", ["dashboard-server.py"]):
            spec.loader.exec_module(dashboard)
        path = self.workspace / "state" / "triage" / "worker-current.json"
        path.write_text(json.dumps(self.worker(started_minutes=34, log="worker.log")))
        with patch.object(dashboard, "_heartbeat_age", side_effect=lambda value:
                          15 if value == self.stamp(seconds=15) else 34 * 60):
            live = dashboard.build_live_run()
        self.assertTrue(live["overdue"])
        self.assertEqual(live["state"], "overdue")
        self.assertEqual(live["timeout_s"], 1800)

    def test_dashboard_respects_configured_heartbeat_interval(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "sidequestor_dashboard_heartbeat_test", TRIAGE_ROOT / "ops" / "dashboard-server.py")
        dashboard = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.dict(os.environ, {"YAAS_WORKSPACE": str(self.workspace),
                                  "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT)}), \
                patch.object(sys, "argv", ["dashboard-server.py"]):
            spec.loader.exec_module(dashboard)
        path = self.workspace / "state" / "triage" / "worker-current.json"
        path.write_text(json.dumps(self.worker(started_minutes=34, heartbeat_seconds=90,
                                              timeout_s=2400, log="worker.log")))
        with patch.object(dashboard, "_heartbeat_age", side_effect=lambda value:
                          90 if value == self.stamp(seconds=90) else 34 * 60), \
                patch.object(dashboard, "_dotenv", side_effect=lambda key, default="":
                             "60" if key == "YAAS_WORKER_HEARTBEAT_SECONDS" else
                             "12" if key == "YAAS_HEALTH_STALL_MIN" else default):
            live = dashboard.build_live_run()
            self.assertEqual(dashboard._hard_tick_ceiling_s(), 6300)
            self.assertEqual(dashboard._stall_threshold_s(), 720)
        self.assertTrue(live["running"])
        self.assertFalse(live["stale"])
        self.assertFalse(live["overdue"])


if __name__ == "__main__":
    unittest.main()
