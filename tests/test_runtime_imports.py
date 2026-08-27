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
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"


class InstalledRuntimeImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-imports-")
        self.workspace = Path(self.temp.name)
        (self.workspace / "state" / "triage").mkdir(parents=True)
        (self.workspace / "state" / "quests" / "active").mkdir(parents=True)
        (self.workspace / "logs").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("YAAS_RUNTIME_ROOT", None)
        env["YAAS_WORKSPACE"] = str(self.workspace)
        return env

    def test_direct_approval_helper_uses_installed_runtime_root(self) -> None:
        script = RUNTIME_ROOT / "ledger" / "approval-helper.py"
        result = subprocess.run(
            [sys.executable, str(script), "ensure-inbox"],
            text=True,
            capture_output=True,
            env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("quest-inbox", result.stdout)

    def test_approval_watch_arming_stays_inside_installed_runtime(self) -> None:
        script = RUNTIME_ROOT / "ledger" / "approval-helper.py"
        inbox = subprocess.run(
            [sys.executable, str(script), "ensure-inbox"],
            text=True,
            capture_output=True,
            env=self._env(),
        )
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        payload = (
            '{"quest_id":"quest-inbox","target":{"channel_id":"C123",'
            '"thread_ts":"123.456"},"message_text":"draft"}'
        )
        result = subprocess.run(
            [sys.executable, str(script), "write", payload],
            text=True,
            capture_output=True,
            env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        watch = json.loads(
            (self.workspace / "state" / "quests" / "active" / "quest-inbox" / "watch.json").read_text()
        )
        self.assertTrue(any(w.get("type") == "approval" for w in watch["watches"]))

    def test_runtime_modules_import_without_workspace_runtime_tree(self) -> None:
        for relative in (
            "ledger/approval-helper.py",
            "ledger/checker-health.py",
            "checkers/approval.py",
            "skills/yaas-quest-creation/new-quest.py",
        ):
            path = RUNTIME_ROOT / relative
            module_name = "sidequestor_test_" + path.stem.replace("-", "_")
            spec = importlib.util.spec_from_file_location(module_name, path)
            self.assertIsNotNone(spec, relative)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                assert spec.loader is not None
                spec.loader.exec_module(module)
            except ModuleNotFoundError as exc:
                self.fail(f"{relative} imported a workspace-relative module: {exc}")
            finally:
                sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
