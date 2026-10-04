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
REPLY = (
    PACKAGE_ROOT
    / "src"
    / "sidequestor"
    / "runtime"
    / "yaas-triage"
    / "skills"
    / "yaas-gmail-reply"
    / "gmail-reply.py"
)


class GmailReplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="sidequestor-gmail-reply-")
        root = Path(self.tempdir.name)
        self.calls_file = root / "calls.jsonl"
        self.fake_gws = root / "gws"
        self.fake_gws.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['GWS_CALLS_FILE'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('GWS_FAIL') == '1':\n"
            "    print('API error envelope')\n"
            "    print('transport detail', file=sys.stderr)\n"
            "    raise SystemExit(7)\n"
            "if 'get' in sys.argv:\n"
            "    print(os.environ.get('GWS_METADATA', '{\"payload\":{\"headers\":[]}}'))\n"
            "else:\n"
            "    print(os.environ.get('GWS_SEND_OUTPUT', '{\"id\":\"sent-1\"}'))\n"
        )
        self.fake_gws.chmod(self.fake_gws.stat().st_mode | stat.S_IXUSR)
        self.env = {
            **os.environ,
            "GWS_BIN": str(self.fake_gws),
            "GWS_CALLS_FILE": str(self.calls_file),
            "SIDEQUESTOR_FROM_EMAIL": "Guangmian <me@example.com>",
        }

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_reply(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPLY), *args],
            input=stdin,
            text=True,
            capture_output=True,
            env=self.env,
        )

    def recorded_calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def test_requires_exactly_one_explicit_reply_mode(self) -> None:
        missing = self.run_reply("message-1", "--send", "--body", "Hello")
        self.assertEqual(missing.returncode, 2)
        self.assertIn(
            "one of the arguments --reply-sender --reply-all is required", missing.stderr
        )

        conflicting = self.run_reply(
            "message-1", "--reply-sender", "--reply-all", "--send", "--body", "Hello"
        )
        self.assertEqual(conflicting.returncode, 2)
        self.assertIn("not allowed with argument --reply-sender", conflicting.stderr)

    def test_reply_sender_uses_native_gws_reply(self) -> None:
        result = self.run_reply("message-1", "--reply-sender", "--send", stdin="Hello sender")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sent-1")
        self.assertEqual(
            self.recorded_calls(),
            [[
                "gmail", "+reply", "--message-id", "message-1",
                "--body", "Hello sender", "--from", "me@example.com",
            ]],
        )

    def test_sender_only_rejects_recipient_mutation(self) -> None:
        for flag in ("--cc", "--remove"):
            with self.subTest(flag):
                result = self.run_reply(
                    "message-1", "--reply-sender", "--send", "--body", "Hello",
                    flag, "a@example.com",
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"{flag} requires --reply-all", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_reply_all_restores_noncanonical_original_cc_and_passes_options(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({
            "payload": {"headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "CC", "value": "Bob <bob@example.com>, carol@example.com"},
            ]}
        })
        result = self.run_reply(
            "message-2", "--reply-all", "--body", "<p>Hello all</p>",
            "--cc", "new@example.com", "--remove", "former@example.com",
            "--html", "--attach", "/tmp/notes.pdf", "--draft",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded_calls()
        self.assertEqual(calls[0][:4], ["gmail", "users", "messages", "get"])
        self.assertIn('"format": "metadata"', calls[0][5])
        self.assertEqual(
            calls[1],
            [
                "gmail", "+reply-all", "--message-id", "message-2",
                "--body", "<p>Hello all</p>", "--from", "me@example.com",
                "--cc", "bob@example.com,carol@example.com,new@example.com",
                "--remove", "former@example.com", "--html",
                "--attach", "/tmp/notes.pdf", "--draft",
            ],
        )

    def test_reply_all_stops_if_cc_workaround_metadata_read_fails(self) -> None:
        self.env["GWS_FAIL"] = "1"
        result = self.run_reply("message-2", "--reply-all", "--send", "--body", "Hello")

        self.assertEqual(result.returncode, 7)
        self.assertIn("API error envelope", result.stderr)
        self.assertIn("transport detail", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_missing_gws_is_reported_without_traceback(self) -> None:
        self.env["GWS_BIN"] = str(Path(self.tempdir.name) / "missing-gws")
        result = self.run_reply("message-1", "--reply-sender", "--send", "--body", "Hello")

        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_banner_prefixed_json_and_no_sender_are_supported(self) -> None:
        self.env.pop("SIDEQUESTOR_FROM_EMAIL")
        self.env.pop("YAAS_FROM_EMAIL", None)
        self.env["GWS_SEND_OUTPUT"] = "gws notice\\n{\"id\":\"sent-2\"}"
        result = self.run_reply(
            "message-1", "--reply-sender", "--send", "--body", "Hello"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sent-2")
        self.assertNotIn("--from", self.recorded_calls()[0])

    def test_empty_body_is_rejected_before_gws(self) -> None:
        result = self.run_reply("message-1", "--reply-sender", "--send", stdin="  ")

        self.assertEqual(result.returncode, 2)
        self.assertIn("no reply body provided", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_requires_exactly_one_explicit_delivery_mode(self) -> None:
        missing = self.run_reply("message-1", "--reply-sender", "--body", "Hello")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("one of the arguments --send --draft is required", missing.stderr)

        conflicting = self.run_reply(
            "message-1", "--reply-sender", "--send", "--draft", "--body", "Hello"
        )
        self.assertEqual(conflicting.returncode, 2)
        self.assertIn("not allowed with argument --send", conflicting.stderr)


if __name__ == "__main__":
    unittest.main()
