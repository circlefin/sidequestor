from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CHECKER = (ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
           / "checkers" / "reactions.py")


def load_checker():
    spec = importlib.util.spec_from_file_location("reactions_timeout_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReactionCheckerTimeoutTest(unittest.TestCase):
    def test_slack_search_timeout_is_transient(self) -> None:
        module = load_checker()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="sidequestor-reactions-") as raw:
            workspace = Path(raw)
            pending = workspace / "state" / "triage" / "pending_reactions.json"
            argv = ["reactions.py", "mcp-call", "1970-01-01",
                    str(workspace), str(pending)]
            with mock.patch.object(
                module.subprocess,
                "run",
                side_effect=module.subprocess.TimeoutExpired("mcp-call", 30),
            ), mock.patch.object(module.sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(stderr):
                code = module.main()

        self.assertEqual(code, 4)
        self.assertIn("REACTIONS_TRANSIENT_ERROR", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
