from __future__ import annotations

import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TRIAGE_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"


def _future_warnings(fn, *args, **kwargs) -> list[FutureWarning]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fn(*args, **kwargs)
    return [w.message for w in caught if issubclass(w.category, FutureWarning)]


class LegacyEnvDeprecationTest(unittest.TestCase):
    def test_legacy_process_env_input_warns_and_still_resolves(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-env-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
                workspace.env_file.write_text("")
                with patch.dict(os.environ, {"YAAS_AGENT": "legacy-agent"}, clear=False):
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        environment = _environment(workspace)
            self.assertTrue(any(
                issubclass(w.category, FutureWarning) and "YAAS_AGENT" in str(w.message)
                for w in caught
            ))
            self.assertEqual(environment["YAAS_AGENT"], "legacy-agent")
            self.assertEqual(environment["SIDEQUESTOR_AGENT"], "legacy-agent")

    def test_canonical_process_env_input_does_not_warn(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-canon-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
                "SIDEQUESTOR_AGENT": "codex",
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
                workspace.env_file.write_text("")
                with patch.dict(os.environ, {}, clear=False):
                    environment_warnings = _future_warnings(_environment, workspace)
        self.assertEqual(environment_warnings, [])

    def test_legacy_dotenv_entry_warns(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-dotenv-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
                workspace.env_file.write_text("YAAS_AGENT=legacy\n")
                with patch.dict(os.environ, {}, clear=False):
                    warnings_ = _future_warnings(_environment, workspace)
        self.assertTrue(any("YAAS_AGENT" in str(w) for w in warnings_))

    def test_internal_runtime_exports_do_not_warn(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-internal-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
                workspace.env_file.write_text("")
                with patch.dict(os.environ, {}, clear=False):
                    environment_warnings = _future_warnings(_environment, workspace)
        self.assertEqual(environment_warnings, [])

    def test_workspace_from_environment_legacy_fallback_warns(self) -> None:
        from sidequestor.cli import _workspace_from_environment

        with patch.dict(os.environ, {"YAAS_WORKSPACE": "/tmp/ws"}, clear=False):
            os.environ.pop("SIDEQUESTOR_WORKSPACE", None)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                resolved = _workspace_from_environment()
        self.assertEqual(resolved, "/tmp/ws")
        self.assertTrue(any(
            issubclass(w.category, FutureWarning) and "YAAS_WORKSPACE" in str(w.message)
            for w in caught
        ))

    def test_workspace_from_environment_canonical_does_not_warn(self) -> None:
        from sidequestor.cli import _workspace_from_environment

        with patch.dict(os.environ, {"SIDEQUESTOR_WORKSPACE": "/tmp/ws"}, clear=False):
            os.environ.pop("YAAS_WORKSPACE", None)
            warnings_ = _future_warnings(_workspace_from_environment)
        self.assertEqual(warnings_, [])

    def test_config_home_legacy_fallback_warns(self) -> None:
        from sidequestor.workspace import _config_home

        with patch.dict(os.environ, {"YAAS_CONFIG_HOME": "/tmp/sq-config"}, clear=False):
            os.environ.pop("SIDEQUESTOR_CONFIG_HOME", None)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                home = _config_home()
        self.assertEqual(home, Path("/tmp/sq-config"))
        self.assertTrue(any(
            issubclass(w.category, FutureWarning) and "YAAS_CONFIG_HOME" in str(w.message)
            for w in caught
        ))

    def test_config_home_canonical_does_not_warn(self) -> None:
        from sidequestor.workspace import _config_home

        with patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": "/tmp/sq-config"}, clear=False):
            os.environ.pop("YAAS_CONFIG_HOME", None)
            warnings_ = _future_warnings(_config_home)
        self.assertEqual(warnings_, [])

    def test_runtime_load_environment_legacy_input_warns(self) -> None:
        sys.path.insert(0, str(TRIAGE_ROOT))
        try:
            from tick_state import load_environment
            with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-runtime-") as raw:
                repo = Path(raw)
                (repo / ".env").write_text("")
                with patch.dict(os.environ, {"YAAS_AGENT": "legacy-agent"}, clear=False):
                    os.environ.pop("SIDEQUESTOR_AGENT", None)
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        values = load_environment(repo, dict(os.environ))
            self.assertTrue(any(
                issubclass(w.category, FutureWarning) and "YAAS_AGENT" in str(w.message)
                for w in caught
            ))
            self.assertEqual(values["YAAS_AGENT"], "legacy-agent")
            self.assertEqual(values["SIDEQUESTOR_AGENT"], "legacy-agent")
        finally:
            sys.path.pop(0)

    def test_runtime_load_environment_canonical_input_does_not_warn(self) -> None:
        sys.path.insert(0, str(TRIAGE_ROOT))
        try:
            from tick_state import load_environment
            with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-runtime-c-") as raw:
                repo = Path(raw)
                (repo / ".env").write_text("")
                with patch.dict(os.environ, {"SIDEQUESTOR_AGENT": "codex"}, clear=False):
                    os.environ.pop("YAAS_AGENT", None)
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        values = load_environment(repo, dict(os.environ))
            self.assertFalse(any(issubclass(w.category, FutureWarning) for w in caught))
            self.assertEqual(values["YAAS_AGENT"], "codex")
        finally:
            sys.path.pop(0)

    def test_runtime_legacy_dotenv_entry_warns(self) -> None:
        sys.path.insert(0, str(TRIAGE_ROOT))
        try:
            from tick_state import load_environment
            with tempfile.TemporaryDirectory(prefix="sidequestor-deprec-runtime-d-") as raw:
                repo = Path(raw)
                (repo / ".env").write_text("YAAS_AGENT=legacy\n")
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("YAAS_AGENT", None)
                    os.environ.pop("SIDEQUESTOR_AGENT", None)
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        values = load_environment(repo, dict(os.environ))
            self.assertTrue(any(
                issubclass(w.category, FutureWarning) and "YAAS_AGENT" in str(w.message)
                for w in caught
            ))
            self.assertEqual(values["YAAS_AGENT"], "legacy")
        finally:
            sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
