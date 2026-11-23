"""A schedule bookmarks the boundary it checked, not the earlier tick start."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "src/sidequestor/runtime/yaas-triage"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / "checkers"))
import tick


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schedule = load("schedule_boundary_checker", RUNTIME / "checkers/schedule.py")
commit = load("schedule_boundary_commit", RUNTIME / "ledger/commit.py")


class ScheduleBoundaryTests(unittest.TestCase):
    # 2026-11-06 08:30 UTC, with the tick starting before the scheduled slot.
    slot = 1793953800

    def check(self, entry, now):
        output = io.StringIO()
        with patch.object(schedule.time, "time", return_value=now), \
                patch.object(sys, "argv", ["schedule.py", json.dumps(entry)]), \
                contextlib.redirect_stdout(output):
            schedule.main()
        return json.loads(output.getvalue())

    def test_acknowledged_slot_does_not_repeat_after_tick_crosses_minute(self):
        entry = {"watch_id": "clock", "type": "schedule", "cron": "30 8 * * *",
                 "tz": "UTC", "last_checked_ts": str(self.slot - 1800)}
        first = self.check(entry, self.slot + 5)
        self.assertEqual(first["outcome"], "dirty")
        with tempfile.TemporaryDirectory() as raw:
            quest = Path(raw) / "quest"; quest.mkdir()
            path = quest / "watch.json"
            path.write_text(json.dumps({"watches": [entry]}))
            t = SimpleNamespace(quests_dir=Path(raw), now_ts=self.slot - 28, lag_map={},
                                _read_json=lambda p, default: json.loads(p.read_text()), log=lambda _: None)
            dirty = [{**first, "quest_id": "quest", "watch_id": "clock", "type": "schedule"}]
            held = commit.decide({"quest_id": "quest", "acked": [], "dirty_watches": dirty})
            tick.advance_watches(t, "quest", held["moves"])
            unchanged = json.loads(path.read_text())["watches"][0]
            self.assertEqual(unchanged, entry)
            self.assertEqual(self.check(unchanged, self.slot + 109)["outcome"], "dirty")
            decision = commit.decide({"quest_id": "quest", "acked": ["clock"], "dirty_watches": dirty})
            tick.advance_watches(t, "quest", decision["moves"])
            updated = json.loads(path.read_text())["watches"][0]
            self.assertEqual(float(updated["last_checked_ts"]), self.slot + 5)
            self.assertEqual(self.check(updated, self.slot + 109)["outcome"], "clean")

    def test_next_slot_that_arrives_during_worker_run_is_not_skipped(self):
        entry = {"type": "schedule", "cron": "* * * * *", "tz": "UTC",
                 "last_checked_ts": str(self.slot - 60)}
        first = self.check(entry, self.slot + 5)
        entry["last_checked_ts"] = first["advance_to"]
        self.assertEqual(self.check(entry, self.slot + 65)["outcome"], "dirty")

    def test_one_shot_uses_checked_boundary_and_remains_one_shot(self):
        entry = {"type": "schedule", "next_fire_ts": self.slot,
                 "last_checked_ts": str(self.slot - 60)}
        self.assertEqual(self.check(entry, self.slot - 1)["outcome"], "clean")
        first = self.check(entry, self.slot + 5)
        self.assertEqual(first["outcome"], "dirty")
        entry["last_checked_ts"] = first["advance_to"]
        self.assertEqual(self.check(entry, self.slot + 109)["outcome"], "clean")

    def test_exact_slot_clean_future_slot_and_timezone(self):
        entry = {"type": "schedule", "cron": "30 16 * * *", "tz": "Asia/Singapore",
                 "last_checked_ts": str(self.slot - 60)}
        self.assertEqual(self.check(entry, self.slot - .1)["outcome"], "clean")
        self.assertEqual(self.check(entry, self.slot)["outcome"], "dirty")
        entry["last_checked_ts"] = str(self.slot)
        self.assertEqual(self.check(entry, self.slot)["outcome"], "clean")

    def test_sub_microsecond_rounding_cannot_fire_a_slot_before_its_bookmark(self):
        import math
        just_before = math.nextafter(float(self.slot), -math.inf)
        entry = {"type": "schedule", "cron": "30 8 * * *", "tz": "UTC",
                 "last_checked_ts": str(self.slot - 60)}
        before = self.check(entry, just_before)
        self.assertEqual(before["outcome"], "clean")
        self.assertLess(float(before["advance_to"]), self.slot)
        entry["last_checked_ts"] = before["advance_to"]
        due = self.check(entry, self.slot)
        self.assertEqual(due["outcome"], "dirty")
        entry["last_checked_ts"] = due["advance_to"]
        self.assertEqual(self.check(entry, self.slot + 1)["outcome"], "clean")

    def test_legacy_cron_cli_output_and_never_run_behavior_are_preserved(self):
        cp = subprocess.run([sys.executable, str(RUNTIME / "checkers/cron-due.py"),
                             "* * * * *", "UTC", ""], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "not-due")
        entry = {"type": "schedule", "cron": "invalid", "tz": "UTC", "last_checked_ts": "1"}
        bad = self.check(entry, self.slot)
        self.assertEqual(bad["outcome"], "misconfig")
        self.assertNotIn("advance_to", bad)

    def test_failed_or_unrecognized_evaluation_cannot_advance_past_a_slot(self):
        for code, output in ((1, ""), (0, "unexpected")):
            with self.subTest(code=code, output=output), \
                    patch.object(schedule.subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "failure")):
                verdict = self.check({"type": "schedule", "cron": "* * * * *", "tz": "UTC", "last_checked_ts": "1"}, self.slot)
                self.assertEqual(verdict["outcome"], "error")
                self.assertNotIn("advance_to", verdict)
