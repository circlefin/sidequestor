from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CHECKERS = ROOT / "src/sidequestor/runtime/yaas-triage/checkers"


def load(name, path):
    original_path = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = original_path


class XUserCheckerTests(unittest.TestCase):
    def emitted(self, function):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            function()
        return json.loads(output.getvalue())

    def test_shared_checker_uses_user_oauth(self):
        module = load("x_user_checker_test", CHECKERS / "x.py")
        with mock.patch.object(module, "_call", return_value={"data": [], "meta": {}}) as call:
            self.emitted(lambda: module.run(
                {"last_checked_ts": "100", "credential_id": "work"}, "/2/test", now=200))
        self.assertEqual("user:work", call.call_args.args[2])

    def test_x_user_watchers_are_executable_and_manifested(self):
        for name in ("x_mentions", "x_user_posts", "x_home", "x_dm"):
            self.assertTrue((CHECKERS / f"{name}.py").is_file(), name)
            self.assertTrue((CHECKERS / f"{name}.watch.json").is_file(), name)
            self.assertTrue(os.access(CHECKERS / f"{name}.py", os.X_OK), name)

    def test_dm_checker_only_counts_incoming_events_in_the_selected_conversation(self):
        module = load("x_dm_checker_test", CHECKERS / "x_dm.py")
        rows = {
            "data": [
                {"id": "3", "created_at": "1970-01-01T00:02:30Z", "sender_id": "42",
                 "dm_conversation_id": "c1", "text": "outgoing"},
                {"id": "2", "created_at": "1970-01-01T00:02:20Z", "sender_id": "7",
                 "dm_conversation_id": "c2", "text": "other"},
                {"id": "1", "created_at": "1970-01-01T00:02:10Z", "sender_id": "7",
                 "dm_conversation_id": "c1", "text": "incoming"},
            ],
            "meta": {},
        }
        entry = {"last_checked_ts": "100", "conversation_id": "c1"}
        with mock.patch.object(module.x, "current_user_id", return_value="42"), \
                mock.patch.object(module.x, "_call", return_value=rows):
            result = self.emitted(lambda: module.run(entry, now=200, lag=30))
        self.assertEqual("dirty", result["outcome"])
        self.assertEqual(1, result["count"])
        self.assertEqual("170.000000", result["advance_to"])


if __name__ == "__main__":
    unittest.main()
