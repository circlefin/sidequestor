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


CLAP_VALUE_RULE = (
    "VALUE_FLAGS = {'--body', '--cc', '--remove', '--attach', '--to', '--subject', '--from'}\n"
    "for flag, value in zip(sys.argv[1:], sys.argv[2:]):\n"
    "    if flag in VALUE_FLAGS and value.startswith('-'):\n"
    "        print(f\"error: unexpected argument '{value[:2]}' found\", file=sys.stderr)\n"
    "        raise SystemExit(2)\n"
)


QUEST = "quest-mail"


def write_quest(root: Path, *, allow_send: bool) -> None:
    quest = root / "state" / "quests" / "active" / QUEST
    quest.mkdir(parents=True, exist_ok=True)
    (quest / "meta.json").write_text(json.dumps({"id": QUEST, "allow_send": allow_send}))


def claim(root: Path, spec: dict) -> None:
    """Record `spec` as a reviewed approval the worker has claimed."""
    (root / "state" / "pending-approvals.json").write_text(json.dumps({"version": 1, "items": [{
        "id": "approval-1", "quest_id": QUEST, "status": "executing",
        "lease_expires_at": "2999-01-01T00:00:00Z", **spec,
    }]}))


class GmailReplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="sidequestor-gmail-reply-")
        root = self.root = Path(self.tempdir.name)
        write_quest(root, allow_send=True)
        self.calls_file = root / "calls.jsonl"
        self.fake_gws = root / "gws"
        self.fake_gws.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['GWS_CALLS_FILE'], 'a') as f:\n"
            "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            # Real gws (clap) rejects a separate option value that starts with '-'.
            f"{CLAP_VALUE_RULE}"
            "if os.environ.get('GWS_FAIL') == '1':\n"
            "    print('API error envelope')\n"
            "    print('transport detail', file=sys.stderr)\n"
            "    raise SystemExit(7)\n"
            "if 'sendAs' in sys.argv and 'list' in sys.argv:\n"
            "    print(os.environ.get('GWS_SEND_AS', '{\"sendAs\":[{\"sendAsEmail\":\"me@example.com\",\"isDefault\":true}]}'))\n"
            "elif 'getProfile' in sys.argv:\n"
            "    print(os.environ.get('GWS_PROFILE', '{\"emailAddress\":\"me@example.com\"}'))\n"
            "elif 'get' in sys.argv:\n"
            "    print(os.environ.get('GWS_METADATA', '{\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Alice <alice@example.com>\"},{\"name\":\"To\",\"value\":\"me@example.com\"},{\"name\":\"Cc\",\"value\":\"bob@example.com\"}]}}'))\n"
            "else:\n"
            "    print(os.environ.get('GWS_SEND_OUTPUT', '{\"id\":\"sent-1\"}'))\n"
        )
        self.fake_gws.chmod(self.fake_gws.stat().st_mode | stat.S_IXUSR)
        self.env = {
            **os.environ,
            "GWS_BIN": str(self.fake_gws),
            "GWS_CALLS_FILE": str(self.calls_file),
            "SIDEQUESTOR_FROM_EMAIL": "Guangmian <me@example.com>",
            "YAAS_WORKSPACE": str(root),
        }
        self.env.pop("SIDEQUESTOR_WORKSPACE", None)
        self.env.pop("SIDEQUESTOR_DISPATCH_TARGET", None)

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
        missing = self.run_reply("message-1", "--send", "--quest-id", QUEST, "--body", "Hello")
        self.assertEqual(missing.returncode, 2)
        self.assertIn(
            "one of the arguments --reply-sender --reply-all is required", missing.stderr
        )

        conflicting = self.run_reply(
            "message-1", "--reply-sender", "--reply-all", "--send", "--quest-id", QUEST, "--body", "Hello"
        )
        self.assertEqual(conflicting.returncode, 2)
        self.assertIn("not allowed with argument --reply-sender", conflicting.stderr)

    def test_reply_sender_uses_native_gws_reply(self) -> None:
        result = self.run_reply("message-1", "--reply-sender", "--send", "--quest-id", QUEST, stdin="Hello sender")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sent-1")
        self.assertEqual(
            self.recorded_calls(),
            [[
                "gmail", "+reply", "--message-id", "message-1",
                "--body=Hello sender", "--from=me@example.com",
            ]],
        )

    def test_body_starting_with_a_bullet_reaches_gws_intact(self) -> None:
        # A reply that opens with a list item is ordinary; gws must not read it as a flag.
        body = "- first point\n- second point"
        result = self.run_reply("message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--body", body)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--body={body}", self.recorded_calls()[0])

    def test_sender_only_rejects_recipient_mutation(self) -> None:
        for flag in ("--cc", "--remove"):
            with self.subTest(flag):
                result = self.run_reply(
                    "message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--body", "Hello",
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
                "--body=<p>Hello all</p>", "--from=me@example.com",
                "--cc=bob@example.com,carol@example.com,new@example.com",
                "--remove=former@example.com", "--html",
                "--attach=/tmp/notes.pdf", "--draft",
            ],
        )

    def test_reply_all_stops_if_cc_workaround_metadata_read_fails(self) -> None:
        self.env["GWS_FAIL"] = "1"
        result = self.run_reply("message-2", "--reply-all", "--send", "--quest-id", QUEST, "--body", "Hello")

        self.assertEqual(result.returncode, 7)
        self.assertIn("API error envelope", result.stderr)
        self.assertIn("transport detail", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_missing_gws_is_reported_without_traceback(self) -> None:
        self.env["GWS_BIN"] = str(Path(self.tempdir.name) / "missing-gws")
        result = self.run_reply("message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--body", "Hello")

        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_banner_prefixed_json_and_no_sender_are_supported(self) -> None:
        self.env.pop("SIDEQUESTOR_FROM_EMAIL")
        self.env.pop("YAAS_FROM_EMAIL", None)
        self.env["GWS_SEND_OUTPUT"] = "gws notice\\n{\"id\":\"sent-2\"}"
        result = self.run_reply(
            "message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--body", "Hello"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sent-2")
        self.assertFalse(any(arg.startswith("--from") for arg in self.recorded_calls()[0]))

    def test_empty_body_is_rejected_before_gws(self) -> None:
        result = self.run_reply("message-1", "--reply-sender", "--send", "--quest-id", QUEST, stdin="  ")

        self.assertEqual(result.returncode, 2)
        self.assertIn("no reply body provided", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_requires_exactly_one_explicit_delivery_mode(self) -> None:
        missing = self.run_reply("message-1", "--reply-sender", "--body", "Hello")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("one of the arguments --send --draft --approval-spec is required",
                      missing.stderr)

        conflicting = self.run_reply(
            "message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--draft", "--body", "Hello"
        )
        self.assertEqual(conflicting.returncode, 2)
        self.assertIn("not allowed with argument --send", conflicting.stderr)


    # --send is the only path that emails real people, so it carries the same quest policy
    # as slack-send and slack-file. Drafts stay ungated.

    def test_send_without_a_quest_is_refused_before_gws(self) -> None:
        result = self.run_reply("message-1", "--reply-sender", "--send", "--body", "Hello")

        self.assertEqual(result.returncode, 1)
        self.assertIn("quest_id is required", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_draft_only_quest_cannot_send_without_a_claimed_approval(self) -> None:
        write_quest(self.root, allow_send=False)
        result = self.run_reply(
            "message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--body", "Hello",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("allow_send false", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_dispatch_for_another_quest_cannot_send(self) -> None:
        self.env["SIDEQUESTOR_DISPATCH_TARGET"] = "quest-other"
        result = self.run_reply(
            "message-1", "--reply-sender", "--send", "--quest-id", QUEST, "--body", "Hello",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match dispatch target", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_claimed_approval_authorizes_only_the_reviewed_email(self) -> None:
        write_quest(self.root, allow_send=False)
        attachment = self.root / "deck.pdf"
        attachment.write_bytes(b"reviewed bytes")
        reply = ("message-2", "--reply-all", "--quest-id", QUEST, "--approval-id", "approval-1",
                 "--cc", "new@example.com", "--attach", str(attachment), "--body", "Deck attached")
        spec = self.run_reply(*reply, "--approval-spec")
        self.assertEqual(spec.returncode, 0, spec.stderr)
        expected = json.loads(spec.stdout)
        self.assertEqual(expected["target"]["recipients"], {
            "to": ["alice@example.com"],
            "cc": ["bob@example.com", "new@example.com"],
        })
        self.assertFalse(any("+reply-all" in call for call in self.recorded_calls()))
        claim(self.root, expected)

        sent = self.run_reply(*reply, "--send")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(sent.stdout.strip(), "sent-1")

        widened = self.run_reply(*reply, "--cc", "outsider@example.com", "--send")
        self.assertEqual(widened.returncode, 1)
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "To", "value": "me@example.com"},
            {"name": "Cc", "value": "bob@example.com, outsider@example.com"},
        ]}})
        changed_audience = self.run_reply(*reply, "--send")
        self.assertEqual(changed_audience.returncode, 1)
        self.env.pop("GWS_METADATA")
        attachment.write_bytes(b"different bytes")
        swapped = self.run_reply(*reply, "--send")
        self.assertEqual(swapped.returncode, 1)
        reworded = self.run_reply(*reply[:-1], "Something else", "--send")
        self.assertEqual(reworded.returncode, 1)
        self.assertEqual(sum("+reply-all" in call for call in self.recorded_calls()), 1)

    def test_reply_all_spec_resolves_self_sent_message_and_removals(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "me@example.com"},
            {"name": "To", "value": "alice@example.com, bob@example.com"},
            {"name": "Cc", "value": "carol@example.com"},
        ]}})
        result = self.run_reply(
            "message-3", "--reply-all", "--body", "Hello", "--remove", "bob@example.com",
            "--cc", "new@example.com", "--approval-spec",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["target"]["recipients"], {
            "to": ["alice@example.com"],
            "cc": ["carol@example.com", "new@example.com"],
        })

    def test_sender_only_spec_uses_reply_to_header(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "alice@example.com"},
            {"name": "Reply-To", "value": "team@example.com"},
            {"name": "To", "value": "me@example.com"},
        ]}})
        result = self.run_reply("message-4", "--reply-sender", "--body", "Hello",
                                "--approval-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["target"]["recipients"], {
            "to": ["team@example.com"], "cc": [],
        })

    def test_review_keeps_recipients_before_trailing_commas(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "ceo@example.com"},
            {"name": "Reply-To", "value": "attacker@example.com,"},
            {"name": "To", "value": "me@example.com"},
            {"name": "Cc", "value": "alice@example.com, outsider@example.com,"},
        ]}})
        for mode in ("--reply-sender", "--reply-all"):
            with self.subTest(mode=mode):
                result = self.run_reply("message-5", mode, "--body", "Hello", "--approval-spec")
                self.assertEqual(result.returncode, 0, result.stderr)
                audience = json.loads(result.stdout)["target"]["recipients"]
                self.assertEqual(audience["to"], ["attacker@example.com"])
                self.assertEqual(audience["cc"],
                                 ["alice@example.com", "outsider@example.com"]
                                 if mode == "--reply-all" else [])

    def test_reply_all_spec_accounts_for_default_send_as_alias(self) -> None:
        write_quest(self.root, allow_send=False)
        self.env.pop("SIDEQUESTOR_FROM_EMAIL")
        self.env.pop("YAAS_FROM_EMAIL", None)
        self.env["GWS_SEND_AS"] = json.dumps({"sendAs": [
            {"sendAsEmail": "me@example.com", "isDefault": False},
            {"sendAsEmail": "me@alias.com", "isDefault": True},
        ]})
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "me@alias.com"},
            {"name": "To", "value": "alice@example.com"},
            {"name": "Cc", "value": "carol@example.com"},
        ]}})
        reply = ("message-6", "--reply-all", "--body", "Hello")
        result = self.run_reply(*reply, "--approval-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = json.loads(result.stdout)
        self.assertEqual(spec["target"]["recipients"], {
            "to": ["alice@example.com"], "cc": ["carol@example.com"],
        })
        self.assertEqual(spec["target"]["sender"], "me@alias.com")
        claim(self.root, spec)
        sent = self.run_reply(*reply, "--quest-id", QUEST, "--approval-id", "approval-1",
                              "--send")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("--from=me@alias.com", self.recorded_calls()[-1])

    def test_repeated_reply_to_headers_cannot_hide_a_recipient(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "ceo@example.com"},
            {"name": "Reply-To", "value": '"<ceo@example.com>'},
            {"name": "Reply-To", "value": '"<attacker@evil.com>, <ceo@example.com>'},
            {"name": "To", "value": "me@example.com"},
        ]}})
        result = self.run_reply("message-7", "--reply-sender", "--body", "Hello",
                                "--approval-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["target"]["recipients"]["to"],
                         ["attacker@evil.com", "ceo@example.com"])

    def test_review_refuses_empty_reply_to_instead_of_showing_from(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "ceo@example.com"},
            {"name": "Reply-To", "value": ","},
        ]}})
        result = self.run_reply("message-8", "--reply-sender", "--body", "Hello",
                                "--approval-spec")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no usable recipient", result.stderr)

    def test_unicode_casefold_does_not_hide_a_distinct_cc_address(self) -> None:
        self.env["GWS_PROFILE"] = json.dumps({"emailAddress": "sam@example.com"})
        self.env["SIDEQUESTOR_FROM_EMAIL"] = "sam@example.com"
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "alice@example.com"},
            {"name": "To", "value": "sam@example.com"},
            {"name": "Cc", "value": "ſam@example.com"},
        ]}})
        result = self.run_reply("message-9", "--reply-all", "--body", "Hello",
                                "--approval-spec")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["target"]["recipients"], {
            "to": ["alice@example.com"], "cc": ["ſam@example.com"],
        })

    def test_review_refuses_ambiguous_reparsed_cc_address(self) -> None:
        self.env["GWS_METADATA"] = json.dumps({"payload": {"headers": [
            {"name": "From", "value": "alice@example.com"},
            {"name": "To", "value": "me@example.com"},
            {"name": "CC", "value": '<"b@x.com> ", bob@ok.com'},
        ]}})
        result = self.run_reply("message-10", "--reply-all", "--body", "Hello",
                                "--approval-spec")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot be reviewed safely", result.stderr)


if __name__ == "__main__":
    unittest.main()
