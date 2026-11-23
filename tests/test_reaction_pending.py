from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


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

    def test_expand_trigger_is_discovered_and_receipt_prevents_redispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pending = root / "state/triage/pending_reactions.json"
            results = "### Result 1 of 1\nMessage_ts: 1000.000001\nChannel_id: C123\n"
            self._run_checker(root, pending, "")  # First scan establishes a safe baseline.
            self._run_checker(root, pending, results)
            self.assertIn("1000.000001", json.loads(pending.read_text())["magic_wand"])
            (pending.parent / "reaction-lifecycle.json").write_text(json.dumps({"items": {
                "magic_wand:1000.000001": {"final": True, "confirmed": False}}}))
            self._run_checker(root, pending, results)
            remaining = json.loads(pending.read_text()) if pending.exists() else {}
            self.assertNotIn("magic_wand", remaining)

    def test_enabling_expand_does_not_replay_existing_wand_reactions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pending = root / "state/triage/pending_reactions.json"
            existing = "### Result 1 of 1\nMessage_ts: 1000.000001\nChannel_id: C123\n"
            self._run_checker(root, pending, existing)
            self.assertNotIn("magic_wand", json.loads(pending.read_text()))
            self._run_checker(root, pending, existing)
            self.assertNotIn("magic_wand", json.loads(pending.read_text()))
            new = existing + "### Result 2 of 2\nMessage_ts: 1000.000002\nChannel_id: C123\n"
            self._run_checker(root, pending, new)
            self.assertEqual(json.loads(pending.read_text())["magic_wand"], ["1000.000002"])

    def test_invalid_expand_baseline_is_local_error_not_auth_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pending = root / "state/triage/pending_reactions.json"
            pending.parent.mkdir(parents=True)
            (pending.parent / "reaction-expand-baseline.json").write_text("not json")
            self._run_checker(root, pending, "", expected_exit=2)

    def test_initial_expand_baseline_holds_older_pending_items(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pending = root / "state/triage/pending_reactions.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(json.dumps({"magic_wand": ["1000.000003"]}))
            self._run_checker(root, pending, "")
            remaining = json.loads(pending.read_text()) if pending.exists() else {}
            self.assertNotIn("magic_wand", remaining)
            baseline = json.loads((pending.parent / "reaction-expand-baseline.json").read_text())
            self.assertIn("1000.000003", baseline["seen_timestamps"])

    def test_pending_reaction_retains_source_link_for_inbox_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-reaction-source-") as raw:
            root = Path(raw)
            pending = root / "state/triage/pending_reactions.json"
            results = "### Result 1 of 1\nMessage_ts: 1000.000001\nPermalink: https://team.slack.com/archives/C123/p1000000001\n"
            self._run_checker(root, pending, results)
            source = json.loads((root / "state/reaction-sources.json").read_text())["robot_face:1000.000001"]
            self.assertEqual(source["channel_id"], "C123")
            self.assertEqual(source["permalink"], "https://team.slack.com/archives/C123/p1000000001")

    def _run_checker(self, workspace: Path, pending: Path, results: str, expected_exit=0) -> None:
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
        self.assertEqual(result.returncode, expected_exit, result.stderr)

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

    def test_adapter_timeout_is_transient_and_leaves_state_untouched(self) -> None:
        spec = importlib.util.spec_from_file_location("reactions_checker", CHECKER)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        with tempfile.TemporaryDirectory(prefix="sidequestor-reactions-") as raw:
            workspace = Path(raw)
            state = workspace / "state"
            pending = state / "triage" / "pending_reactions.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(json.dumps({"robot_face": ["1000.000002"]}))
            sources = state / "reaction-sources.json"
            sources.write_text(json.dumps({"robot_face:1000.000002": {"message_ts": "1000.000002"}}))
            before = {path: path.read_text() for path in (pending, sources)}

            stdout, stderr = io.StringIO(), io.StringIO()
            argv = ["reactions.py", "mcp-call", "1970-01-01", str(workspace), str(pending)]
            timeout = subprocess.TimeoutExpired("mcp-call", 30)
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(checker.subprocess, "run", side_effect=timeout), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = checker.main()

            self.assertEqual(code, 4)
            self.assertIn("REACTIONS_TRANSIENT_ERROR=1", stdout.getvalue())
            self.assertIn("REACTIONS_TRANSIENT_ERROR: Slack adapter timed out", stderr.getvalue())
            self.assertEqual({path: path.read_text() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
