"""Where the Slack credential surface keeps its per-workspace state.

The Keychain helper binary and the OAuth refresh lock were both resolved relative to
this file, which was the repo root before Sidequestor became a pip package and is the
installed package directory now. That put a compiled binary and a lock inside
site-packages: shared across every workspace on the venv, and unwritable on a --user or
system install, where the failure surfaces as "could not build the helper" rather than
as the permissions error it is.

The helper has one extra constraint that the lock does not, and it is the reason these
tests exist: macOS binds a Keychain item's ACL to the binary that reads it. Repointing an
existing install at a freshly compiled binary raises a GUI trust prompt that nobody sees
under launchd, and the read times out. Verified against a live workspace before this was
written. So an already-authorized helper wins, wherever it lives.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SURFACE = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
           / "surfaces" / "slack_credentials.py")
WORKSPACE_VARS = ("SIDEQUESTOR_WORKSPACE", "YAAS_WORKSPACE", "REPO_ROOT")


def _load():
    spec = importlib.util.spec_from_file_location("slack_credentials_under_test", SURFACE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CredentialStateRootTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load()
        cls.package_root = SURFACE.resolve().parents[2]

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-cred-")
        self.workspace = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _clear(self) -> dict:
        return {k: v for k, v in os.environ.items() if k not in WORKSPACE_VARS}

    def test_each_workspace_variable_is_honoured_in_order(self) -> None:
        for name in WORKSPACE_VARS:
            with patch.dict(os.environ, {**self._clear(), name: str(self.workspace)}, clear=True):
                self.assertEqual(self.mod._state_root(), self.workspace.resolve(),
                                 f"{name} should name the state root")

    def test_falls_back_to_the_historical_location_when_nothing_names_a_workspace(self) -> None:
        # Running the script bare must behave exactly as it did, not guess at a root.
        with patch.dict(os.environ, self._clear(), clear=True):
            self.assertEqual(self.mod._state_root(), self.package_root)

    def test_a_workspace_variable_naming_a_missing_directory_is_ignored(self) -> None:
        with patch.dict(os.environ,
                        {**self._clear(), "SIDEQUESTOR_WORKSPACE": str(self.workspace / "nope")},
                        clear=True):
            self.assertEqual(self.mod._state_root(), self.package_root)

    def test_an_existing_authorized_helper_wins_over_the_workspace(self) -> None:
        # The migration-safety case: an install that already has a Keychain-authorized
        # binary must keep using it, or macOS re-prompts and launchd never answers.
        legacy = self.package_root / "state" / "bin" / "yaas-keychain-helper"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("#!/bin/sh\nexit 0\n")
        legacy.chmod(legacy.stat().st_mode | stat.S_IXUSR)
        try:
            with patch.dict(os.environ,
                            {**self._clear(), "SIDEQUESTOR_WORKSPACE": str(self.workspace)},
                            clear=True):
                self.assertEqual(self.mod._keychain_helper_path(), legacy)
        finally:
            legacy.unlink()
            for parent in (legacy.parent, legacy.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break

    def test_a_fresh_install_compiles_into_the_workspace(self) -> None:
        # No legacy binary: the new location applies, so site-packages stays clean and a
        # read-only package directory is no longer a hard failure.
        with patch.dict(os.environ,
                        {**self._clear(), "SIDEQUESTOR_WORKSPACE": str(self.workspace)},
                        clear=True):
            self.assertEqual(self.mod._keychain_helper_path(),
                             self.workspace.resolve() / "state" / "bin" / "yaas-keychain-helper")


if __name__ == "__main__":
    unittest.main()
