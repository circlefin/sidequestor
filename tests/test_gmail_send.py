from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Real gws (clap) rejects a separate option value that starts with '-'.
CLAP_VALUE_RULE = (
    "VALUE_FLAGS = {'--body', '--to', '--subject', '--from'}\n"
    "for flag, value in zip(sys.argv[1:], sys.argv[2:]):\n"
    "    if flag in VALUE_FLAGS and value.startswith('-'):\n"
    "        print(f\"error: unexpected argument '{value[:2]}' found\", file=sys.stderr)\n"
    "        raise SystemExit(2)\n"
)


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


QUEST = "quest-mail"


class GmailSendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="sidequestor-gmail-send-")
        self.root = Path(self.tempdir.name)
        self.write_quest(allow_send=True)
        self.args_file = self.root / "args.json"
        fake_gws = self.root / "gws"
        fake_gws.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "open(os.environ['GWS_ARGS_FILE'], 'w').write(json.dumps(sys.argv[1:]))\n"
            f"{CLAP_VALUE_RULE}"
            "print('{\"id\":\"sent-1\"}')\n"
        )
        fake_gws.chmod(fake_gws.stat().st_mode | stat.S_IXUSR)
        self.env = {**os.environ, "GWS_BIN": str(fake_gws), "GWS_ARGS_FILE": str(self.args_file),
                    "SIDEQUESTOR_FROM_EMAIL": "Guangmian <me@example.com>",
                    "YAAS_WORKSPACE": str(self.root)}
        self.env.pop("SIDEQUESTOR_WORKSPACE", None)
        self.env.pop("SIDEQUESTOR_DISPATCH_TARGET", None)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_quest(self, *, allow_send: bool) -> None:
        quest = self.root / "state" / "quests" / "active" / QUEST
        quest.mkdir(parents=True, exist_ok=True)
        (quest / "meta.json").write_text(json.dumps({"id": QUEST, "allow_send": allow_send}))

    def send(self, body: str, *extra: str) -> subprocess.CompletedProcess[str]:
        body_file = self.root / "body.txt"
        body_file.write_text(body)
        return subprocess.run(
            [sys.executable, str(SEND), "--to", "alice@example.com", "--subject", "Hello",
             "--body-file", str(body_file), *extra],
            text=True, capture_output=True, env=self.env,
        )

    def assert_sent(self, body: str) -> None:
        result = self.send(body, "--quest-id", QUEST)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '{"id":"sent-1"}')
        self.assertEqual(
            json.loads(self.args_file.read_text()),
            [
                "gmail", "+send", "--to=alice@example.com",
                "--subject=Hello", f"--body={body}",
                "--from=me@example.com",
            ],
        )

    def test_fresh_send_uses_native_gws_helper(self) -> None:
        self.assert_sent("Hello there\n")

    def test_body_starting_with_a_bullet_reaches_gws_intact(self) -> None:
        # A message that opens with a list item is ordinary; gws must not read it as a flag.
        self.assert_sent("- first point\n- second point\n")

    def test_send_without_a_quest_is_refused_before_gws(self) -> None:
        # Every call here emails real people, so it carries the same quest policy as Slack.
        result = self.send("Hello")

        self.assertEqual(result.returncode, 1)
        self.assertIn("quest_id is required", result.stderr)
        self.assertFalse(self.args_file.exists())

    def test_claimed_approval_authorizes_only_the_reviewed_email(self) -> None:
        self.write_quest(allow_send=False)
        denied = self.send("Hello", "--quest-id", QUEST)
        self.assertEqual(denied.returncode, 1)
        self.assertIn("allow_send false", denied.stderr)

        spec = self.send("Hello", "--quest-id", QUEST, "--approval-spec")
        self.assertEqual(spec.returncode, 0, spec.stderr)
        self.assertFalse(self.args_file.exists())
        (self.root / "state" / "pending-approvals.json").write_text(json.dumps({
            "version": 1, "items": [{
                "id": "approval-1", "quest_id": QUEST, "status": "executing",
                "lease_expires_at": "2999-01-01T00:00:00Z", **json.loads(spec.stdout),
            }]}))

        reworded = self.send("Hello, changed", "--quest-id", QUEST, "--approval-id", "approval-1")
        self.assertEqual(reworded.returncode, 1)
        self.assertFalse(self.args_file.exists())
        sent = self.send("Hello", "--quest-id", QUEST, "--approval-id", "approval-1")
        self.assertEqual(sent.returncode, 0, sent.stderr)

    def test_bug_report_metadata_preserves_exact_email_approval_gate(self) -> None:
        spec = json.loads(self.send('Sanitized report', '--approval-spec').stdout)
        spec['target'].update(system_key='health:example:1', report_kind='package_bug', installed_version='0.1.54')
        item = {'id': 'report-1', 'quest_id': QUEST, 'status': 'pending_review',
                'lease_expires_at': '2999-01-01T00:00:00Z', **spec}
        path = self.root / 'state/pending-approvals.json'
        path.write_text(json.dumps({'items': [item]}))
        # Even an ordinarily send-enabled quest must claim the report first.
        denied = self.send('Sanitized report', '--quest-id', QUEST, '--approval-id', 'report-1')
        self.assertEqual(denied.returncode, 1)
        self.assertFalse(self.args_file.exists())
        item['status'] = 'executing'; path.write_text(json.dumps({'items': [item]}))
        changed = self.send('Changed report', '--quest-id', QUEST, '--approval-id', 'report-1')
        self.assertEqual(changed.returncode, 1)
        self.assertFalse(self.args_file.exists())
        sent = self.send('Sanitized report', '--quest-id', QUEST, '--approval-id', 'report-1')
        self.assertEqual(sent.returncode, 0, sent.stderr)

    def test_maintainer_destination_requires_review_even_for_send_enabled_quest(self):
        body = self.root / 'report.txt'; body.write_text('Sanitized bug report')
        for address in ('guangmian.kung@circle.com', 'Guangmian <guangmian.kung+bug@circle.com>', 'guangmiankung@circle.com'):
            result = subprocess.run([sys.executable, str(SEND), '--to', address,
                                     '--subject', 'Package bug', '--body-file', str(body), '--quest-id', QUEST],
                                    text=True, capture_output=True, env=self.env)
            self.assertEqual(result.returncode, 1)
        self.assertFalse(self.args_file.exists())


if __name__ == "__main__":
    unittest.main()
