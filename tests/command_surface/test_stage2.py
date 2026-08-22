from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[2]
YAAS = Path(os.environ.get("SIDEQUESTOR_BIN", PACKAGE / ".venv" / "bin" / "sq"))
SIDEQUESTOR = Path(os.environ.get("SIDEQUESTOR_BIN", PACKAGE / ".venv" / "bin" / "sidequestor"))


def run(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home or PACKAGE / ".test-home")
    return subprocess.run([str(YAAS), *args], text=True, capture_output=True, env=env)


def run_sidequestor(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home or PACKAGE / ".test-home")
    return subprocess.run([str(SIDEQUESTOR), *args], text=True, capture_output=True, env=env)


class Stage2CommandSurfaceTest(unittest.TestCase):
    def test_public_help(self) -> None:
        result = run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("init", "tick", "loop", "dashboard", "sync-resources", "mcp-call"):
            self.assertIn(command, result.stdout)

    def test_every_public_command_has_help(self) -> None:
        commands = (
            "init", "instances", "setup", "start", "stop", "tick", "loop", "dashboard", "doctor",
            "migrate", "sync-resources", "watch", "ack", "approval", "log", "slack-send",
            "react", "mcp-call", "jira-call",
        )
        for command in commands:
            result = run(command, "--help")
            self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
            self.assertIn(command, result.stdout)

    def test_new_workspace_smoke(self) -> None:
        with tempfile.TemporaryDirectory(prefix="yaas-stage2-") as temp, tempfile.TemporaryDirectory(prefix="yaas-home-") as home:
            workspace = Path(temp) / "workspace"
            home_path = Path(home)
            result = run("init", str(workspace), "--name", "stage2", home=home_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workspace / ".yaas" / "instance.json").exists())
            for command in (
                ("--workspace", str(workspace), "doctor"),
                ("--workspace", str(workspace), "instances", "doctor"),
                ("--workspace", str(workspace), "sync-resources"),
                ("--workspace", str(workspace), "tick", "--dry-run"),
                ("--workspace", str(workspace), "setup", "--render-only"),
            ):
                result = run(*command, home=home_path)
                self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}\n{result.stdout}")
            self.assertFalse((workspace / "yaas-triage").exists())
            self.assertTrue((workspace / ".yaas" / "engine" / "current" / "skills").is_dir())

    def test_sidequestor_brand_and_legacy_alias_share_the_same_engine(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-" ) as temp, tempfile.TemporaryDirectory(prefix="sidequestor-home-") as home:
            workspace = Path(temp) / "workspace"
            home_path = Path(home)
            result = run_sidequestor("init", str(workspace), "--name", "rebrand", home=home_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("initialized Sidequestor workspace", result.stdout)
            self.assertIn("SIDEQUESTOR_SLACK_CHECKERS_ENABLED", (workspace / ".env.example").read_text())
            self.assertTrue((workspace / ".yaas").is_dir())
            self.assertFalse((workspace / ".sidequestor").exists())
            self.assertEqual(run("--workspace", str(workspace), "doctor", home=home_path).returncode, 0)
            self.assertEqual(
                subprocess.run(
                    [sys.executable, "-m", "yaas_triage", "--version"],
                    text=True, capture_output=True,
                    env={"HOME": str(home_path), "PYTHONPATH": str(PACKAGE / "src")},
                ).returncode,
                0,
            )

    def test_stop_defaults_to_workspace_instance_from_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-stop-") as temp, tempfile.TemporaryDirectory(prefix="sidequestor-stop-home-") as home:
            workspace = Path(temp) / "workspace"
            nested = workspace / "logs" / "nested"
            nested.mkdir(parents=True)
            home_path = Path(home)
            initialized = run("init", str(workspace), home=home_path)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            marker = json.loads((workspace / ".yaas" / "instance.json").read_text())
            production = workspace / ".yaas" / "launchd" / "production.json"
            production.parent.mkdir(parents=True, exist_ok=True)
            production.write_text(json.dumps({
                "backend": "production",
                "workspace": str(workspace),
                "jobs": {"triage": {"label": f"com.sidequestor.{marker['instance_id']}.triage"}},
            }) + "\n")
            stopped = subprocess.run(
                [str(YAAS), "stop"], cwd=nested, text=True, capture_output=True,
                env={**os.environ, "HOME": str(home_path)},
            )
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertIn(marker["instance_id"], stopped.stdout)


if __name__ == "__main__":
    unittest.main()
