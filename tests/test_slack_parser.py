import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECKERS = Path(__file__).parents[1] / "src" / "sidequestor" / "runtime" / "yaas-triage" / "checkers"
spec = importlib.util.spec_from_file_location("slack_utils", CHECKERS / "slack_utils.py")
slack_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slack_utils)


THREAD = """=== THREAD PARENT MESSAGE ===
From: Guangmian Kung <g@example.com> (U1)
Time: 2026-08-27 10:00:00 +07
Message TS: 100.000000
parent

=== THREAD REPLIES (2 total) ===

--- Reply 1 of 2 ---
From: Guangmian Kung <g@example.com> (U1)
Time: 2026-08-27 10:01:00 +07
Message TS: 101.000000
first reply
*Sent using* <@U123|Sidequestor>

--- Reply 2 of 2 ---
From: Someone Else <s@example.com> (U2)
Time: 2026-08-27 10:02:00 +07
Message TS: 102.000000
second reply
"""

EXTERNAL_THREAD = """=== THREAD PARENT MESSAGE ===
From: External Parent <parent@partner.example> (UEXT1, external: Partner Org (Singapore))
Time: 2026-08-27 10:00:00 +07
Message TS: 100.000000
external parent

=== THREAD REPLIES (2 total) ===

--- Reply 1 of 2 ---
From: Internal Reply <internal@example.com> (UINT1)
Time: 2026-08-27 10:01:00 +07
Message TS: 101.000000
internal reply

--- Reply 2 of 2 ---
From: External Reply <reply@another.example> (UEXT2, external: Another Organization (APAC))
Time: 2026-08-27 10:02:00 +07
Message TS: 102.000000
external reply
"""

MIXED_CHANNEL = """=== Message from External Author <external@partner.example> (UEXT3, external: Partner Org (APAC)) at 2026-08-27 10:03:00 +07 ===
Message TS: 103.000000
external channel message
=== Message from Internal Author <internal@example.com> (UINT2) at 2026-08-27 10:04:00 +07 ===
Message TS: 104.000000
internal channel message
"""


class SlackMessageParserTest(unittest.TestCase):
    def test_external_channel_author_is_counted_and_filterable(self):
        got = slack_utils.parse_slack_messages(MIXED_CHANNEL, 100.0, ["UEXT3"])
        self.assertEqual(got, (1, "external channel message"))

    def test_external_channel_author_advances_page_coverage(self):
        got = slack_utils._parse_page(MIXED_CHANNEL, 100.0, ["UEXT3"])
        self.assertEqual(got, (1, "external channel message", 103.0, False, 2))

    def test_channel_header_tolerates_trailing_whitespace_and_crlf(self):
        text = MIXED_CHANNEL.replace(" ===\n", " === \r\n")
        got = slack_utils._parse_page(text, 100.0, ["UEXT3"])
        self.assertEqual(got, (1, "external channel message", 103.0, False, 2))


