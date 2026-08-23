from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime"
TRIAGE_ROOT = RUNTIME_ROOT / "yaas-triage"


class RuntimeConfigTest(unittest.TestCase):
    def test_canonical_environment_resolves_to_runtime_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-config-") as raw:
            workspace = Path(raw)
            (workspace / ".env").write_text(
                "SIDEQUESTOR_AGENT=codex\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
            )
            with patch.dict(os.environ, {"YAAS_WORKSPACE": str(workspace)}, clear=False):
                import sys
                sys.path.insert(0, str(TRIAGE_ROOT))
                try:
                    from tick_state import Config

                    config = Config(TRIAGE_ROOT)
                finally:
                    sys.path.pop(0)
            self.assertEqual(config.env["YAAS_AGENT"], "codex")
            self.assertEqual(config.env["YAAS_SLACK_CHECKERS_ENABLED"], "0")

    def test_dashboard_uses_the_same_effective_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-dashboard-config-") as raw:
            workspace = Path(raw)
            (workspace / ".env").write_text(
                "SIDEQUESTOR_AGENT=codex\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
            )
            environment = {
                "YAAS_WORKSPACE": str(workspace),
                "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
            }
            with patch.dict(os.environ, environment, clear=False):
                import sys
                sys.path.insert(0, str(TRIAGE_ROOT))
                try:
                    spec = importlib.util.spec_from_file_location(
                        "sidequestor_dashboard_test",
                        TRIAGE_ROOT / "ops" / "dashboard-server.py",
                    )
                    module = importlib.util.module_from_spec(spec)
                    assert spec.loader is not None
                    with patch.object(sys, "argv", ["dashboard-server.py"]):
                        spec.loader.exec_module(module)
                    config = module.build_config()
                finally:
                    sys.path.pop(0)

            items = {
                item["key"]: item
                for group in config["groups"]
                for item in group["items"]
            }
            self.assertEqual(items["YAAS_AGENT"]["value"], "codex")
            self.assertTrue(items["YAAS_AGENT"]["set"])
            self.assertEqual(items["YAAS_SLACK_CHECKERS_ENABLED"]["value"], "0")
            self.assertTrue(items["YAAS_SLACK_CHECKERS_ENABLED"]["set"])


if __name__ == "__main__":
    unittest.main()
