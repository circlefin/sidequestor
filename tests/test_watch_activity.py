from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
RUNTIME = PACKAGE / "src" / "sidequestor" / "runtime" / "yaas-triage"


def load_module(name: str, relative: str):
    path = RUNTIME / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


housekeep = load_module("sidequestor_test_housekeep", "ledger/housekeep.py")
tick = load_module("sidequestor_test_tick_activity", "tick.py")


class RetirementActivityTest(unittest.TestCase):
    def test_existing_watch_without_optional_fields_keeps_legacy_behavior(self) -> None:
        watch = {"type": "slack_thread", "thread_ts": "100.000000"}
        self.assertTrue(housekeep.retire_thread(watch, 200.0))

    def test_creation_or_new_activity_extends_an_old_thread(self) -> None:
        created = {"type": "slack_thread", "thread_ts": "100", "created_ts": "300"}
        active = {"type": "slack_thread", "thread_ts": "100", "last_activity_ts": "400"}
        self.assertFalse(housekeep.retire_thread(created, 200.0))
        self.assertFalse(housekeep.retire_thread(active, 200.0))

    def test_malformed_activity_is_ignored(self) -> None:
        watch = {
            "type": "slack_thread",
            "thread_ts": "100",
            "created_ts": "not-an-epoch",
            "last_activity_ts": "nan",
        }
        self.assertTrue(housekeep.retire_thread(watch, 200.0))


class CheckerActivityPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-activity-")
        self.quests = Path(self.temp.name)
        self.quest = self.quests / "quest-one"
        self.quest.mkdir()
        self.watch_path = self.quest / "watch.json"
        self.watch_path.write_text(json.dumps({"watches": [{
            "watch_id": "watch-a1",
            "type": "slack_thread",
            "thread_ts": "100.000000",
        }]}) + "\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _triage(self, advance_to: str):
        class Triage:
            pass

        triage = Triage()
        triage.quests_dir = self.quests
        triage.now_ts = time.time()
        triage.dirty_watches = [{
            "quest_id": "quest-one",
            "watch_id": "watch-a1",
            "type": "slack_thread",
            "advance_to": advance_to,
        }]
        triage._read_json = lambda path, default: json.loads(path.read_text())
        triage.log = lambda message: None
        return triage

    def test_dirty_checker_activity_is_monotonic(self) -> None:
        self.assertEqual(tick.record_thread_activity(self._triage("300.1234567")), set())
        first = json.loads(self.watch_path.read_text())["watches"][0]
        self.assertEqual(first["last_activity_ts"], "300.123456")

        self.assertEqual(tick.record_thread_activity(self._triage("200.000000")), set())
        second = json.loads(self.watch_path.read_text())["watches"][0]
        self.assertEqual(second["last_activity_ts"], "300.123456")

    def test_failed_activity_write_holds_housekeeping_for_that_quest(self) -> None:
        self.watch_path.write_text("not json")
        self.assertEqual(tick.record_thread_activity(self._triage("300")), {"quest-one"})


class AdoptionRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-adopt-")
        self.workspace = Path(self.temp.name)
        quest = self.workspace / "state" / "quests" / "active" / "quest-adopt"
        quest.mkdir(parents=True)
        (quest / "watch.json").write_text('{"watches": []}\n')
        (quest / "timeline.ndjson").touch()
        self.watch_path = quest / "watch.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _add(self, payload: dict) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["SIDEQUESTOR_WORKSPACE"] = str(self.workspace)
        env["YAAS_RUNTIME_ROOT"] = str(RUNTIME)
        return subprocess.run(
            [sys.executable, str(RUNTIME / "ledger" / "add-watch.py"),
             "quest-adopt", json.dumps(payload)],
            text=True,
            capture_output=True,
            env=env,
        )

    def test_adoption_refreshes_duplicate_without_adding_one(self) -> None:
        base = {
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "100.000000",
            "created_ts": "200.000000",
            "reason": "existing old thread",
        }
        first = self._add(base)
        self.assertEqual(first.returncode, 0, first.stderr)

        refreshed = self._add(dict(base, refresh_activity=True))
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertTrue(refreshed.stdout.startswith("skip:duplicate:"))
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(len(watches), 1)
        self.assertGreater(float(watches[0]["last_activity_ts"]), time.time() - 10)

    def test_ordinary_duplicate_does_not_refresh_activity(self) -> None:
        base = {
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "100.000000",
            "created_ts": "200.000000",
            "last_activity_ts": "250.000000",
            "reason": "existing thread",
        }
        self.assertEqual(self._add(base).returncode, 0)
        self.assertEqual(self._add(base).returncode, 0)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(watches[0]["last_activity_ts"], "250.000000")


if __name__ == "__main__":
    unittest.main()