class SlackThreadParserTest(unittest.TestCase):
    def test_internal_only_thread_replies_are_counted(self):
        got = slack_utils._parse_thread_page(THREAD, 100.0)
        self.assertEqual(got[:3], (2, "second reply", 102.0))
        self.assertEqual(got[3:], (True, 3))

    def test_external_parent_is_parsed_but_never_counted(self):
        parent = EXTERNAL_THREAD.split("=== THREAD REPLIES", 1)[0]
        got = slack_utils._parse_thread_page(parent, 0.0)
        self.assertEqual(got, (0, "", 0.0, False, 1))

    def test_external_reply_is_counted_and_filterable_by_user_id(self):
        got = slack_utils._parse_thread_page(EXTERNAL_THREAD, 100.0, ["UEXT2"])
        self.assertEqual(got, (1, "external reply", 102.0, True, 3))

    def test_mixed_internal_and_external_authors_are_counted(self):
        got = slack_utils._parse_thread_page(EXTERNAL_THREAD, 100.0)
        self.assertEqual(got, (2, "external reply", 102.0, True, 3))

    def test_unknown_author_annotation_still_fails_closed(self):
        malformed = EXTERNAL_THREAD.replace(
            ", external: Another Organization (APAC)", ", guest: Another Organization (APAC)"
        )
        with self.assertRaisesRegex(ValueError, r"incomplete slack thread response record$"):
            slack_utils._parse_thread_page(malformed, 100.0)

    def test_thread_author_filter_is_applied_to_replies(self):
        got = slack_utils._parse_thread_page(THREAD, 100.0, ["U1"])
        self.assertEqual(got[:3], (1, "first reply *Sent using* <@U123|Sidequestor>", 101.0))
        self.assertEqual(got[4], 3)

    def test_thread_keyword_filter_is_applied_to_replies(self):
        got = slack_utils._parse_thread_page(THREAD, 100.0, filter_keywords=["SECOND"])
        self.assertEqual(got[:3], (1, "second reply", 102.0))

    def test_old_reply_is_raw_coverage_but_not_activity(self):
        got = slack_utils._parse_thread_page(THREAD, 101.0)
        self.assertEqual(got[:3], (1, "second reply", 102.0))
        self.assertEqual(got[3:], (True, 3))

    def test_parent_is_never_counted_as_thread_activity(self):
        parent = THREAD.split("=== THREAD REPLIES", 1)[0]
        got = slack_utils._parse_thread_page(parent, 0.0)
        self.assertEqual(got[:3], (0, "", 0.0))
        self.assertEqual(got[3:], (False, 1))

    def test_parent_can_be_counted_for_an_explicit_handoff(self):
        parent = THREAD.split("=== THREAD REPLIES", 1)[0]
        got = slack_utils._parse_thread_page(parent, 99.999999, include_parent=True)
        self.assertEqual(got, (1, "parent", 100.0, False, 1))

    def test_empty_response_is_clean(self):
        self.assertEqual(
            slack_utils._parse_thread_page("", 100.0),
            (0, "", 0.0, False, 0),
        )

    def test_incomplete_record_fails_closed(self):
        malformed = """--- Reply 1 of 1 ---
Message TS: 101.000000
reply without an author
"""
        with self.assertRaisesRegex(ValueError, "incomplete slack thread response record"):
            slack_utils._parse_thread_page(malformed, 100.0)

    def test_extra_timestamp_in_record_fails_closed(self):
        malformed = THREAD.replace("first reply", "first reply\nMessage TS: 999.000000")
        with self.assertRaisesRegex(ValueError, "incomplete slack thread response records"):
            slack_utils._parse_thread_page(malformed, 100.0)

    def test_unknown_record_delimiter_fails_closed(self):
        malformed = """=== THREAD PARENT MESSAGE ===
From: Guangmian Kung <g@example.com> (U1)
Message TS: 100.000000
parent

=== THREAD RESPONSES ===
From: Someone Else <s@example.com> (U2)
Message TS: 102.000000
reply
"""
        with self.assertRaisesRegex(ValueError, "incomplete slack thread response records"):
            slack_utils._parse_thread_page(malformed, 100.0)

    def test_pagination_does_not_count_repeated_parent(self):
        parent = THREAD.split("=== THREAD REPLIES", 1)[0]
        pages = [
            (parent + """=== THREAD REPLIES (1 total) ===

--- Reply 1 of 1 ---
From: Guangmian Kung <g@example.com> (U1)
Time: 2026-08-27 10:01:00 +07
Message TS: 101.000000
first reply
*Sent using* <@U123|Sidequestor>
""", "next", None),
            (parent + """=== THREAD REPLIES (1 total) ===

--- Reply 1 of 1 ---
From: Someone Else <s@example.com> (U2)
Time: 2026-08-27 10:02:00 +07
Message TS: 102.000000
second reply
""", None, None),
        ]

        def fetch_page(_cursor, _oldest, _latest):
            return pages.pop(0)

        got = slack_utils.drain(
            fetch_page, 100.0, now=200.0,
            page_parser=slack_utils._parse_thread_page,
        )
        self.assertEqual(got, (2, "first reply *Sent using* <@U123|Sidequestor>",
                               102.0, True, None))


class SlackThreadHandoffCheckerTest(unittest.TestCase):
    def _check(self, messages: str, target_ts: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="sidequestor-slack-handoff-") as raw:
            mcp = Path(raw) / "mcp-call"
            payload = json.dumps({"messages": messages, "pagination": ""})
            mcp.write_text("#!/bin/sh\nprintf '%s\\n' '" + payload + "'\n")
            mcp.chmod(0o755)
            watch = {
                "type": "slack_thread",
                "channel_id": "C0AAAA1",
                "thread_ts": "100.000000",
                "last_checked_ts": "99.999999",
                "include_parent": True,
                "one_shot_until_ts": target_ts,
                "reason": "retry blocked mention",
            }
            result = subprocess.run(
                [sys.executable, str(CHECKERS / "slack_thread.py"), json.dumps(watch)],
                text=True, capture_output=True, env={**os.environ, "MCP_CALL": str(mcp)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_handoff_target_parent_dispatches(self):
        parent = THREAD.split("=== THREAD REPLIES", 1)[0]
        result = self._check(parent, "100.000000")
        self.assertEqual(result["outcome"], "dirty")
        self.assertEqual(result["advance_to"], "100.000000")

    def test_missing_handoff_target_fails_closed(self):
        parent = THREAD.split("=== THREAD REPLIES", 1)[0]
        result = self._check(parent, "101.000000")
        self.assertEqual(result["outcome"], "error")
        self.assertFalse(result["complete"])
        self.assertIn("target message not observed", result["reason"])

    def test_handoff_excludes_messages_after_target(self):
        result = self._check(THREAD, "100.000000")
        self.assertEqual(result["outcome"], "dirty")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["advance_to"], "100.000000")


if __name__ == "__main__":
    unittest.main()
