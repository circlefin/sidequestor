from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CHECKER = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
           / "checkers" / "reactions.py")


class ReactionPendingQueueTest(unittest.TestCase):
    def test_terminal_receipt_prunes_pending_even_without_worker_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            pending = workspace / 'state/triage/pending_reactions.json'
            pending.parent.mkdir(parents=True)
            pending.write_text(json.dumps({'robot_face': ['1000.000001']}))
            (pending.parent / 'reaction-lifecycle.json').write_text(json.dumps({'items': {
                'robot_face:1000.000001': {'final': True, 'confirmed': False}}}))
            self._run_checker(workspace, pending, '### Result 1 of 1\nMessage_ts: 1000.000001\n')
            remaining = json.loads(pending.read_text()) if pending.exists() else {}
            self.assertNotIn('robot_face', remaining)

    def test_pending_reaction_retains_source_link_for_inbox_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-reaction-source-") as raw:
            root = Path(raw)
            pending = root / "state/triage/pending_reactions.json"
            results = "### Result 1 of 1\nMessage_ts: 1000.000001\nPermalink: https://team.slack.com/archives/C123/p1000000001\n"
            self._run_checker(root, pending, results)
            source = json.loads((root / "state/reaction-sources.json").read_text())["robot_face:1000.000001"]
            self.assertEqual(source["channel_id"], "C123")
            self.assertEqual(source["permalink"], "https://team.slack.com/archives/C123/p1000000001")

    def _run_checker(self, workspace: Path, pending: Path, results: str) -> None:
        adapter = workspace / "fake-reaction-search.py"
        adapter.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "print(json.dumps({'results': os.environ['FAKE_RESULTS'], 'pagination_info': ''}))\n"
        )
        adapter.chmod(0o755)
        result = subprocess.run(
            [sys.executable, str(CHECKER), str(adapter), "1970-01-01",
             str(workspace), str(pending)],
            text=True,
            capture_output=True,
            env={**os.environ, "FAKE_RESULTS": results},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_blocked_pending_survives_a_clean_sweep(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-reactions-") as raw:
            workspace = Path(raw)
            pending = workspace / "state" / "triage" / "pending_reactions.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(json.dumps({"robot_face": ["1000.000002"]}))

            self._run_checker(workspace, pending, "")

            self.assertEqual(
                json.loads(pending.read_text()),
                {"robot_face": ["1000.000002"]},
            )

    def test_pending_queue_is_merged_and_completed_entries_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-reactions-") as raw:
            workspace = Path(raw)
            state = workspace / "state"
            pending = state / "triage" / "pending_reactions.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(json.dumps({
                "robot_face": ["1000.000002", "1000.000004"],
            }))
            (state / "claude_intensifies_replied.json").write_text(json.dumps({
                "replied_timestamps": ["1000.000002"],
            }))
            results = "### Result 1 of 1\nMessage_ts: 1000.000001\n"

            self._run_checker(workspace, pending, results)

            queued = json.loads(pending.read_text())
            self.assertEqual(
                queued["robot_face"],
                ["1000.000001", "1000.000004"],
            )

    def test_malformed_pending_file_does_not_block_new_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-reactions-") as raw:
            workspace = Path(raw)
            pending = workspace / "state" / "triage" / "pending_reactions.json"
            pending.parent.mkdir(parents=True)
            pending.write_text("not json")
            results = "### Result 1 of 1\nMessage_ts: 1000.000001\n"

            self._run_checker(workspace, pending, results)

            self.assertEqual(
                json.loads(pending.read_text())["robot_face"],
                ["1000.000001"],
            )


if __name__ == "__main__":
    unittest.main()
