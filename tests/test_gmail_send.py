from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SEND = (
    PACKAGE_ROOT
    / "src"
    / "sidequestor"
    / "runtime"
    / "yaas-triage"
    / "skills"
    / "yaas-gmail-reply"
    / "send_fresh.py"
)


class GmailSendTest(unittest.TestCase):
    def test_fresh_send_uses_native_gws_helper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-gmail-send-") as raw:
            root = Path(raw)
            body_file = root / "body.txt"
            body_file.write_text("Hello there\n")
            args_file = root / "args.json"
            fake_gws = root / "gws"
            fake_gws.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "open(os.environ['GWS_ARGS_FILE'], 'w').write(json.dumps(sys.argv[1:]))\n"
                "print('{\"id\":\"sent-1\"}')\n"
            )
            fake_gws.chmod(fake_gws.stat().st_mode | stat.S_IXUSR)

            result = subprocess.run(
                [
                    sys.executable, str(SEND), "--to", "alice@example.com",
                    "--subject", "Hello", "--body-file", str(body_file),
                ],
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "GWS_BIN": str(fake_gws),
                    "GWS_ARGS_FILE": str(args_file),
                    "SIDEQUESTOR_FROM_EMAIL": "Guangmian <me@example.com>",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '{"id":"sent-1"}')
            self.assertEqual(
                json.loads(args_file.read_text()),
                [
                    "gmail", "+send", "--to", "alice@example.com",
                    "--subject", "Hello", "--body", "Hello there\n",
                    "--from", "me@example.com",
                ],
            )


if __name__ == "__main__":
    unittest.main()
