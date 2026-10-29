from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DISPATCH = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "dispatch" / "dispatch-agent.sh"


class CursorDispatchPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-cursor-dispatch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        stub_dir = self.home / ".local" / "bin"
        stub_dir.mkdir(parents=True)
        self.args_file = self.root / "args.txt"
        stub = stub_dir / "cursor-agent"
        stub.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\n')
        stub.chmod(0o755)

    def _run(self, mode: str | None) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "REPO_ROOT": str(self.root),
            "YAAS_AGENT": "cursor",
            "CAPTURE_ARGS": str(self.args_file),
        }
        if mode is not None:
            environment["YAAS_CURSOR_PERMISSION_MODE"] = mode
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(DISPATCH), "test prompt"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def test_bypass_permissions_forces_commands_and_disables_sandbox(self) -> None:
        result = self._run("bypassPermissions")

        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.args_file.read_text().splitlines()
        self.assertEqual(arguments, [
            "-p", "--output-format", "stream-json", "--approve-mcps", "--trust",
            "--force", "--sandbox", "disabled", "--workspace", str(self.root), "test prompt",
        ])

    def test_default_mode_always_trusts_without_force_or_sandbox_override(self) -> None:
        for mode in (None, "default"):
            with self.subTest(mode=mode):
                result = self._run(mode)

                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = self.args_file.read_text().splitlines()
                self.assertEqual(arguments, [
                    "-p", "--output-format", "stream-json", "--approve-mcps", "--trust",
                    "--workspace", str(self.root), "test prompt",
                ])
                self.assertNotIn("--force", arguments)
                self.assertNotIn("--sandbox", arguments)

    def test_invalid_permission_mode_exits_two_without_launching_cursor(self) -> None:
        result = self._run("workspace-write")

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid YAAS_CURSOR_PERMISSION_MODE='workspace-write'", result.stderr)
        self.assertIn("expected default or bypassPermissions", result.stderr)
        self.assertFalse(self.args_file.exists())


if __name__ == "__main__":
    unittest.main()
