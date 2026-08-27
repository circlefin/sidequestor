"""Explicit bootstrap state must replace schedule-shape inference."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"


def _load_tick():
    sys.path.insert(0, str(RUNTIME_ROOT))
    spec = importlib.util.spec_from_file_location("tick_under_test", RUNTIME_ROOT / "tick.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTick:
    def __init__(self, root: Path):
        self.repo_root = root
        self.quests_dir = root / "state" / "quests" / "active"
        self.quests_dir.mkdir(parents=True)
        self.unacked_file = root / "state" / "triage" / "unacked-counts.json"
        self.unacked_file.parent.mkdir(parents=True)
        self.now_ts = 1000.0
        self.dispatch_run_id = "run-test"
        self.logs = []
        self.events = []

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return default

    def log(self, message):
        self.logs.append(message)

    def event(self, event):
        self.events.append(event)


class BootstrapStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tick = _load_tick()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-bootstrap-")
        self.t = _FakeTick(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def _quest(self, meta, watches):
        qid = "quest-under-test"
        folder = self.t.quests_dir / qid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").write_text(json.dumps({"id": qid, **meta}))
        (folder / "watch.json").write_text(json.dumps({"watches": watches}))
        return folder

    def test_explicit_empty_bootstrap_quest_gets_synthetic_dispatch(self):
        folder = self._quest({"status": "active", "sidequestor_bootstrap": True}, [])
        pending = self.tick.prepare_bootstrap_quests(self.t, [folder])
        self.assertEqual(pending, {folder.name})
        row = self.tick.bootstrap_result(self.t, folder.name)
        self.assertEqual(row["status"], "dirty")
        self.assertEqual(row["watch_id"], self.tick.BOOTSTRAP_ITEM_ID)

    def test_real_watch_alone_does_not_clear_flag_before_ack(self):
        watch = {"type": "email", "query": "from:a@example.com", "reason": "real"}
        folder = self._quest({"status": "active", "sidequestor_bootstrap": True}, [watch])
        self.assertEqual(self.tick.prepare_bootstrap_quests(self.t, [folder]), {folder.name})
        self.assertTrue(json.loads((folder / "meta.json").read_text())["sidequestor_bootstrap"])

    def test_arbitrary_one_shot_schedule_is_never_bootstrap(self):
        one_shot = {"type": "schedule", "next_fire_ts": "1234", "reason": "legitimate"}
        folder = self._quest({"status": "active"}, [one_shot])
        self.assertEqual(self.tick.prepare_bootstrap_quests(self.t, [folder]), set())
        self.assertEqual(json.loads((folder / "watch.json").read_text())["watches"], [one_shot])

    def test_exact_legacy_placeholder_migrates_to_flag(self):
        legacy = {"type": "schedule", "next_fire_ts": "1234",
                  "reason": self.tick.LEGACY_BOOTSTRAP_REASON}
        folder = self._quest({"status": "active", "requires_initial_run": True}, [legacy])
        self.assertEqual(self.tick.prepare_bootstrap_quests(self.t, [folder]), {folder.name})
        self.assertEqual(json.loads((folder / "watch.json").read_text())["watches"], [])
        meta = json.loads((folder / "meta.json").read_text())
        self.assertTrue(meta["sidequestor_bootstrap"])
        self.assertNotIn("requires_initial_run", meta)

    def test_unknown_legacy_one_shot_is_left_untouched(self):
        unknown = {"type": "schedule", "next_fire_ts": "1234", "reason": "initial run"}
        folder = self._quest({"status": "active", "requires_initial_run": True}, [unknown])
        self.assertEqual(self.tick.prepare_bootstrap_quests(self.t, [folder]), set())
        self.assertTrue(json.loads((folder / "meta.json").read_text())["requires_initial_run"])

    def test_bootstrap_uses_existing_no_progress_backoff(self):
        folder = self._quest({"status": "active", "sidequestor_bootstrap": True}, [])
        self.t.unacked_file.write_text(json.dumps({
            f"{folder.name}|{self.tick.BOOTSTRAP_ITEM_ID}": {"next_retry_ts": "1100"}
        }))
        self.assertEqual(self.tick.bootstrap_result(self.t, folder.name)["status"], "backoff")

    def test_blocked_active_quest_retains_bootstrap_state(self):
        folder = self._quest({"status": "blocked", "sidequestor_bootstrap": True}, [])
        self.assertEqual(self.tick.prepare_bootstrap_quests(self.t, [folder]), {folder.name})
        self.assertTrue(json.loads((folder / "meta.json").read_text())["sidequestor_bootstrap"])

    def test_ack_and_real_watch_clear_flag_together(self):
        watch = {"type": "email", "query": "from:a@example.com", "reason": "real"}
        folder = self._quest({"status": "active", "sidequestor_bootstrap": True}, [watch])
        with mock.patch.object(self.tick, "_record_progress") as progress:
            self.tick.commit_bootstrap(self.t, folder.name, [self.tick.BOOTSTRAP_ITEM_ID])
        self.assertNotIn("sidequestor_bootstrap",
                         json.loads((folder / "meta.json").read_text()))
        progress.assert_called_once_with(self.t, folder.name, [self.tick.BOOTSTRAP_ITEM_ID])

    def test_ack_without_activation_evidence_retains_flag(self):
        folder = self._quest({"status": "active", "sidequestor_bootstrap": True}, [])
        with mock.patch.object(self.tick, "_record_progress") as progress:
            self.tick.commit_bootstrap(self.t, folder.name, [self.tick.BOOTSTRAP_ITEM_ID])
        self.assertTrue(json.loads((folder / "meta.json").read_text())["sidequestor_bootstrap"])
        progress.assert_called_once_with(self.t, folder.name, [])

    def test_bootstrap_prompt_uses_existing_quest_dispatch_contract(self):
        source = (RUNTIME_ROOT / "tick.py").read_text()
        bootstrap = source[source.index("elif is_bootstrap:"):source.index("    else:\n", source.index("elif is_bootstrap:"))]
        self.assertIn("yaas-quest-dispatch", bootstrap)
        self.assertNotIn("yaas-quest-creation", bootstrap)

    def test_bootstrap_prompt_requires_live_identifier_verification_before_watch(self):
        source = (RUNTIME_ROOT / "tick.py").read_text()
        start = source.index("elif is_bootstrap:")
        bootstrap = source[start:source.index("    else:\n", start)]
        self.assertIn("Do not call `sq watch` until", bootstrap)
        self.assertIn("candidate `channel_id` plus parent `thread_ts`", bootstrap)
        self.assertIn("Permalink parsing, a name match, or a", bootstrap)
        self.assertIn("checker-equivalent live-read", bootstrap)
        self.assertIn("block instead of installing the watch", bootstrap)


if __name__ == "__main__":
    unittest.main()
