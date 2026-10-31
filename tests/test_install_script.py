"""Offline checks for the public one-command bootstrap."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls"
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        TEST_BIN=str(self.bin), TEST_LOG=str(self.log),
                        PATH=f"{self.bin}:/usr/bin:/bin")
        self.command("uv", """#!/bin/sh
if [ "$1 $2 $3" = "tool dir --bin" ]; then printf '%s\\n' "$TEST_BIN"; exit 0; fi
printf 'uv %s\\n' "$*" >> "$TEST_LOG"
if [ "$1 $2" = "tool install" ]; then cp "$TEST_BIN/sq.template" "$TEST_BIN/sq"; chmod +x "$TEST_BIN/sq"; fi
""")
        self.command("sq.template", """#!/bin/sh
printf 'sq %s\\n' "$*" >> "$TEST_LOG"
if [ "$1" = init ]; then mkdir -p "$2/.yaas"; : > "$2/.yaas/instance.json"; : > "$2/.env"; fi
""")
        self.command("curl", "#!/bin/sh\nexit 99\n")

    def command(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def run_installer(self):
        return subprocess.run(["sh", str(SCRIPT)], env=self.env, capture_output=True, text=True)

    def test_fresh_install_creates_only_default_workspace(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "sidequestor/.yaas/instance.json").exists())
        calls = self.log.read_text()
        self.assertIn("uv tool install --python 3.11 sidequestor", calls)
        self.assertIn(f"sq --workspace {self.home / 'sidequestor'} setup", calls)
        self.assertIn("sq dashboard", calls)

    def test_registered_user_does_not_get_second_workspace(self):
        (self.home / ".config/yaas").mkdir(parents=True)
        (self.home / ".config/yaas/instances.json").write_text("[]")
        self.command("sq", (self.bin / "sq.template").read_text())
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "sidequestor").exists())
        self.assertEqual(self.log.read_text().splitlines(), ["sq setup", "sq dashboard"])

    def test_supplied_slack_metadata_is_seeded_once_without_overwriting(self):
        self.env.update(SIDEQUESTOR_INSTALL_SLACK_APP_ID="ATEST123",
                        SIDEQUESTOR_INSTALL_SLACK_CLIENT_ID="12345.67890",
                        SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_NAME="Fixture",
                        SIDEQUESTOR_INSTALL_SLACK_WORKSPACE_DOMAIN="fixture.example")
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        env_file = self.home / "sidequestor/.env"
        self.assertEqual(env_file.read_text().count("SLACK_APP_ID=ATEST123"), 1)
        self.assertEqual(env_file.read_text().count("SLACK_CLIENT_ID=12345.67890"), 1)

        self.env["SIDEQUESTOR_INSTALL_SLACK_APP_ID"] = "AOTHER123"
        changed = self.run_installer()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("Existing SLACK_APP_ID differs", changed.stderr)
        self.assertIn("SLACK_APP_ID=ATEST123", env_file.read_text())


if __name__ == "__main__":
    unittest.main()
