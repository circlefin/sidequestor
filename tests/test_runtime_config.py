from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime"
TRIAGE_ROOT = RUNTIME_ROOT / "yaas-triage"


class RuntimeConfigTest(unittest.TestCase):
    def test_native_children_receive_workspace_dotenv_aliases(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-native-env-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
            workspace.env_file.write_text(
                "SIDEQUESTOR_TRIAGE_INTERVAL=11\n"
                "SIDEQUESTOR_HEARTBEAT_INTERVAL=17\n"
                "SIDEQUESTOR_APPROVAL_LEASE_MIN=23\n"
            )
            environment = _environment(workspace)
            self.assertEqual(environment["YAAS_TRIAGE_INTERVAL"], "11")
            self.assertEqual(environment["YAAS_HEARTBEAT_INTERVAL"], "17")
            self.assertEqual(environment["YAAS_APPROVAL_LEASE_MIN"], "23")
            overridden = _environment(workspace, {"YAAS_AGENT": "stub"})
            self.assertEqual(overridden["YAAS_AGENT"], "stub")
            self.assertEqual(overridden["SIDEQUESTOR_AGENT"], "stub")

    def test_explicit_legacy_environment_wins_over_canonical_dotenv(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-native-precedence-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
            workspace.env_file.write_text("SIDEQUESTOR_AGENT=codex\n")
            with patch.dict(os.environ, {"YAAS_AGENT": "stub"}, clear=False):
                environment = _environment(workspace)
            self.assertEqual(environment["YAAS_AGENT"], "stub")
            self.assertEqual(environment["SIDEQUESTOR_AGENT"], "stub")

    def test_canonical_dotenv_wins_regardless_of_mixed_namespace_order(self) -> None:
        from sidequestor.native import _environment
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-native-mixed-env-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
            workspace.env_file.write_text(
                "YAAS_AGENT=legacy\nSIDEQUESTOR_AGENT=codex\n"
            )
            environment = _environment(workspace)
            self.assertEqual(environment["YAAS_AGENT"], "codex")
            self.assertEqual(environment["SIDEQUESTOR_AGENT"], "codex")

    def test_shell_loop_preserves_exports_and_does_not_execute_dotenv(self) -> None:
        script = TRIAGE_ROOT / "triage-loop.sh"
        with tempfile.TemporaryDirectory(prefix="sidequestor-loop-env-") as raw:
            root = Path(raw)
            workspace = root / "workspace"
            (workspace / "state" / "triage").mkdir(parents=True)
            (workspace / ".yaas").mkdir()
            (workspace / ".yaas" / "instance.json").write_text("{}\n")
            marker = root / "executed"
            (workspace / ".env").write_text(
                "SIDEQUESTOR_TRIAGE_INTERVAL=0.03\n"
                f"SIDEQUESTOR_DANGEROUS=$(touch {marker})\n"
            )
            capture = root / "capture"
            fake_python = root / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-\" ]; then\n"
                "  printf '%s\\n' \"$2\"\n"
                "  exit 0\n"
                "fi\n"
                f"printf '%s\\n' \"${{SIDEQUESTOR_TRIAGE_INTERVAL-}}|${{YAAS_TRIAGE_INTERVAL-}}\" > \"{capture}\"\n"
            )
            fake_python.chmod(0o755)
            environment = {
                "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                "SIDEQUESTOR_WORKSPACE": str(workspace),
                "YAAS_TRIAGE_INTERVAL": "0.02",
            }
            process = subprocess.Popen(
                ["bash", str(script)], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                for _ in range(100):
                    if capture.exists():
                        break
                    import time
                    time.sleep(0.02)
            finally:
                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
            self.assertFalse(marker.exists())
            self.assertTrue(capture.exists(), f"loop output={stdout!r} stderr={stderr!r}")
            self.assertEqual(capture.read_text().strip(), "|0.02")

    def test_shell_loops_read_only_their_dotenv_interval(self) -> None:
        for script_name, key, expected in (
            ("triage-loop.sh", "SIDEQUESTOR_TRIAGE_INTERVAL", "0.03"),
            ("ops/heartbeat-loop.sh", "SIDEQUESTOR_HEARTBEAT_INTERVAL", "0.04"),
        ):
            with self.subTest(script_name=script_name):
                with tempfile.TemporaryDirectory(prefix="sidequestor-loop-dotenv-") as raw:
                    root = Path(raw)
                    workspace = root / "workspace"
                    (workspace / "state" / "triage").mkdir(parents=True)
                    (workspace / ".yaas").mkdir()
                    (workspace / ".yaas" / "instance.json").write_text("{}\n")
                    (workspace / ".env").write_text(f"{key}={expected}\n")
                    capture = root / "capture"
                    fake_python = root / "python3"
                    fake_python.write_text(
                        "#!/bin/sh\n"
                        "if [ \"$1\" = \"-\" ]; then\n"
                        "  case \"$2\" in\n"
                        f"    */.env) printf '%s\\n' \"{expected}\" ;;\n"
                        "    *) printf '%s\\n' \"$2\" ;;\n"
                        "  esac\n"
                        "  exit 0\n"
                        "fi\n"
                        f"printf '%s\\n' \"${{{key}-}}\" > \"{capture}\"\n"
                    )
                    fake_python.chmod(0o755)
                    process = subprocess.Popen(
                        ["bash", str(TRIAGE_ROOT / script_name)],
                        env={
                            "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                            "SIDEQUESTOR_WORKSPACE": str(workspace),
                        },
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    try:
                        for _ in range(100):
                            if capture.exists():
                                break
                            import time
                            time.sleep(0.02)
                    finally:
                        process.terminate()
                        stdout, stderr = process.communicate(timeout=5)
                    self.assertTrue(capture.exists(), f"loop output={stdout!r} stderr={stderr!r}")
                    self.assertEqual(capture.read_text().strip(), expected)

    def test_cli_loop_validates_and_forwards_interval(self) -> None:
        from sidequestor.cli import _cmd_loop
        from sidequestor.workspace import init_workspace

        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-interval-") as raw:
            with patch.dict(os.environ, {
                "SIDEQUESTOR_CONFIG_HOME": str(Path(raw) / "config"),
            }, clear=False):
                workspace = init_workspace(Path(raw) / "workspace")
            with patch("sidequestor.cli.run_native", return_value=0) as run:
                self.assertEqual(_cmd_loop(workspace, ["--interval", "7"]), 0)
            self.assertEqual(run.call_args.kwargs["extra_env"], {"YAAS_TRIAGE_INTERVAL": "7.0"})
            with self.assertRaises(SystemExit):
                _cmd_loop(workspace, ["--interval", "0"])

    def test_legacy_environment_aliases_reach_canonical_consumers(self) -> None:
        from sidequestor.native import _apply_env_aliases

        environment = {
            "YAAS_IDE_APP": "Legacy IDE",
            "SIDEQUESTOR_AGENT": "codex",
            "YAAS_AGENT": "claude",
        }
        resolved = _apply_env_aliases(environment)

        self.assertEqual(resolved["SIDEQUESTOR_IDE_APP"], "Legacy IDE")
        self.assertEqual(resolved["SIDEQUESTOR_AGENT"], "codex")
        self.assertEqual(resolved["YAAS_AGENT"], "codex")

    def test_canonical_environment_resolves_to_runtime_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-config-") as raw:
            workspace = Path(raw)
            (workspace / ".env").write_text(
                "SIDEQUESTOR_AGENT=codex\n"
                "SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n"
                "SIDEQUESTOR_UNACKED_PROMOTE=9\n"
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
                "SIDEQUESTOR_UNACKED_PROMOTE=9\n"
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
                    self.assertEqual(module._dotenv("YAAS_UNACKED_PROMOTE", "3"), "9")
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

    def test_health_monitor_configures_thresholds_from_workspace_dotenv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-health-config-") as raw:
            workspace = Path(raw)
            (workspace / ".env").write_text(
                "SIDEQUESTOR_HEALTH_STALL_MIN=21\n"
                "SIDEQUESTOR_HEALTH_HUNG_MIN=81\n"
                "SIDEQUESTOR_APPROVAL_LEASE_MIN=33\n"
            )
            import sys
            sys.path.insert(0, str(TRIAGE_ROOT))
            try:
                spec = importlib.util.spec_from_file_location(
                    "sidequestor_health_test",
                    TRIAGE_ROOT / "ops" / "health-monitor.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                with patch.dict(os.environ, {
                    "SIDEQUESTOR_WORKSPACE": str(workspace),
                    "SIDEQUESTOR_RUNTIME_ROOT": str(RUNTIME_ROOT),
                }, clear=False):
                    spec.loader.exec_module(module)
                module.configure(module.load_environment(workspace))
            finally:
                sys.path.pop(0)
            self.assertEqual(module.STALL_MIN, 21.0)
            self.assertEqual(module.HUNG_MIN, 81.0)

    def test_numeric_runtime_settings_fall_back_on_invalid_values(self) -> None:
        import sys

        sys.path.insert(0, str(TRIAGE_ROOT))
        try:
            spec = importlib.util.spec_from_file_location(
                "sidequestor_health_invalid_test",
                TRIAGE_ROOT / "ops" / "health-monitor.py",
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            module.configure({
                "YAAS_HEALTH_STALL_MIN": "not-a-number",
                "YAAS_HEALTH_FAIL_STREAK": "nan",
            })
            self.assertEqual(module.STALL_MIN, 10.0)
            self.assertEqual(module.FAIL_STREAK, 5)

            approval_spec = importlib.util.spec_from_file_location(
                "sidequestor_approval_invalid_test",
                TRIAGE_ROOT / "approval_state.py",
            )
            approval = importlib.util.module_from_spec(approval_spec)
            assert approval_spec.loader is not None
            approval_spec.loader.exec_module(approval)
            approval.configure({"SIDEQUESTOR_APPROVAL_LEASE_MIN": "bad"})
            self.assertEqual(approval.LEASE_MINUTES, 45)
        finally:
            sys.path.pop(0)

    def test_dashboard_build_info_honors_launch_environment(self) -> None:
        environment = {
            "YAAS_WORKSPACE": tempfile.gettempdir(),
            "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
            "SIDEQUESTOR_VERSION": "0.1.5",
            "SIDEQUESTOR_COMMIT": "abcdef0123456789",
            "SIDEQUESTOR_REF": "package/sidequestor-0.1.0",
        }
        with patch.dict(os.environ, environment, clear=False):
            import sys
            sys.path.insert(0, str(TRIAGE_ROOT))
            try:
                spec = importlib.util.spec_from_file_location(
                    "sidequestor_dashboard_build_test",
                    TRIAGE_ROOT / "ops" / "dashboard-server.py",
                )
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                with patch.object(sys, "argv", ["dashboard-server.py"]):
                    spec.loader.exec_module(module)
                build = module.build_build_info()
            finally:
                sys.path.pop(0)
        self.assertEqual(build["version"], "0.1.5")
        self.assertEqual(build["commit"], "abcdef0")
        self.assertEqual(build["commit_full"], "abcdef0123456789")
        self.assertEqual(build["ref"], "package/sidequestor-0.1.0")

    def test_dashboard_reaction_guide_resolves_defaults_and_overrides(self) -> None:
        """The guide must show the real emoji. Seeding the canonical key with an empty
        string shadowed both the legacy value and the default, failing every role."""
        import sys

        def guide(extra_env):
            environment = {"YAAS_WORKSPACE": tempfile.gettempdir(), "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT)}
            environment.update(extra_env)
            with patch.dict(os.environ, environment, clear=False):
                sys.path.insert(0, str(TRIAGE_ROOT))
                try:
                    spec = importlib.util.spec_from_file_location(
                        "sidequestor_dashboard_emoji_test", TRIAGE_ROOT / "ops" / "dashboard-server.py",
                    )
                    module = importlib.util.module_from_spec(spec)
                    assert spec.loader is not None
                    with patch.object(sys, "argv", ["dashboard-server.py"]):
                        spec.loader.exec_module(module)
                    return module.build_reaction_emojis()
                finally:
                    sys.path.pop(0)

        default = guide({})
        self.assertIsNone(default["error"])
        self.assertEqual(default["roles"]["process"], "robot_face")
        self.assertEqual(default["roles"]["done"], "white_check_mark")

        overridden = guide({"SIDEQUESTOR_REACTION_PROCESS_EMOJI": "eyes"})
        self.assertIsNone(overridden["error"])
        self.assertEqual(overridden["roles"]["process"], "eyes")

    def test_dashboard_surfaces_unknown_non_terminal_approval_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-dashboard-status-") as raw:
            workspace = Path(raw)
            approvals = workspace / "state" / "pending-approvals.json"
            approvals.parent.mkdir(parents=True)
            approvals.write_text('{"version": 1, "items": []}')
            environment = {
                "YAAS_WORKSPACE": str(workspace),
                "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
            }
            with patch.dict(os.environ, environment, clear=False):
                import sys
                sys.path.insert(0, str(TRIAGE_ROOT))
                try:
                    spec = importlib.util.spec_from_file_location(
                        "sidequestor_dashboard_status_test",
                        TRIAGE_ROOT / "ops" / "dashboard-server.py",
                    )
                    module = importlib.util.module_from_spec(spec)
                    assert spec.loader is not None
                    with patch.object(sys, "argv", ["dashboard-server.py"]):
                        spec.loader.exec_module(module)
                    unknown = {
                        "id": "approval-blocked",
                        "quest_id": "quest-one",
                        "quest_title": "Quest one",
                        "status": "blocked",
                        "action_type": "slack_message",
                        "message_text": "Worker could not complete this action.",
                    }
                    with patch.object(
                        module, "_read_approvals",
                        return_value={"version": 1, "items": [unknown]},
                    ):
                        messages = module.build_messages()
                finally:
                    sys.path.pop(0)

            self.assertEqual(messages["needs_you"], [])
            self.assertEqual(messages["queued_items"], [])
            self.assertEqual(
                [item["id"] for item in messages["other_actions"]],
                ["approval-blocked"],
            )


if __name__ == "__main__":
    unittest.main()
