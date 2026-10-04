"""The Slack Keychain helper and refresh lock are stable across workspaces/upgrades."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SURFACE = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
           / "surfaces" / "slack_credentials.py")
WORKSPACE_VARS = ("SIDEQUESTOR_WORKSPACE", "YAAS_WORKSPACE", "REPO_ROOT")
CONFIG_VARS = ("SIDEQUESTOR_CONFIG_HOME", "YAAS_CONFIG_HOME")


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
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = self.root / "config"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _clear(self) -> dict:
        removed = set(WORKSPACE_VARS + CONFIG_VARS)
        return {key: value for key, value in os.environ.items() if key not in removed}

    def _environment(self, **values: str) -> dict[str, str]:
        return {**self._clear(), "SIDEQUESTOR_CONFIG_HOME": str(self.config), **values}

    def test_each_workspace_variable_is_honoured_in_order(self) -> None:
        for name in WORKSPACE_VARS:
            with patch.dict(os.environ, self._environment(**{name: str(self.workspace)}),
                            clear=True):
                self.assertEqual(self.mod._state_root(), self.workspace.resolve())

    def test_falls_back_to_historical_state_root_without_a_workspace(self) -> None:
        with patch.dict(os.environ, self._environment(), clear=True):
            self.assertEqual(self.mod._state_root(), self.package_root)

    def test_helper_path_is_global_and_versioned_across_workspaces(self) -> None:
        expected = (self.config / "yaas" / "credentials" / "bin"
                    / "sidequestor-keychain-helper-v1")
        paths = []
        for name in ("one", "two"):
            workspace = self.root / name
            workspace.mkdir()
            with patch.dict(
                os.environ,
                self._environment(SIDEQUESTOR_WORKSPACE=str(workspace)),
                clear=True,
            ):
                paths.append(self.mod._keychain_helper_path())
        self.assertEqual(paths, [expected, expected])

    def test_legacy_config_home_alias_is_supported(self) -> None:
        environment = self._clear()
        environment["YAAS_CONFIG_HOME"] = str(self.config)
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                self.mod._keychain_helper_path(),
                self.config / "yaas" / "credentials" / "bin"
                / "sidequestor-keychain-helper-v1",
            )

    def test_refresh_lock_is_global_across_workspaces(self) -> None:
        paths = []
        for name in ("refresh-one", "refresh-two"):
            workspace = self.root / name
            workspace.mkdir()
            with patch.dict(
                os.environ,
                self._environment(SIDEQUESTOR_WORKSPACE=str(workspace)),
                clear=True,
            ), patch.object(self.mod, "MacOSKeychain", return_value=object()):
                paths.append(self.mod._default_credentials().lock.path)
        expected = self.config / "yaas" / "credentials" / "slack-oauth.lock"
        self.assertEqual(paths, [expected, expected])

    def test_install_copies_a_safe_legacy_helper_byte_for_byte(self) -> None:
        legacy = self.root / "legacy-helper"
        legacy.write_bytes(b"existing authorized helper\n")
        legacy.chmod(0o700)
        helper = self.config / "yaas" / "credentials" / "bin" / "helper-v1"
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "_legacy_helper_candidates", return_value=[legacy]), \
                patch.object(self.mod.subprocess, "run") as compile_helper:
            migrated_from = self.mod._install_global_helper(self.root / "source.c", helper)
        self.assertEqual(migrated_from, legacy)
        self.assertEqual(helper.read_bytes(), legacy.read_bytes())
        self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(stat.S_IMODE((self.config / "yaas").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.config / "yaas" / "credentials").stat().st_mode),
            0o700,
        )
        self.assertEqual(stat.S_IMODE(helper.parent.stat().st_mode), 0o700)
        compile_helper.assert_not_called()

    def test_legacy_candidates_include_every_registered_workspace_layout(self) -> None:
        other = self.root / "registered-workspace"
        (other / ".yaas").mkdir(parents=True)
        (other / ".yaas" / "instance.json").write_text("{}\n")
        registry = self.config / "yaas" / "instances.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps([{"path": str(other)}]) + "\n")
        expected = {
            other / "state" / "bin" / "yaas-keychain-helper",
            other / ".local" / "yaas-package" / "src" / "sidequestor"
            / "runtime" / "state" / "bin" / "yaas-keychain-helper",
        }
        with patch.dict(os.environ, self._environment(), clear=True):
            candidates = set(self.mod._legacy_helper_candidates())
        self.assertTrue(expected.issubset(candidates))

    def test_install_preserves_the_previous_resolution_order_not_atime(self) -> None:
        current = self.root / "current-helper"
        registered = self.root / "registered-helper"
        for path, contents, timestamp in (
            (current, b"currently selected identity", 1_000_000_000),
            (registered, b"newer but unrelated identity", 2_000_000_000),
        ):
            path.write_bytes(contents)
            path.chmod(0o700)
            os.utime(path, ns=(timestamp, timestamp))
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "_legacy_helper_candidates",
                             return_value=[current, registered]):
            migrated_from = self.mod._install_global_helper(self.root / "source.c", helper)
        self.assertEqual(migrated_from, current)
        self.assertEqual(helper.read_bytes(), b"currently selected identity")

    def test_install_never_replaces_an_existing_helper_identity(self) -> None:
        helper = self.config / "yaas" / "credentials" / "bin" / "helper-v1"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"immutable identity\n")
        helper.chmod(0o700)
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod.subprocess, "run") as compile_helper:
            migrated_from = self.mod._install_global_helper(self.root / "source.c", helper)
        self.assertIsNone(migrated_from)
        self.assertEqual(helper.read_bytes(), b"immutable identity\n")
        compile_helper.assert_not_called()

    def test_fresh_install_compiles_once_when_no_legacy_helper_exists(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)

        def compile_helper(command, **_kwargs):
            Path(command[-1]).write_bytes(b"fresh helper")
            return self.mod.subprocess.CompletedProcess(command, 0, "", "")

        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "_legacy_helper_candidates", return_value=[]), \
                patch.object(self.mod.subprocess, "run", side_effect=compile_helper) as run:
            migrated_from = self.mod._install_global_helper(self.root / "source.c", helper)
            self.mod._install_global_helper(self.root / "source.c", helper)
        self.assertIsNone(migrated_from)
        self.assertEqual(helper.read_bytes(), b"fresh helper")
        self.assertEqual(run.call_count, 1)

    def test_ready_marker_is_bound_to_the_exact_helper_bytes(self) -> None:
        helper = self.config / "yaas" / "credentials" / "bin" / "helper-v1"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"first identity")
        helper.chmod(0o700)
        with patch.dict(os.environ, self._environment(), clear=True):
            self.mod._mark_helper_ready(helper, credential_present=True)
            marker = json.loads(self.mod._helper_marker_path().read_text())
            self.assertTrue(self.mod._helper_ready(helper))
            self.assertTrue(marker["credential_present"])
            helper.write_bytes(b"different identity")
            self.assertFalse(self.mod._helper_ready(helper))

    def test_install_refuses_to_execute_a_helper_changed_after_verification(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"verified identity")
        helper.chmod(0o700)
        with patch.dict(os.environ, self._environment(), clear=True):
            self.mod._mark_helper_ready(helper, credential_present=True)
            helper.write_bytes(b"unexpected replacement")
            with self.assertRaises(self.mod.HelperUnavailableError):
                self.mod._install_global_helper(self.root / "source.c", helper)

    def test_repair_reads_twice_then_marks_the_exact_helper_ready(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"authorized identity")
        helper.chmod(0o700)

        class FakeKeychain:
            def __init__(fake_self):
                fake_self.source = self.root / "source.c"
                fake_self.helper = helper
                fake_self.reads = 0

            def read(fake_self, service, account):
                self.assertEqual((service, account),
                                 (self.mod.BUNDLE_SERVICE, self.mod.KEYCHAIN_ACCOUNT))
                fake_self.reads += 1
                return '{"access_token":"redacted"}'

        keychain = FakeKeychain()
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "MacOSKeychain", return_value=keychain), \
                patch.object(self.mod, "_install_global_helper", return_value=self.root / "legacy"), \
                patch.object(sys.stderr, "isatty", return_value=True):
            summary = self.mod.repair_keychain_helper()
        self.assertEqual(keychain.reads, 2)
        self.assertTrue(summary["ready"])
        self.assertNotIn("access_token", json.dumps(summary))

    def test_noninteractive_repair_never_touches_keychain_without_a_ready_marker(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"new identity")
        helper.chmod(0o700)

        class FakeKeychain:
            def __init__(fake_self):
                fake_self.source = self.root / "source.c"
                fake_self.helper = helper

            def read(fake_self, service, account):
                raise AssertionError("noninteractive migration must not trigger Keychain UI")

        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "MacOSKeychain", return_value=FakeKeychain()), \
                patch.object(self.mod, "_install_global_helper", return_value=self.root / "legacy"), \
                patch.object(sys.stderr, "isatty", return_value=False):
            with self.assertRaises(self.mod.HelperUnavailableError):
                self.mod.repair_keychain_helper()

    def test_repair_serializes_verification_with_the_global_refresh_lock(self) -> None:
        events = []

        class FakeKeychain:
            def __init__(fake_self):
                fake_self.source = self.root / "source.c"
                fake_self.helper = self.root / "helper"

        class RecordingLock:
            def __init__(fake_self, path):
                fake_self.name = Path(path).name

            def __enter__(fake_self):
                events.append(f"enter:{fake_self.name}")

            def __exit__(fake_self, *_args):
                events.append(f"exit:{fake_self.name}")

        def verify(_keychain, require_interactive=True):
            self.assertTrue(require_interactive)
            events.append("verify")
            return {"ready": True}

        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "MacOSKeychain", return_value=FakeKeychain()), \
                patch.object(self.mod, "_install_global_helper"), \
                patch.object(self.mod, "FileLock", RecordingLock), \
                patch.object(self.mod, "_verify_keychain_helper", side_effect=verify):
            self.assertEqual(self.mod.repair_keychain_helper(), {"ready": True})
        self.assertEqual(events, [
            "enter:slack-oauth.lock",
            f"enter:.{self.mod.HELPER_NAME}.repair.lock",
            "verify",
            f"exit:.{self.mod.HELPER_NAME}.repair.lock",
            "exit:slack-oauth.lock",
        ])

    def test_background_read_fails_closed_before_unready_helper_touches_keychain(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"not authorized yet")
        helper.chmod(0o700)
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "sys_platform", return_value="darwin"), \
                patch.object(self.mod.subprocess, "run") as run:
            keychain = self.mod.MacOSKeychain()
            with self.assertRaises(self.mod.HelperMigrationRequired):
                keychain.read(self.mod.BUNDLE_SERVICE, self.mod.KEYCHAIN_ACCOUNT)
        run.assert_not_called()

    def test_explicit_repair_may_read_with_an_unready_helper(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"foreground identity")
        helper.chmod(0o700)
        missing = self.mod.subprocess.CompletedProcess([], 44, "", "")
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "sys_platform", return_value="darwin"), \
                patch.object(self.mod.subprocess, "run", return_value=missing) as run:
            keychain = self.mod.MacOSKeychain(allow_unready=True)
            self.assertIsNone(
                keychain.read(self.mod.BUNDLE_SERVICE, self.mod.KEYCHAIN_ACCOUNT))
        run.assert_called_once()

    def test_store_does_not_fall_back_to_legacy_when_migration_is_required(self) -> None:
        class FakeKeychain:
            def __init__(fake_self):
                fake_self.calls = []

            def read(fake_self, service, account):
                fake_self.calls.append((service, account))
                raise self.mod.HelperMigrationRequired("repair required")

        keychain = FakeKeychain()
        store = self.mod.KeychainCredentialStore(keychain)
        with self.assertRaises(self.mod.HelperMigrationRequired):
            store.load()
        self.assertEqual(keychain.calls, [
            (self.mod.BUNDLE_SERVICE, self.mod.KEYCHAIN_ACCOUNT),
        ])

    def test_fresh_oauth_install_uses_and_verifies_the_stable_helper(self) -> None:
        helper = (self.config / "yaas" / "credentials" / "bin"
                  / self.mod.HELPER_NAME)
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"fresh stable identity")
        helper.chmod(0o700)

        class FakeKeychain:
            values = {}
            instances = []

            def __init__(fake_self, allow_unready=False):
                fake_self.allow_unready = allow_unready
                fake_self.source = self.root / "source.c"
                fake_self.helper = helper
                fake_self.reads = 0
                FakeKeychain.instances.append(fake_self)

            def write(fake_self, service, account, value):
                self.assertTrue(fake_self.allow_unready)
                FakeKeychain.values[(service, account)] = value

            def read(fake_self, service, account):
                self.assertTrue(fake_self.allow_unready)
                fake_self.reads += 1
                return FakeKeychain.values.get((service, account))

        response = {
            "ok": True,
            "authed_user": {
                "access_token": "redacted-access",
                "refresh_token": "redacted-refresh",
                "expires_in": 3600,
                "id": "U123",
            },
            "team": {"id": "T123"},
        }
        with patch.dict(os.environ, self._environment(), clear=True), \
                patch.object(self.mod, "MacOSKeychain", FakeKeychain), \
                patch.object(self.mod.sys, "stdin", StringIO(json.dumps(response))), \
                redirect_stdout(StringIO()):
            self.assertEqual(self.mod.main(["install", "client-id"]), 0)
            self.assertTrue(self.mod._helper_ready(helper))
        self.assertEqual([instance.allow_unready for instance in FakeKeychain.instances],
                         [True, True])
        self.assertEqual(FakeKeychain.instances[1].reads, 2)


if __name__ == "__main__":
    unittest.main()
