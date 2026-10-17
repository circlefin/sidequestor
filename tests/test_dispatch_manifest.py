from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TRIAGE_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"


def _load_tick():
    sys.path.insert(0, str(TRIAGE_ROOT))
    spec = importlib.util.spec_from_file_location("dispatch_manifest_tick", TRIAGE_ROOT / "tick.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_housekeep():
    spec = importlib.util.spec_from_file_location(
        "dispatch_manifest_housekeep", TRIAGE_ROOT / "ledger" / "housekeep.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DispatchManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tick = _load_tick()
        cls.housekeep = _load_housekeep()

    def test_quest_dispatch_items_preserve_complete_gate(self) -> None:
        dirty_watches = [
            {"quest_id": "quest-a", "watch_id": "watch-complete",
             "type": "slack_mention", "complete": True},
            {"quest_id": "quest-a", "watch_id": "watch-incomplete",
             "type": "slack_mention", "complete": False},
            {"quest_id": "quest-b", "watch_id": "watch-other",
             "type": "slack_mention", "complete": True},
        ]

        self.assertEqual(self.tick._quest_dispatch_items("quest-a", dirty_watches), [
            {"item_id": "watch-complete", "type": "slack_mention", "complete": True},
            {"item_id": "watch-incomplete", "type": "slack_mention", "complete": False},
        ])

    def test_ack_manifest_persists_complete_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-dispatch-manifest-") as raw:
            workspace = Path(raw)
            env = {
                **os.environ,
                "YAAS_WORKSPACE": str(workspace),
                "YAAS_RUNTIME_ROOT": str(PACKAGE_ROOT / "src" / "sidequestor" / "runtime"),
            }
            helper = TRIAGE_ROOT / "ledger" / "ack-watch.py"
            result = subprocess.run([
                sys.executable, str(helper), "open", "run-complete", "quest-a", "quest",
                ('[{"item_id":"watch-a","type":"slack_mention","complete":true},'
                 '{"item_id":"watch-b","type":"slack_mention","complete":false}]'),
            ], text=True, capture_output=True, env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(Path(result.stdout.strip()).read_text())
            self.assertIs(manifest["items"][0]["complete"], True)
            self.assertIs(manifest["items"][1]["complete"], False)

    def test_completed_thread_handoff_is_retired(self) -> None:
        pending = {
            "type": "slack_thread",
            "last_checked_ts": "100.000000",
            "last_activity_ts": "101.000000",
            "one_shot_until_ts": "101.000000",
        }
        completed = {**pending, "last_checked_ts": "101.000000"}

        self.assertFalse(self.housekeep.retire_handoff(pending))
        self.assertTrue(self.housekeep.retire_handoff(completed))

    def test_clean_cursor_without_observed_target_does_not_retire_handoff(self) -> None:
        watch = {
            "type": "slack_thread",
            "last_checked_ts": "200.000000",
            "one_shot_until_ts": "101.000000",
        }

        self.assertFalse(self.housekeep.retire_handoff(watch))

    def test_add_watch_rejects_an_unsafe_thread_handoff_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-thread-handoff-") as raw:
            workspace = Path(raw)
            quest = workspace / "state" / "quests" / "active" / "quest-a"
            quest.mkdir(parents=True)
            (quest / "watch.json").write_text('{"watches": []}\n')
            env = {**os.environ, "SIDEQUESTOR_WORKSPACE": str(workspace)}
            helper = TRIAGE_ROOT / "ledger" / "add-watch.py"
            payload = {
                "type": "slack_thread",
                "channel_id": "C0AAAA1",
                "thread_ts": "101.000000",
                "last_checked_ts": "101.000000",
                "ephemeral": True,
                "include_parent": True,
                "one_shot_until_ts": "101.000000",
                "reason": "retry blocked mention",
            }

            result = subprocess.run(
                [sys.executable, str(helper), "quest-a", json.dumps(payload)],
                text=True, capture_output=True, env=env,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("one_shot_until_ts_must_be_after_last_checked_ts", result.stderr)

    def test_include_parent_requires_bounded_handoff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-slack-parent-") as raw:
            watch_path = Path(raw) / "watch.json"
            quest_dir = watch_path.parent / "state" / "quests" / "active" / "quest-a"
            quest_dir.mkdir(parents=True)
            (quest_dir / "watch.json").write_text('{"watches": []}')
            watch = {
                "type": "slack_thread",
                "channel_id": "C0AAAA1",
                "thread_ts": "100.000000",
                "include_parent": True,
                "reason": "unbounded parent read",
            }
            result = subprocess.run(
                [sys.executable, str(TRIAGE_ROOT / "ledger" / "add-watch.py"),
                 "quest-a", json.dumps(watch)],
                text=True, capture_output=True,
                env={**os.environ, "SIDEQUESTOR_WORKSPACE": raw},
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("include_parent_needs_one_shot_until_ts", result.stderr)

    def test_thread_handoff_requires_ephemeral_watch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-slack-ephemeral-") as raw:
            quest_dir = Path(raw) / "state" / "quests" / "active" / "quest-a"
            quest_dir.mkdir(parents=True)
            (quest_dir / "watch.json").write_text('{"watches": []}')
            watch = {
                "type": "slack_thread",
                "channel_id": "C0AAAA1",
                "thread_ts": "100.000000",
                "last_checked_ts": "99.999999",
                "include_parent": True,
                "one_shot_until_ts": "100.000000",
                "reason": "bounded parent read",
            }
            result = subprocess.run(
                [sys.executable, str(TRIAGE_ROOT / "ledger" / "add-watch.py"),
                 "quest-a", json.dumps(watch)],
                text=True, capture_output=True,
                env={**os.environ, "SIDEQUESTOR_WORKSPACE": raw},
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("one_shot_until_ts_needs_ephemeral_true", result.stderr)


if __name__ == "__main__":
    unittest.main()
