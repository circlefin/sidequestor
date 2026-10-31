"""No-progress counters must not outlive the watch they describe."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TRIAGE_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
RUNTIME_ROOT = TRIAGE_ROOT.parent


def _load_tick():
    sys.path.insert(0, str(TRIAGE_ROOT))
    spec = importlib.util.spec_from_file_location("tick_unacked_prune_test", TRIAGE_ROOT / "tick.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(count=7):
    return {"count": count, "type": "slack_thread", "last_status": "pending",
            "backoff_sec": 300, "next_retry_ts": "0"}


class _FakeTick:
    def __init__(self, root: Path):
        self.repo_root = root
        self.quests_dir = root / "state" / "quests" / "active"
        self.quests_dir.mkdir(parents=True)
        self.manifest_dir = root / "state" / "triage"
        self.manifest_dir.mkdir(parents=True)
        self.unacked_file = self.manifest_dir / "unacked-counts.json"
        self.now_ts = 1000.0
        self.now_utc = "2026-10-22T00:00:00Z"
        self.unacked_promote = 3
        self.dispatch_run_id = "run-test"
        self.dispatch_last_error = ""
        self.logs = []
        self.events = []

    @staticmethod
    def _read_json(path, default=None):
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            return default

    def log(self, message):
        self.logs.append(message)

    def event(self, event):
        self.events.append(event)


class PruneUnackedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tick = _load_tick()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-unacked-prune-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.t = _FakeTick(self.root)

    def _quest(self, qid, watch_ids, bucket="active", meta=None, watch_doc=None):
        quest = self.root / "state" / "quests" / bucket / qid
        quest.mkdir(parents=True)
        (quest / "meta.json").write_text(json.dumps(meta if meta is not None
                                                    else {"id": qid, "status": "active"}))
        doc = watch_doc if watch_doc is not None else {
            "watches": [{"watch_id": wid, "type": "slack_thread"} for wid in watch_ids]}
        (quest / "watch.json").write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
        return quest

    def _counts(self, data):
        self.t.unacked_file.write_text(json.dumps(data))

    def _prune(self):
        dirs = sorted(d for d in self.t.quests_dir.iterdir() if d.is_dir())
        self.tick.prune_unacked(self.t, dirs)
        return json.loads(self.t.unacked_file.read_text())

    def test_retired_watch_counter_is_dropped_and_live_one_kept(self):
        self._quest("quest-a", ["watch-current"])
        self._counts({"quest-a|watch-retired": _record(), "quest-a|watch-current": _record(1)})
        self.assertEqual(set(self._prune()), {"quest-a|watch-current"})
        self.assertEqual(self.t.events[-1]["event"], "unacked_pruned")
        self.assertEqual(self.t.events[-1]["keys"], ["quest-a|watch-retired"])

    def test_closed_quest_counters_are_dropped(self):
        self._quest("quest-done", ["watch-1"], bucket="completed")
        self._quest("quest-gone", ["watch-2"], bucket="archived")
        self._counts({"quest-done|watch-1": _record(),
                      "quest-gone|sidequestor-bootstrap": _record()})
        self.assertEqual(self._prune(), {})

    def test_sentinels_reactions_and_unknown_scopes_survive(self):
        self._quest("quest-boot", [], meta={"sidequestor_bootstrap": True})
        self._quest("quest-a", ["watch-1"])
        kept = {"quest-boot|sidequestor-bootstrap": _record(),
                "quest-a|<unreadable-manifest>": _record(),
                "reactions|claude_intensifies:1730000000.000100": _record(),
                "some-future-scope|item": _record()}
        self._counts(kept)
        self.assertEqual(self._prune(), kept)
        self.assertEqual(self.t.events, [])

    def test_bootstrap_counter_dropped_once_bootstrap_has_finished(self):
        self._quest("quest-a", ["watch-1"])
        self._counts({"quest-a|sidequestor-bootstrap": _record()})
        self.assertEqual(self._prune(), {})

    def test_unreadable_quest_files_prune_nothing_for_that_quest(self):
        self._quest("quest-bad-json", [], watch_doc="{not json")
        self._quest("quest-no-id", [], watch_doc={"watches": [{"type": "slack_thread"}]})
        self._quest("quest-int-id", [], watch_doc={"watches": [{"watch_id": 7}]})
        self._quest("quest-bad-meta", ["watch-1"], meta=[])
        counts = {"quest-bad-json|watch-x": _record(),
                  "quest-no-id|watch-x": _record(),
                  "quest-bad-meta|watch-x": _record(),
                  "quest-int-id|7": _record()}
        self._counts(counts)
        self.assertEqual(self._prune(), counts)

    def test_unreadable_counter_file_is_left_untouched(self):
        self._quest("quest-a", ["watch-1"])
        self.t.unacked_file.write_text("{truncated")
        self.tick.prune_unacked(self.t, [self.t.quests_dir / "quest-a"])
        self.assertEqual(self.t.unacked_file.read_text(), "{truncated")

    def test_missing_counter_file_is_not_created(self):
        self._quest("quest-a", ["watch-1"])
        self.tick.prune_unacked(self.t, [self.t.quests_dir / "quest-a"])
        self.assertFalse(self.t.unacked_file.exists())

    def test_readable_manifest_clears_unreadable_manifest_sentinel(self):
        self._quest("quest-a", ["watch-1"])
        self._counts({"quest-a|<unreadable-manifest>": _record()})
        (self.t.manifest_dir / "dispatch-run-test.json").write_text(json.dumps({
            "items": [{"item_id": "watch-1", "type": "slack_thread", "status": "handled"}]}))
        self.tick._record_progress(self.t, "quest-a", ["watch-1"])
        self.assertEqual(json.loads(self.t.unacked_file.read_text()), {})


class DashboardHidesOrphanCountersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-unacked-dashboard-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        env = patch.dict(os.environ, {
            "YAAS_WORKSPACE": str(self.workspace),
            "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
            "SIDEQUESTOR_CHECKER_CONNECTORS": "slack,email,github,jira,x",
        })
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(TRIAGE_ROOT))
        self.addCleanup(lambda: sys.path.remove(str(TRIAGE_ROOT)))
        spec = importlib.util.spec_from_file_location(
            "sidequestor_dashboard_unacked_test", TRIAGE_ROOT / "ops" / "dashboard-server.py")
        self.server = importlib.util.module_from_spec(spec)
        with patch.object(sys, "argv", ["dashboard-server.py"]):
            spec.loader.exec_module(self.server)
        self.quest = self.workspace / "state" / "quests" / "active" / "quest-one"
        self.quest.mkdir(parents=True)
        (self.quest / "meta.json").write_text(json.dumps(
            {"id": "quest-one", "title": "Quest one", "status": "active"}))
        (self.quest / "watch.json").write_text(json.dumps({"watches": [
            {"watch_id": "watch-current", "type": "slack_thread", "last_checked_ts": "0"}]}))
        triage = self.workspace / "state" / "triage"
        triage.mkdir(parents=True)
        (triage / "unacked-counts.json").write_text(json.dumps({
            "quest-one|watch-retired": _record(7),
            "quest-one|watch-current": _record(4),
            "quest-one|<unreadable-manifest>": _record(3),
        }))

    def _list_ids(self):
        quest = self.server.build_dashboard()["quests"][0]
        return {w["watch_id"] for w in quest["backoff_watches"]}

    def _detail_ids(self):
        detail = self.server.build_quest_detail("quest-one")
        return {w["watch_id"] for w in detail["open_items"]["backoff_watches"]}

    def test_retired_watch_is_not_shown_as_backing_off(self):
        expected = {"watch-current", "<unreadable-manifest>"}
        self.assertEqual(self._list_ids(), expected)
        self.assertEqual(self._detail_ids(), expected)

    def test_bootstrap_counter_shown_only_while_bootstrap_is_pending(self):
        counts = self.workspace / "state" / "triage" / "unacked-counts.json"
        counts.write_text(json.dumps({"quest-one|sidequestor-bootstrap": _record(5)}))
        self.assertEqual(self._list_ids(), set())
        (self.quest / "meta.json").write_text(json.dumps(
            {"id": "quest-one", "title": "Quest one", "status": "active",
             "sidequestor_bootstrap": True}))
        self.assertEqual(self._list_ids(), {"sidequestor-bootstrap"})
        self.assertEqual(self._detail_ids(), {"sidequestor-bootstrap"})

    def test_watch_without_usable_id_shows_every_counter(self):
        (self.quest / "watch.json").write_text(json.dumps({"watches": [
            {"watch_id": "watch-current", "type": "slack_thread"}, {"type": "slack_thread"}]}))
        self.assertIn("watch-retired", self._detail_ids())

    def test_unreadable_watch_file_shows_every_counter(self):
        (self.quest / "watch.json").write_text("{partial")
        expected = {"watch-retired", "watch-current", "<unreadable-manifest>"}
        self.assertEqual(self._list_ids(), expected)
        self.assertEqual(self._detail_ids(), expected)


if __name__ == "__main__":
    unittest.main()
