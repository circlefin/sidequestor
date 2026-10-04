from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.job_path import login_shell_path, resolve_job_path
from sidequestor.launchd import install_production, production_agent_status
from sidequestor.native import _environment
from sidequestor.workspace import init_workspace


def executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


class JobPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-job-path-")
        self.root = Path(self.temp.name)
        self.config_patch = patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": str(self.root / "config")})
        self.config_patch.start()
        os.environ.pop("SIDEQUESTOR_PATH", None)
        self.workspace = init_workspace(self.root / "workspace")
        self.venv = self.root / "tool-env"
        (self.venv / "pyvenv.cfg").parent.mkdir(parents=True)
        (self.venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.python = executable(self.venv / "bin" / "python")
        self.dispatch_patch = patch("sidequestor.job_path._dispatch_dirs", return_value=[])
        self.dispatch_patch.start()

    def tearDown(self) -> None:
        self.dispatch_patch.stop()
        self.config_patch.stop()
        self.temp.cleanup()

    def env_file(self, text: str) -> None:
        self.workspace.env_file.write_text(text)

    def test_installation_bin_comes_first_and_caller_path_is_kept_in_order(self) -> None:
        agents = self.root / "agents"
        executable(agents / "codex")
        caller = os.pathsep.join([str(agents), "", "relative/bin", "/usr/bin", str(agents)])
        with patch.dict(os.environ, {"PATH": caller}), \
                patch("sidequestor.job_path.login_shell_path") as probe:
            resolved = resolve_job_path(self.workspace, self.python)

        self.assertEqual(
            resolved.value.split(os.pathsep),
            [str(self.venv / "bin"), str(agents), "/usr/bin", "/bin", "/usr/sbin", "/sbin"],
        )
        self.assertEqual(resolved.agent_binary, str(agents / "codex"))
        probe.assert_not_called()

    def test_interpreter_outside_a_virtualenv_adds_nothing_in_front(self) -> None:
        with patch.dict(os.environ, {"PATH": "/usr/bin"}), \
                patch("sidequestor.job_path.login_shell_path", return_value=[]):
            resolved = resolve_job_path(self.workspace, "/usr/bin/python3")

        self.assertEqual(resolved.value.split(os.pathsep)[0], "/usr/bin")

    def test_missing_agent_falls_back_to_login_shell_path(self) -> None:
        shell_bin = self.root / "shell-bin"
        executable(shell_bin / "cursor-agent")
        self.env_file("SIDEQUESTOR_AGENT=cursor\n")
        with patch.dict(os.environ, {"PATH": "/usr/bin"}), \
                patch("sidequestor.job_path.login_shell_path", return_value=[str(shell_bin)]):
            resolved = resolve_job_path(self.workspace, self.python)

        parts = resolved.value.split(os.pathsep)
        self.assertLess(parts.index("/usr/bin"), parts.index(str(shell_bin)))
        self.assertEqual(resolved.agent, "cursor")
        self.assertEqual(resolved.agent_binary, str(shell_bin / "cursor-agent"))
        self.assertTrue(resolved.login_shell_used)

    def test_explicit_path_in_dotenv_replaces_caller_path_without_probing(self) -> None:
        pinned = self.root / "pinned"
        executable(pinned / "codex")
        self.env_file(f"SIDEQUESTOR_PATH={pinned}\n")
        with patch.dict(os.environ, {"PATH": "/somewhere/else"}), \
                patch("sidequestor.job_path.login_shell_path") as probe:
            resolved = resolve_job_path(self.workspace, self.python)

        self.assertNotIn("/somewhere/else", resolved.value)
        self.assertIn(str(pinned), resolved.value.split(os.pathsep))
        self.assertEqual(resolved.agent_binary, str(pinned / "codex"))
        probe.assert_not_called()

    def test_login_shell_probe_ignores_startup_noise(self) -> None:
        shell = executable(self.root / "noisy-shell", (
            "#!/bin/sh\n"
            "echo 'welcome banner'\n"
            "echo 'rc warning' >&2\n"
            "printf '%s' '/from/login:relative:/also/login' > \"$SIDEQUESTOR_PATH_PROBE\"\n"
        ))
        with patch.dict(os.environ, {"SHELL": str(shell)}):
            self.assertEqual(login_shell_path(), ["/from/login", "/also/login"])

    def test_login_shell_probe_gives_up_on_a_hanging_shell(self) -> None:
        shell = executable(self.root / "hanging-shell", "#!/bin/sh\nexec sleep 30\n")
        started = time.monotonic()
        with patch.dict(os.environ, {"SHELL": str(shell)}):
            self.assertEqual(login_shell_path(timeout=0.5), [])
        self.assertLess(time.monotonic() - started, 10)

    def test_runtime_scripts_see_this_installation_first(self) -> None:
        with patch("sidequestor.native.sys.executable", str(self.python)), \
                patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            environment = _environment(self.workspace)

        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(self.venv / "bin"))
        self.assertIn("/usr/bin", environment["PATH"].split(os.pathsep))

    def test_installed_plists_record_the_path_and_doctor_reads_it_back(self) -> None:
        agents = self.root / "agents"
        executable(agents / "codex")
        launch_agents = self.root / "LaunchAgents"
        launchctl = lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1 if command[1] == "print" else 0, "", "")
        with patch.dict(os.environ, {"PATH": str(agents)}), \
                patch("sidequestor.job_path.login_shell_path", return_value=[]), \
                patch("sidequestor.launchd._production_root", return_value=launch_agents), \
                patch("sidequestor.launchd.subprocess.run", side_effect=launchctl):
            manifest = install_production(self.workspace, self.python)
            status = production_agent_status(self.workspace)

        self.assertEqual(manifest["agent"]["binary"], str(agents / "codex"))
        for job in manifest["jobs"].values():
            with open(job["plist"], "rb") as handle:
                recorded = plistlib.load(handle)["EnvironmentVariables"]["PATH"]
            self.assertEqual(recorded.split(os.pathsep)[:2], [str(self.venv / "bin"), str(agents)])
        self.assertEqual(status, ("codex", str(agents / "codex")))


if __name__ == "__main__":
    unittest.main()
