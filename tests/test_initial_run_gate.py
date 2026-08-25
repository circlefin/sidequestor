"""The bootstrap gate: a quest may not finish its first run watching nothing.

A quest created with `requires_initial_run` carries one placeholder one-shot `schedule`
watch whose only job is to wake the worker so it can install the real watches. Nothing
enforced that. On 2026-08-25 a worker read the fired schedule as an ordinary sweep, acked
`nothing_to_do`, the watermark advanced past `next_fire_ts`, and housekeep.retire_schedule()
deleted the spent one-shot — leaving an active quest with `watches: []` that could never
fire again and still rendered as healthy.

These tests pin the decision, not the plumbing: which shapes count as armed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"


def _load_tick():
    sys.path.insert(0, str(RUNTIME_ROOT))
    spec = importlib.util.spec_from_file_location("tick_under_test", RUNTIME_ROOT / "tick.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTick:
    """The two attributes _initial_run_incomplete() actually touches."""

    def __init__(self, quests_dir: Path):
        self.quests_dir = quests_dir

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return default


BOOTSTRAP = {"type": "schedule", "next_fire_ts": "1787618578.0", "reason": "initial run"}
REAL_WATCH = {"type": "email", "query": "from:someone@example.com", "reason": "the actual job"}
RECURRING = {"type": "schedule", "cron": "0 9 * * 1", "tz": "Asia/Singapore", "reason": "weekly"}


class InitialRunGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tick = _load_tick()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-gate-")
        self.quests = Path(self.temp.name)
        self.t = _FakeTick(self.quests)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _quest(self, meta: dict, watches: list) -> str:
        qid = "quest-under-test"
        folder = self.quests / qid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").write_text(json.dumps({"id": qid, **meta}))
        (folder / "watch.json").write_text(json.dumps({"watches": watches}))
        return qid

    def incomplete(self, meta, watches) -> bool:
        return self.tick._initial_run_incomplete(self.t, self._quest(meta, watches))

    def test_bootstrap_quest_left_with_only_its_placeholder_is_incomplete(self) -> None:
        self.assertTrue(self.incomplete(
            {"requires_initial_run": True, "status": "active"}, [BOOTSTRAP]))

    def test_bootstrap_quest_left_with_no_watches_at_all_is_incomplete(self) -> None:
        # The already-retired case: the watch is gone but the quest is still active.
        self.assertTrue(self.incomplete(
            {"requires_initial_run": True, "status": "active"}, []))

    def test_a_real_watch_arms_the_quest(self) -> None:
        self.assertFalse(self.incomplete(
            {"requires_initial_run": True, "status": "active"}, [BOOTSTRAP, REAL_WATCH]))

    def test_a_recurring_schedule_arms_the_quest(self) -> None:
        # A cron schedule is a real watch: housekeep never retires it, so the quest lives.
        self.assertFalse(self.incomplete(
            {"requires_initial_run": True, "status": "active"}, [BOOTSTRAP, RECURRING]))

    def test_completing_the_quest_is_a_legitimate_way_to_finish_with_no_watches(self) -> None:
        # "Do this once" is a real shape. It has to SAY so by leaving active, though,
        # rather than sitting active and empty — that is the distinction being drawn.
        self.assertFalse(self.incomplete(
            {"requires_initial_run": True, "status": "completed"}, []))

    def test_ordinary_quests_are_never_gated(self) -> None:
        self.assertFalse(self.incomplete({"status": "active"}, []))

    def test_unreadable_watch_file_does_not_trip_the_gate(self) -> None:
        # Malformed watch.json is watch-guard's problem; blocking commit here would
        # strand the quest for a different reason than the one this gate exists for.
        qid = self._quest({"requires_initial_run": True, "status": "active"}, [])
        (self.quests / qid / "watch.json").write_text("{not json")
        self.assertFalse(self.tick._initial_run_incomplete(self.t, qid))


if __name__ == "__main__":
    unittest.main()
