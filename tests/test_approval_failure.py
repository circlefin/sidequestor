from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime"
TRIAGE_ROOT = RUNTIME_ROOT / "yaas-triage"


class ApprovalFailureTest(unittest.TestCase):
    def test_failure_returns_reviewed_item_to_pending_review(self) -> None:
        sys.path.insert(0, str(TRIAGE_ROOT))
        try:
            import approval_state

            now = datetime(2026, 8, 23, tzinfo=timezone.utc)
            item = {"id": "approval-1", "status": "reviewed"}
            failed = approval_state.apply_transition(
                item, "fail", {"reason": "channel_not_found"}, now,
            )
            self.assertEqual(failed["status"], "pending_review")
            self.assertEqual(failed["processing_error"], "channel_not_found")
            self.assertEqual(failed["failed_from_status"], "reviewed")

            recovered = approval_state.apply_transition(
                {**item, **failed}, "review", {}, now,
            )
            self.assertIsNone(recovered["processing_error"])
            self.assertIsNone(recovered["processing_error_at"])
        finally:
            sys.path.pop(0)

    def test_failed_approval_is_clean_until_human_reviews_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-approval-failure-") as raw:
            workspace = Path(raw)
            approvals = workspace / "state" / "pending-approvals.json"
            approvals.parent.mkdir(parents=True)
            approvals.write_text(json.dumps({
                "version": 1,
                "items": [{"id": "approval-1", "status": "reviewed"}],
            }))
            env = {
                **os.environ,
                "YAAS_WORKSPACE": str(workspace),
                "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
            }
            helper = TRIAGE_ROOT / "ledger" / "approval-helper.py"
            checker = TRIAGE_ROOT / "checkers" / "approval.py"
            failed = subprocess.run(
                [sys.executable, str(helper), "fail", "approval-1", "channel_not_found"],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(failed.returncode, 0, failed.stderr)

            checked = subprocess.run(
                [sys.executable, str(checker), json.dumps({"approval_id": "approval-1"})],
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            result = json.loads(checked.stdout)
            self.assertEqual(result["outcome"], "clean")
            self.assertIn("status=pending_review", result["preview"])
            item = json.loads(approvals.read_text())["items"][0]
            self.assertEqual(item["processing_error"], "channel_not_found")

    def test_tick_returns_unprocessed_approval_without_retry_counter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-tick-approval-failure-") as raw:
            workspace = Path(raw)
            quest = workspace / "state" / "quests" / "active" / "quest-1"
            manifest_dir = workspace / "state" / "triage"
            quest.mkdir(parents=True)
            manifest_dir.mkdir(parents=True)
            (quest / "watch.json").write_text(json.dumps({"watches": [{
                "type": "approval",
                "approval_id": "approval-1",
                "watch_id": "watch-approval-1",
            }]}))
            (workspace / "state" / "pending-approvals.json").write_text(json.dumps({
                "version": 1,
                "items": [{"id": "approval-1", "status": "reviewed"}],
            }))
            (manifest_dir / "dispatch-run-test.json").write_text(json.dumps({
                "items": [{
                    "item_id": "watch-approval-1",
                    "type": "approval",
                    "status": "pending",
                    "note": "channel_not_found",
                }],
            }))
            counts = manifest_dir / "unacked-counts.json"
            counts.write_text(json.dumps({
                "quest-1|watch-approval-1": {"count": 4},
            }))

            spec = importlib.util.spec_from_file_location(
                "sidequestor_tick_failure_test", TRIAGE_ROOT / "tick.py",
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.path.insert(0, str(TRIAGE_ROOT))
            try:
                spec.loader.exec_module(module)

                class FakeTick:
                    def __init__(self):
                        self.manifest_dir = manifest_dir
                        self.unacked_file = counts
                        self.quests_dir = workspace / "state" / "quests" / "active"
                        self.dispatch_run_id = "run-test"
                        self.dispatch_last_error = ""
                        self.now_utc = "2026-08-23T00:00:00Z"
                        self.unacked_promote = 3

                    def _read_json(self, path, default):
                        try:
                            return json.loads(Path(path).read_text())
                        except (OSError, ValueError):
                            return default

                    def helper(self, *parts):
                        return TRIAGE_ROOT.joinpath(*parts)

                    def py(self, *args):
                        return [sys.executable, *(str(arg) for arg in args)]

                    def run(self, command):
                        return subprocess.run(
                            command,
                            text=True,
                            capture_output=True,
                            env={**os.environ,
                                 "YAAS_WORKSPACE": str(workspace),
                                 "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT)},
                        )

                    def log(self, message):
                        self.logged = message

                    def event(self, event):
                        self.event_record = event

                module._record_progress(FakeTick(), "quest-1", [])
            finally:
                sys.path.pop(0)

            self.assertEqual(json.loads(counts.read_text()), {})
            item = json.loads(
                (workspace / "state" / "pending-approvals.json").read_text(),
            )["items"][0]
            self.assertEqual(item["status"], "pending_review")
            self.assertEqual(item["processing_error"], "channel_not_found")


if __name__ == "__main__":
    unittest.main()
