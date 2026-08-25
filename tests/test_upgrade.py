from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor.upgrade import github_requirement, run_upgrade
from sidequestor.workspace import init_workspace


class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-upgrade-")
        self.root = Path(self.temp.name)
        self.config_patch = patch.dict(
            "os.environ", {"SIDEQUESTOR_CONFIG_HOME": str(self.root / "config")}
        )
        self.config_patch.start()
        self.workspace = init_workspace(self.root / "workspace")

    def tearDown(self) -> None:
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
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--force-reinstall", commands[0])
        self.assertEqual(
            commands[0][-1],
            "sidequestor @ git+https://github.com/circlefin/sidequestor.git@upgrade-command",
        )
        self.assertEqual([command[-1] for command in commands[1:]], [
            "sync-resources", "doctor", "start",
        ])

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
