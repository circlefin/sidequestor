from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SLACK_SEND = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
              / "surfaces" / "slack-send.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("slack_send_under_test", SLACK_SEND)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SlackSendStaleGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_reviewed_at_refreshes_an_old_thread(self) -> None:
        reviewed_at = "2026-12-08T00:00:00+00:00"
        reviewed_epoch = self.mod.datetime.fromisoformat(reviewed_at).timestamp()
        now = reviewed_epoch + 6 * 3600
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=reviewed_epoch - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={
                 "items": [{"id": "appr-1", "reviewed_at": reviewed_at}],
             }):
            self.assertIsNone(
                self.mod._stale_reason(
                    "C123", "parent-ts", now=now, approval_id="appr-1",
                )
            )

    def test_old_review_does_not_refresh_an_old_thread_forever(self) -> None:
        now = 1_800_000_000.0
        reviewed_at = "2026-11-20T00:00:00+00:00"
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=now - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={
                 "items": [{"id": "appr-1", "reviewed_at": reviewed_at}],
             }):
            reason = self.mod._stale_reason(
                "C123", "parent-ts", now=now, approval_id="appr-1",
            )
            self.assertIn("48.0h old", reason)

    def test_missing_approval_timestamp_preserves_existing_guard(self) -> None:
        now = 1_800_000_000.0
        with patch.object(self.mod, "STALE_HOURS", 24), \
             patch.object(self.mod, "_thread_last_activity", return_value=now - 48 * 3600), \
             patch.object(self.mod.approval_store, "read_queue", return_value={"items": []}):
            reason = self.mod._stale_reason(
                "C123", "parent-ts", now=now, approval_id="missing",
            )
            self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main()
