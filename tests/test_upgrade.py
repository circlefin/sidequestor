from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor.upgrade import (
    Installer, _select_installer, detect_installer, github_requirement, installer_command,
    run_upgrade,
)
from sidequestor.workspace import init_workspace


class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-upgrade-")
        self.root = Path(self.temp.name)
        self.config_patch = patch.dict(
            "os.environ", {"SIDEQUESTOR_CONFIG_HOME": str(self.root / "config")}
        )
        self.config_patch.start()
        # The suite's own interpreter may live in a pip-less uv environment; these tests
        # exercise the plain-venv path explicitly.
        self.installer_patch = patch(
            "sidequestor.upgrade.detect_installer", return_value=Installer("pip"),
        )
        self.installer_patch.start()
        self.workspace = init_workspace(self.root / "workspace")

    def tearDown(self) -> None:
        self.installer_patch.stop()
        self.config_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _success(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0)

    def test_github_requirement_accepts_repository_and_explicit_ref(self) -> None:
        self.assertEqual(
            github_requirement(
                "https://github.com/circlefin/sidequestor.git", "feature/safe-upgrade"
            ),
            "sidequestor @ git+https://github.com/circlefin/sidequestor.git@feature/safe-upgrade",
        )
        self.assertEqual(
            github_requirement("https://github.com/circlefin/sidequestor/", "abc1234"),
            "sidequestor @ git+https://github.com/circlefin/sidequestor.git@abc1234",
        )

    def test_github_requirement_rejects_ambiguous_or_unsafe_inputs(self) -> None:
        invalid = (
            ("http://github.com/circlefin/sidequestor", "main"),
            ("https://example.com/circlefin/sidequestor", "main"),
            ("https://token@github.com/circlefin/sidequestor", "main"),
            ("https://github.com/circlefin/sidequestor/tree/main", "main"),
            ("https://github.com/circlefin/sidequestor", "../main"),
            ("https://github.com/circlefin/sidequestor", "main^{commit}"),
        )
        for source, ref in invalid:
            with self.subTest(source=source, ref=ref), self.assertRaises(ValueError):
                github_requirement(source, ref)

    def test_pypi_upgrade_syncs_and_validates_in_fresh_process(self) -> None:
        output = StringIO()
        with patch("sidequestor.upgrade.production_status", return_value=None), \
                patch("sidequestor.upgrade.subprocess.run", side_effect=self._success) as run, \
                redirect_stdout(output):
            self.assertEqual(run_upgrade(self.workspace, []), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands[0],
            [sys.executable, "-m", "pip", "install", "--upgrade", "sidequestor"],
        )
        self.assertEqual(commands[1][-1], "sync-resources")
        self.assertEqual(commands[2][-1], "doctor")
        self.assertTrue(all(str(self.workspace.root) in command for command in commands[1:]))
        self.assertNotIn("PYTHONPATH", run.call_args_list[1].kwargs["env"])
        self.assertIn("Sidequestor upgrade complete", output.getvalue())

    def test_git_upgrade_force_reinstalls_and_restores_running_jobs(self) -> None:
        output = StringIO()
        manifest = {"running": True}
        with patch("sidequestor.upgrade.production_status", return_value=manifest), \
                patch("sidequestor.upgrade.read_dashboard_port", return_value=43123), \
                patch("sidequestor.upgrade.wait_for_dashboard_port", return_value=True) as wait, \
                patch("sidequestor.upgrade.stop_production", return_value=True) as stop, \
                patch("sidequestor.upgrade.subprocess.run", side_effect=self._success) as run, \
                redirect_stdout(output):
            self.assertEqual(
                run_upgrade(self.workspace, [
                    "--source", "https://github.com/circlefin/sidequestor",
                    "--branch", "upgrade-command", "--yes",
                ]),
                0,
            )

        stop.assert_called_once_with(self.workspace)
        wait.assert_called_once_with(43123)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--force-reinstall", commands[0])
        self.assertEqual(
            commands[0][-1],
            "sidequestor @ git+https://github.com/circlefin/sidequestor.git@upgrade-command",
        )
        self.assertEqual([command[-1] for command in commands[1:3]], [
            "sync-resources", "doctor",
        ])
        self.assertEqual(commands[3][-3:], ["start", "--dashboard-port", "43123"])
        restart_kwargs = run.call_args_list[3].kwargs
        self.assertNotIn("capture_output", restart_kwargs)
        self.assertNotIn("stdout", restart_kwargs)
        self.assertNotIn("stderr", restart_kwargs)

    def test_install_failure_attempts_to_restore_previously_running_jobs(self) -> None:
        results = iter((
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0),
        ))
        with patch("sidequestor.upgrade.production_status", return_value={"running": True}), \
                patch("sidequestor.upgrade.stop_production", return_value=True), \
                patch(
                    "sidequestor.upgrade.subprocess.run",
                    side_effect=lambda *_a, **_k: next(results),
                ) as run, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(run_upgrade(self.workspace, []), 1)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][2:5], ["pip", "install", "--upgrade"])
        self.assertEqual(commands[1][-1], "start")
        self.assertNotIn("sync-resources", [command[-1] for command in commands])

    def test_sync_failure_leaves_previously_running_jobs_stopped(self) -> None:
        results = iter((
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 2),
        ))
        with patch("sidequestor.upgrade.production_status", return_value={"running": True}), \
                patch("sidequestor.upgrade.stop_production", return_value=True), \
                patch(
                    "sidequestor.upgrade.subprocess.run",
                    side_effect=lambda *_a, **_k: next(results),
                ) as run, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(run_upgrade(self.workspace, []), 2)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[-1] for command in commands[1:]], ["sync-resources"])

    def test_upgrade_does_not_restart_on_a_different_dashboard_port(self) -> None:
        with patch("sidequestor.upgrade.production_status", return_value={"running": True}), \
                patch("sidequestor.upgrade.read_dashboard_port", return_value=43123), \
                patch("sidequestor.upgrade.wait_for_dashboard_port", return_value=False), \
                patch("sidequestor.upgrade.stop_production", return_value=True), \
                patch("sidequestor.upgrade.subprocess.run", side_effect=self._success) as run, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(run_upgrade(self.workspace, []), 1)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[-1] for command in commands[1:]], [
            "sync-resources", "doctor",
        ])

    def test_no_restart_preserves_an_explicitly_stopped_post_upgrade_state(self) -> None:
        with patch("sidequestor.upgrade.production_status", return_value={"running": True}), \
                patch("sidequestor.upgrade.stop_production", return_value=True), \
                patch("sidequestor.upgrade.subprocess.run", side_effect=self._success) as run, \
                redirect_stdout(StringIO()):
            self.assertEqual(run_upgrade(self.workspace, ["--no-restart"]), 0)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[-1] for command in commands[1:]], [
            "sync-resources", "doctor",
        ])

    def test_git_upgrade_requires_yes_without_a_terminal(self) -> None:
        with patch("sidequestor.upgrade.sys.stdin.isatty", return_value=False), \
                self.assertRaisesRegex(SystemExit, "require --yes"):
            run_upgrade(self.workspace, [
                "--source", "https://github.com/circlefin/sidequestor",
                "--ref", "main",
            ])

    def test_source_requires_ref(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            run_upgrade(self.workspace, ["--source", "https://github.com/circlefin/sidequestor"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()


class InstallerDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-installer-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def pipx_env(self, package_or_url: str = "sidequestor[telegram, x]") -> Path:
        prefix = self.root / "pipx" / "venvs" / "sidequestor"
        prefix.mkdir(parents=True)
        (prefix / "pipx_metadata.json").write_text(json.dumps({"main_package": {
            "package": "sidequestor", "package_or_url": package_or_url,
        }}))
        return prefix

    def uv_env(self) -> Path:
        prefix = self.root / "uv" / "tools" / "sidequestor"
        prefix.mkdir(parents=True)
        (prefix / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "sidequestor", extras = ["telegram"] }]\n'
        )
        return prefix

    def test_pipx_environment_is_detected_with_its_extras_and_home(self) -> None:
        prefix = self.pipx_env()
        self.assertEqual(
            detect_installer(prefix),
            Installer("pipx", "sidequestor", ("telegram", "x"),
                      {"PIPX_HOME": str(self.root / "pipx")}),
        )

    def test_uv_tool_environment_is_detected_without_pip(self) -> None:
        prefix = self.uv_env()
        self.assertEqual(
            detect_installer(prefix),
            Installer("uv", "sidequestor", ("telegram",),
                      {"UV_TOOL_DIR": str(self.root / "uv" / "tools")}),
        )

    def test_pipx_git_upgrade_preserves_the_recorded_suffix(self) -> None:
        prefix = self.root / "pipx" / "venvs" / "sidequestor-test"
        prefix.mkdir(parents=True)
        (prefix / "pipx_metadata.json").write_text(json.dumps({"main_package": {
            "package": "sidequestor", "package_or_url": "sidequestor[telegram]",
            "suffix": "-test",
        }}))
        installer = detect_installer(prefix)
        self.assertIsNotNone(installer)
        git = "sidequestor @ git+https://github.com/circlefin/sidequestor.git@main"
        with patch("sidequestor.upgrade.shutil.which", return_value="/bin/pipx"):
            self.assertEqual(installer_command(installer, git_requirement=git), [
                "/bin/pipx", "install", "--force", "--suffix=-test",
                "sidequestor[telegram] @ git+https://github.com/circlefin/sidequestor.git@main",
            ])
            self.assertEqual(installer_command(installer), [
                "/bin/pipx", "upgrade", "sidequestor-test",
            ])
        self.assertEqual(installer.env, {"PIPX_HOME": str(self.root / "pipx")})

    def test_receipt_for_another_package_is_not_ownership(self) -> None:
        prefix = self.pipx_env()
        (prefix / "pipx_metadata.json").write_text(
            json.dumps({"main_package": {"package": "other"}})
        )
        self.assertIsNone(detect_installer(prefix))

    def test_broken_manager_receipt_does_not_fall_back_to_pip(self) -> None:
        prefix = self.pipx_env()
        (prefix / "pipx_metadata.json").write_text("{broken")
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.sys.base_prefix", "/usr/bin"), \
                patch("sidequestor.upgrade.importlib.util.find_spec", return_value=object()):
            self.assertIsNone(detect_installer())

    def test_missing_manager_receipt_does_not_fall_back_to_pip(self) -> None:
        prefix = self.root / "uv" / "tools" / "sidequestor"
        prefix.mkdir(parents=True)
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.sys.base_prefix", "/usr/bin"), \
                patch("sidequestor.upgrade.importlib.util.find_spec", return_value=object()):
            self.assertIsNone(detect_installer())

    def test_custom_manager_directory_without_receipt_is_not_plain_pip(self) -> None:
        prefix = self.root / "custom" / "sidequestor"
        prefix.mkdir(parents=True)
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.sys.base_prefix", "/usr/bin"), \
                patch("sidequestor.upgrade.importlib.util.find_spec", return_value=object()), \
                patch("sidequestor.upgrade._manager_root",
                      side_effect=lambda name: prefix.parent.resolve() if name == "uv" else None):
            self.assertIsNone(detect_installer())

    def test_override_requires_the_manager_to_target_this_environment(self) -> None:
        prefix = self.root / "uv" / "tools" / "sidequestor"
        prefix.mkdir(parents=True)
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade._manager_root", return_value=self.root / "elsewhere"):
            with self.assertRaisesRegex(ValueError, "cannot verify"):
                _select_installer("uv")
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade._manager_root", return_value=prefix.parent.resolve()):
            self.assertEqual(
                _select_installer("uv"),
                Installer("uv", "sidequestor", env={"UV_TOOL_DIR": str(prefix.parent.resolve())}),
            )

    def test_override_cannot_replace_known_owner(self) -> None:
        with patch("sidequestor.upgrade.detect_installer", return_value=Installer("pip")):
            with self.assertRaisesRegex(ValueError, "managed by pip, not uv"):
                _select_installer("uv")

    def test_git_override_requires_receipt_to_preserve_installed_extras(self) -> None:
        prefix = self.root / "tools" / "sidequestor"
        prefix.mkdir(parents=True)
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade._manager_root", return_value=prefix.parent.resolve()), \
                self.assertRaisesRegex(ValueError, "cannot read installed extras"):
            _select_installer("uv", needs_extras=True)

    def test_pip_override_accepts_plain_pip_outside_detectable_venvs(self) -> None:
        # ~/venvs/sidequestor and a pyenv or --user interpreter are refused by detection,
        # but the pre-0.1.37 pip upgrader handled them, so the explicit choice must work.
        venv = self.root / "venvs" / "sidequestor"
        venv.mkdir(parents=True)
        base = self.root / "pyenv" / "3.12"
        base.mkdir(parents=True)
        for prefix, base_prefix in ((venv, "/usr/bin"), (base, str(base))):
            with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                    patch("sidequestor.upgrade.sys.base_prefix", base_prefix), \
                    patch("sidequestor.upgrade.importlib.util.find_spec", return_value=object()), \
                    patch("sidequestor.upgrade._manager_root", return_value=None):
                self.assertIsNone(detect_installer())
                self.assertEqual(_select_installer("pip"), Installer("pip"))

    def test_pip_override_refuses_when_a_manager_claims_the_environment(self) -> None:
        prefix = self.uv_env()
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade.importlib.util.find_spec", return_value=object()), \
                self.assertRaisesRegex(ValueError, "cannot verify that pip"):
            _select_installer("pip")
        (prefix / "uv-receipt.toml").unlink()
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade.importlib.util.find_spec", return_value=object()), \
                patch("sidequestor.upgrade._manager_root",
                      side_effect=lambda name: prefix.parent.resolve() if name == "uv" else None), \
                self.assertRaisesRegex(ValueError, "cannot verify that pip"):
            _select_installer("pip")
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade.importlib.util.find_spec", return_value=None), \
                patch("sidequestor.upgrade._manager_root", return_value=None), \
                self.assertRaisesRegex(ValueError, "cannot verify that pip"):
            _select_installer("pip")

    def test_override_rejects_a_receipt_for_another_package(self) -> None:
        prefix = self.uv_env()
        (prefix / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "other-tool" }]\n'
        )
        with patch("sidequestor.upgrade.sys.prefix", str(prefix)), \
                patch("sidequestor.upgrade.detect_installer", return_value=None), \
                self.assertRaisesRegex(ValueError, "receipt does not identify"):
            _select_installer("uv")

    def test_manager_commands_preserve_extras_and_prerelease_intent(self) -> None:
        pipx = Installer("pipx", "sidequestor", ("telegram",))
        uv = Installer("uv", "sidequestor", ("telegram",))
        git = "sidequestor @ git+https://github.com/circlefin/sidequestor.git@main"
        with patch("sidequestor.upgrade.shutil.which", side_effect=lambda name: f"/bin/{name}"):
            self.assertEqual(installer_command(pipx), ["/bin/pipx", "upgrade", "sidequestor"])
            self.assertEqual(
                installer_command(pipx, pre=True),
                ["/bin/pipx", "upgrade", "--pip-args=--pre", "sidequestor"],
            )
            self.assertEqual(
                installer_command(uv, pre=True),
                ["/bin/uv", "tool", "upgrade", "--prerelease", "allow", "sidequestor"],
            )
            self.assertEqual(installer_command(pipx, git_requirement=git), [
                "/bin/pipx", "install", "--force",
                "sidequestor[telegram] @ git+https://github.com/circlefin/sidequestor.git@main",
            ])
            self.assertEqual(installer_command(uv, git_requirement=git), [
                "/bin/uv", "tool", "install", "--force", "--reinstall-package", "sidequestor",
                "sidequestor[telegram] @ git+https://github.com/circlefin/sidequestor.git@main",
            ])

    def test_missing_manager_is_rejected(self) -> None:
        with patch("sidequestor.upgrade.shutil.which", return_value=None), \
                self.assertRaisesRegex(ValueError, "managed by uv"):
            installer_command(Installer("uv"))


class ManagedUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-managed-upgrade-")
        self.root = Path(self.temp.name)
        self.config_patch = patch.dict(
            "os.environ", {"SIDEQUESTOR_CONFIG_HOME": str(self.root / "config")}
        )
        self.config_patch.start()
        self.workspace = init_workspace(self.root / "workspace")
        self.other = init_workspace(self.root / "other")

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.temp.cleanup()

    def test_unknown_installer_fails_before_stopping_jobs(self) -> None:
        error = StringIO()
        with patch("sidequestor.upgrade.detect_installer", return_value=None), \
                patch("sidequestor.upgrade.production_status", return_value={"running": True}), \
                patch("sidequestor.upgrade.stop_production") as stop, \
                patch("sidequestor.upgrade.subprocess.run") as run, \
                redirect_stderr(error), redirect_stdout(StringIO()):
            self.assertEqual(run_upgrade(self.workspace, []), 1)

        stop.assert_not_called()
        run.assert_not_called()
        self.assertIn("--installer", error.getvalue())

    def test_uv_upgrade_targets_the_owning_tool_dir_and_names_other_running_workspaces(self) -> None:
        installer = Installer("uv", "sidequestor", (), {"UV_TOOL_DIR": "/tools"})
        python = os.path.abspath(sys.executable)

        def status(workspace):
            if workspace.root == self.other.root:
                return {"running": True, "python": python}
            return None

        output = StringIO()
        with patch("sidequestor.upgrade.detect_installer", return_value=installer), \
                patch("sidequestor.upgrade.shutil.which", return_value="/bin/uv"), \
                patch("sidequestor.upgrade.production_status", side_effect=status), \
                patch("sidequestor.upgrade.subprocess.run",
                      side_effect=lambda command, **_k: subprocess.CompletedProcess(command, 0)) as run, \
                redirect_stdout(output):
            self.assertEqual(run_upgrade(self.workspace, []), 0)

        self.assertEqual(run.call_args_list[0].args[0], ["/bin/uv", "tool", "upgrade", "sidequestor"])
        self.assertEqual(run.call_args_list[0].kwargs["env"]["UV_TOOL_DIR"], "/tools")
        self.assertIn(f"sq --workspace {self.other.root} start", output.getvalue())
