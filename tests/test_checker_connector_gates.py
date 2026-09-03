from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"


def load_tick():
    sys.path.insert(0, str(TRIAGE))
    try:
        spec = importlib.util.spec_from_file_location("connector_gate_tick", TRIAGE / "tick.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class FakeConfig:
    def __init__(self, enabled):
        self.enabled = set(enabled)

    def checker_enabled(self, watch_type):
        return watch_type in self.enabled


class FakeTick:
    def __init__(self, root, enabled, outputs):
        self.quests_dir = root / "quests"
        self.quests_dir.mkdir()
        self.unacked_file = root / "unacked.json"
        self.script_dir = TRIAGE
        self.cfg = FakeConfig(enabled)
        self.slack_checkers_enabled = "slack_thread" in enabled
        self.checker_health_json = {}
        self.now_ts = 1000.0
        self.unacked_promote = 3
        self.error_promote = 6
        self.outputs = iter(outputs)
        self.calls = []

    @staticmethod
    def _read_json(path, default=None):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return default

    def py(self, *args):
        return ["test-python", *map(str, args)]

    def helper(self, *parts):
        return str(self.script_dir.joinpath(*parts))

    def run(self, argv, **_kwargs):
        self.calls.append(argv)
        output = next(self.outputs)
        return type("Result", (), {"stdout": json.dumps(output), "stderr": "", "returncode": 0})()

    def log(self, _message):
        pass


class ConnectorGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tick = load_tick()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-connector-gate-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def quest(self, tick, watches):
        quest = tick.quests_dir / "quest-test"
        quest.mkdir()
        (quest / "watch.json").write_text(json.dumps({"watches": watches}))

    def test_disabled_connector_is_not_executed(self):
        tick = FakeTick(
            self.root,
            {"schedule"},
            [{"outcome": "clean", "count": 0, "complete": True, "advance_to": "900"}],
        )
        self.quest(tick, [
            {"watch_id": "watch-a1", "type": "telegram_chat", "peer": "@test"},
            {"watch_id": "watch-b2", "type": "schedule", "cron": "0 0 1 1 *", "tz": "UTC"},
        ])

        rows = self.tick.check_quest(tick, "quest-test")

        self.assertEqual([row["type"] for row in rows if "type" in row], ["schedule"])
        self.assertEqual(len(tick.calls), 1)

    def test_non_slack_transient_does_not_stop_later_watch(self):
        tick = FakeTick(
            self.root,
            {"telegram_chat", "schedule"},
            [
                {"outcome": "ratelimited", "complete": False, "reason": "timeout"},
                {"outcome": "clean", "count": 0, "complete": True, "advance_to": "900"},
            ],
        )
        self.quest(tick, [
            {"watch_id": "watch-a1", "type": "telegram_chat", "peer": "@test"},
            {"watch_id": "watch-b2", "type": "schedule", "cron": "0 0 1 1 *", "tz": "UTC"},
        ])

        rows = self.tick.check_quest(tick, "quest-test")

        self.assertEqual([row["status"] for row in rows], ["skip", "ok"])
        self.assertEqual(len(tick.calls), 2)

    def test_slack_transient_stops_additional_slack_calls(self):
        tick = FakeTick(
            self.root,
            {"slack_thread", "slack_dm"},
            [{"outcome": "ratelimited", "complete": False, "reason": "HTTP 429"}],
        )
        self.quest(tick, [
            {"watch_id": "watch-a1", "type": "slack_thread", "channel_id": "C1", "thread_ts": "1"},
            {"watch_id": "watch-b2", "type": "slack_dm", "channel_id": "D1"},
        ])

        rows = self.tick.check_quest(tick, "quest-test")

        self.assertEqual([row["status"] for row in rows], ["skip"])
        self.assertEqual(len(tick.calls), 1)

    def test_tick_helpers_use_configured_package_interpreter(self):
        fake = type("Tick", (), {"env": {"SIDEQUESTOR_PYTHON": "/test/venv/bin/python"}})()
        self.assertEqual(
            self.tick.Tick.py(fake, "/tmp/checker.py", "{}"),
            ["/test/venv/bin/python", "/tmp/checker.py", "{}"],
        )


if __name__ == "__main__":
    unittest.main()
