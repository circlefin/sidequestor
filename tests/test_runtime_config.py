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
