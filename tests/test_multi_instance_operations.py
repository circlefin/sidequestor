"""Exercise real instance resolution and lifecycle writes with a fake launchctl."""

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, chdir, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor import cli, launchd
from sidequestor.dashboard import saved_dashboard_port
from sidequestor.workspace import default_workspace, init_workspace, set_default_workspace


class MultiInstanceOperationsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        raw = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="sq-multi-instance-"))
        self.root = Path(raw).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.home), "SIDEQUESTOR_CONFIG_HOME": str(self.root / "config"),
            "YAAS_CONFIG_HOME": str(self.root / "config"),
            "SIDEQUESTOR_WORKSPACE": "", "YAAS_WORKSPACE": "",
        }))
        self.workspaces = [init_workspace(self.root / name, name) for name in ("A", "B", "C")]
        self.agents = self.home / "Library/LaunchAgents"
        self.loaded = set()
        self.actions = []
        self.violations = []
        self.addCleanup(lambda: self.assertEqual(self.violations, []))
        self.allowed = {f"com.sidequestor.{ws.instance_id}.{job}"
                        for ws in self.workspaces for job in launchd.PRODUCTION_JOB_NAMES}
        self.stack.enter_context(patch("sidequestor.launchd._production_root", return_value=self.agents))
        self.stack.enter_context(patch("sidequestor.launchd.subprocess.run", side_effect=self.launchctl))
        for function in ("Popen", "check_output", "call", "check_call"):
            self.stack.enter_context(patch(f"subprocess.{function}", side_effect=self.forbidden_operation))
        self.stack.enter_context(patch("sidequestor.job_path.login_shell_path", return_value=[]))
        self.stack.enter_context(patch("sidequestor.launchd.build_info", return_value={
            "version": "test-build", "commit_full": "fixture", "ref": "fixture",
        }))
        self.stack.enter_context(patch("sidequestor.cli._ensure_keychain_helper", return_value=0))
        self.stack.enter_context(patch("sidequestor.setup_worker.report_worker_version"))
        self.stack.enter_context(patch("sidequestor.cli._sync_resources_if_version_drifted"))
        self.stack.enter_context(patch("sidequestor.dashboard.wait_for_dashboard_port", return_value=True))
        self.stack.enter_context(patch("sidequestor.cli.wait_for_dashboard_url", return_value="http://127.0.0.1:18000"))
        self.stack.enter_context(patch("sidequestor.dashboard.stop_dashboard_process", return_value=False))
        self.stack.enter_context(patch("sidequestor.cli.stop_dashboard_process", return_value=False))
        self.stack.enter_context(patch("sidequestor.launchd.os.kill", side_effect=self.forbidden_operation))
        self.stack.enter_context(patch("os.killpg", side_effect=self.forbidden_operation))
        for index, ws in enumerate(self.workspaces):
            ws.env_file.write_text(f"SIDEQUESTOR_CHECKER_CONNECTORS=\nOWNER={ws.display_name}\n")
            saved_dashboard_port(ws, 18000 + index)
            launchd.install_production(ws, Path(sys.executable))
        set_default_workspace(self.workspaces[0])
        self.actions.clear()

    def forbidden_operation(self, *_args, **_kwargs):
        self.violations.append("Unexpected process execution or signal")
        raise AssertionError(self.violations[-1])

    def launchctl(self, command, **kwargs):
        if command[0] != "launchctl":
            self.forbidden_operation()
        action = command[1]
        if action == "bootstrap":
            path = Path(command[3])
            self.assertEqual(path.parent, self.agents)
            data = plistlib.loads(path.read_bytes())
            label = data["Label"]
            self.assertIn(label, self.allowed)
            owner = next(ws for ws in self.workspaces if ws.instance_id in label)
            self.assertEqual(data["WorkingDirectory"], str(owner.root))
            self.assertEqual(data["EnvironmentVariables"]["SIDEQUESTOR_WORKSPACE"], str(owner.root))
            self.loaded.add(label)
            code = 0
            self.actions.append((action, label))
        else:
            label = command[2].split("/", 2)[-1]
            self.assertIn(label, self.allowed)
            if action == "print":
                code = 0 if label in self.loaded else 1
            elif action == "bootout":
                code = 0 if label in self.loaded else 1
                self.loaded.discard(label)
                self.actions.append((action, label))
            else:
                self.fail(f"Unexpected launchctl operation: {action}")
        return subprocess.CompletedProcess(command, code, "state = running\n" if code == 0 else "", "")

    def snapshot(self, workspace):
        return {str(p.relative_to(workspace.root)): p.read_bytes()
                for p in workspace.root.rglob("*") if p.is_file()}

    def invoke(self, arguments):
        with redirect_stdout(StringIO()):
            return cli.main(arguments)

    def test_stop_and_resume_preserve_every_other_instance(self):
        target = self.workspaces[1]
        others = {ws: self.snapshot(ws) for ws in self.workspaces if ws != target}
        env = {"SIDEQUESTOR_WORKSPACE": str(self.workspaces[2].root)}
        with patch.dict(os.environ, env), chdir(self.workspaces[0].root):
            for _ in range(2):
                self.assertEqual(self.invoke(["stop", target.instance_id]), 0)
            self.assertFalse(launchd.production_status(target)["running"])
            self.assertEqual(self.invoke(["--instance", target.instance_id, "resume"]), 0)
        self.assertTrue(launchd.production_status(target)["running"])
        self.assertEqual(saved_dashboard_port(target), 18001)
        self.assertEqual(len(self.loaded), 9)
        self.assertTrue(all(target.instance_id in label for _, label in self.actions))
        for ws, before in others.items():
            self.assertEqual(self.snapshot(ws), before)
            self.assertTrue(launchd.production_is_running(ws))
        self.assertEqual(default_workspace(), self.workspaces[0])

    def test_start_running_instance_is_idempotent_and_pause_is_targeted(self):
        target = self.workspaces[2]
        before = {ws: self.snapshot(ws) for ws in self.workspaces}
        self.assertEqual(self.invoke(["--workspace", str(target.root), "start"]), 0)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.invoke(["--workspace", str(target.root), "pause"]), 0)
        self.assertEqual(len(self.loaded), 6)
        self.assertTrue(all(target.instance_id in label for _, label in self.actions))
        for ws in self.workspaces[:2]:
            self.assertEqual(self.snapshot(ws), before[ws])

    def test_port_change_cannot_interrupt_another_running_instance(self):
        target = self.workspaces[1]
        before = {ws: self.snapshot(ws) for ws in self.workspaces}
        self.assertEqual(self.invoke(["--instance", target.instance_id, "start",
                                      "--dashboard-port", "18000"]), 1)
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)
        for ws in self.workspaces:
            self.assertEqual(self.snapshot(ws), before[ws])

    def test_registered_runtime_forwarding_keeps_target_and_saved_port(self):
        target = self.workspaces[1]
        executable = self.root / "different-runtime/bin/python"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture executable; never run")
        executable.chmod(0o755)
        path = target.yaas_dir / "launchd/production.json"
        manifest = json.loads(path.read_text())
        manifest["python"] = str(executable)
        path.write_text(json.dumps(manifest))
        forwarded = []
        def run(command, **kwargs):
            if command[0] == str(executable):
                forwarded.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            return self.launchctl(command, **kwargs)
        with patch("sidequestor.cli.subprocess.run", side_effect=run), \
                patch.dict(os.environ, {"SIDEQUESTOR_WORKSPACE": str(self.workspaces[2].root)}), \
                chdir(self.workspaces[0].root):
            self.assertEqual(self.invoke(["--instance", target.instance_id, "start"]), 0)
        self.assertEqual(forwarded[-1], [str(executable), "-P", "-m", "sidequestor",
                                         "--workspace", str(target.root), "start",
                                         "--dashboard-port", "18001"])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)

    def test_unknown_stale_and_conflicting_selectors_cannot_touch_jobs(self):
        registry = self.root / "config/yaas/instances.json"
        rows = json.loads(registry.read_text())
        rows.append({"instance_id": "stale-id", "path": str(self.workspaces[1].root)})
        registry.write_text(json.dumps(rows))
        registry_before = registry.read_bytes()
        before = {ws: self.snapshot(ws) for ws in self.workspaces}
        for command in ("start", "stop", "pause", "resume"):
            for selector in (["--instance", "unknown"], ["--instance", "stale-id"],
                             ["--workspace", str(self.workspaces[0].root),
                              "--instance", self.workspaces[1].instance_id]):
                with self.subTest(command=command, selector=selector), \
                        self.assertRaisesRegex(SystemExit, "not found|stale|choose either"):
                    self.invoke([*selector, command])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)
        self.assertEqual(registry.read_bytes(), registry_before)
        for ws in self.workspaces:
            self.assertEqual(self.snapshot(ws), before[ws])
        with self.assertRaisesRegex(SystemExit, "choose either"):
            self.invoke(["--workspace", str(self.workspaces[0].root),
                         "--instance", self.workspaces[1].instance_id, "--help"])

    def test_targeting_matrix_respects_precedence_with_three_instances(self):
        selected = []
        def record(workspace, *_args):
            selected.append(workspace.instance_id)
            return 0
        count = 0
        with patch.object(cli, "_cmd_start", side_effect=record), \
                patch.object(cli, "_cmd_stop", side_effect=record), \
                patch.object(cli, "_cmd_setup", side_effect=record):
            for default in self.workspaces:
                set_default_workspace(default)
                for cwd in (self.root, *(ws.root for ws in self.workspaces)):
                    a, b, c = self.workspaces
                    for canonical, legacy in ((None, None), (a, None), (b, None), (None, c), (c, a)):
                        selectors = [([], canonical or legacy or default)]
                        for ws in self.workspaces:
                            selectors.extend([(["--workspace", str(ws.root)], ws),
                                              (["--instance", ws.instance_id], ws),
                                              (["--instance", ws.display_name], ws)])
                        env = {"SIDEQUESTOR_WORKSPACE": str(canonical.root) if canonical else "",
                               "YAAS_WORKSPACE": str(legacy.root) if legacy else ""}
                        with patch.dict(os.environ, env), chdir(cwd):
                            for command in ("start", "resume", "stop", "pause", "setup"):
                                for selector, expected in selectors:
                                    with self.subTest(default=default.display_name, cwd=str(cwd),
                                                      env=env, command=command, selector=selector):
                                        selected.clear()
                                        self.assertEqual(self.invoke([*selector, command]), 0)
                                        self.assertEqual(selected, [expected.instance_id])
                                        count += 1
        self.assertEqual(count, 3000)
        self.assertEqual(self.actions, [])

    def test_ambiguous_names_truncated_ids_and_missing_paths_never_touch_jobs(self):
        registry = self.root / "config/yaas/instances.json"
        rows = json.loads(registry.read_text())
        rows[0]["display_name"] = rows[1]["display_name"] = "duplicate"
        registry.write_text(json.dumps(rows))
        for command in ("start", "stop", "pause", "resume", "setup"):
            for selector in (["--instance", "duplicate"],
                             ["--instance", self.workspaces[0].instance_id[:8]],
                             ["--workspace", str(self.root / "missing")]):
                with self.subTest(command=command, selector=selector), \
                        self.assertRaisesRegex(SystemExit, "ambiguous|not a Sidequestor workspace"):
                    self.invoke([*selector, command])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)

    def test_explicit_empty_selectors_cannot_fall_back_to_a_default_instance(self):
        before = {ws: self.snapshot(ws) for ws in self.workspaces}
        registry = self.root / "config/yaas/instances.json"
        registry_before = registry.read_bytes()
        commands = (["start"], ["stop"], ["pause"], ["resume"], ["setup"],
                    ["instances", "list"], ["migrate"], ["status"], ["--help"])
        with patch.object(cli, "_cmd_start", side_effect=AssertionError("Start handler must not run")), \
                patch.object(cli, "_cmd_stop", side_effect=AssertionError("Stop handler must not run")), \
                patch.object(cli, "_cmd_setup", side_effect=AssertionError("Setup handler must not run")):
            for option in ("--workspace", "--instance"):
                for selector in ([option, ""], [option + "="]):
                    for command in commands:
                        with self.subTest(selector=selector, command=command), \
                                self.assertRaisesRegex(SystemExit, "non-empty"):
                            self.invoke([*selector, *command])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)
        self.assertEqual(registry.read_bytes(), registry_before)
        for ws in self.workspaces:
            self.assertEqual(self.snapshot(ws), before[ws])

    def test_empty_positional_stop_id_cannot_match_a_blank_display_name(self):
        registry = self.root / "config/yaas/instances.json"
        rows = json.loads(registry.read_text())
        rows[0]["display_name"] = ""
        registry.write_text(json.dumps(rows))
        for command in ("stop", "pause"):
            with self.subTest(command=command), self.assertRaisesRegex(SystemExit, "non-empty"):
                self.invoke([command, ""])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)

    def test_missing_default_cannot_fall_back_to_another_running_workspace(self):
        (self.workspaces[0].yaas_dir / "instance.json").unlink()
        with chdir(self.workspaces[1].root), self.assertRaisesRegex(SystemExit, "Default workspace is unavailable"):
            self.invoke(["stop"])
        self.assertEqual(self.actions, [])
        self.assertEqual(len(self.loaded), 9)

    def test_cwd_fallback_without_registry_targets_only_the_containing_workspace(self):
        registry = self.root / "config/yaas/instances.json"
        registry.write_text("[]")
        target = self.workspaces[1]
        nested = target.root / "nested"
        nested.mkdir()
        with chdir(nested):
            self.assertEqual(self.invoke(["stop"]), 0)
        self.assertTrue(all(target.instance_id in label for _, label in self.actions))
        self.assertEqual(len(self.loaded), 6)
