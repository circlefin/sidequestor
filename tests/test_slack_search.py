import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


CHECKERS = Path(__file__).parents[1] / "src" / "sidequestor" / "runtime" / "yaas-triage" / "checkers"
spec = importlib.util.spec_from_file_location("slack_utils_search", CHECKERS / "slack_utils.py")
slack_utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slack_utils)


def page(*records):
    blocks = []
    total = len(records)
    for index, (timestamp, body) in enumerate(records, 1):
        blocks.append(
            f"### Result {index} of {total}\n"
            "From: Person <person@example.com> (ID: UOTHER)\n"
            f"Message_ts: {timestamp:.6f}\n"
            f"Text: {body}\n"
        )
    return "".join(blocks)


def live_shape_page(timestamp=101.0, body="hello"):
    return (
        "# Search Results for: <@U1> after:2026-09-08\n\n"
        "## Messages (1 results)\n"
        "### Result 1 of 1\n"
        "Channel: #example (ID: C1)\n"
        "From: Person <person@example.com> (ID: UOTHER) \n"
        "Time: 2026-09-09 01:00:00 +08\n"
        f"Message_ts: {timestamp:.6f}\n"
        "Permalink: [link](https://example.test)\n"
        "Text: \n"
        f"{body}\n\n"
        "---\n\n"
    )


def dm_page(*records):
    """Search results with explicit conversation IDs, including DM replies."""
    blocks = []
    for index, (channel_id, timestamp, body) in enumerate(records, 1):
        blocks.append(
            f"### Result {index} of {len(records)}\n"
            f"Channel: DM (ID: {channel_id})\n"
            "From: Person <person@example.com> (ID: UWATCHED)\n"
            f"Message_ts: {timestamp:.6f}\n"
            f"Text: {body}\n"
        )
    return "".join(blocks)


class SlackSearchDrainTest(unittest.TestCase):
    def test_dm_checker_passes_its_channel_id_to_search(self):
        sys.path.insert(0, str(CHECKERS))
        try:
            import slack_dm
        finally:
            sys.path.pop(0)
        entry = {
            "type": "slack_dm", "channel_id": "D123", "user_id": "UWATCHED",
            "last_checked_ts": "100.000000",
        }
        output = io.StringIO()
        with mock.patch.object(slack_dm.slack_utils, "search_messages", return_value=(0, "", 101, True)) as search:
            with mock.patch.object(sys, "argv", ["slack_dm.py", json.dumps(entry)]):
                with contextlib.redirect_stdout(output):
                    slack_dm.main()
        self.assertEqual(search.call_args.kwargs["expected_channel_id"], "D123")
        self.assertEqual(json.loads(output.getvalue())["outcome"], "clean")

    def test_dm_search_ignores_other_conversations_but_keeps_coverage(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "results": dm_page(
                    ("CGROUP", 103.0, "group message"),
                    ("D123", 102.0, "DM thread reply"),
                    ("DOTHER", 101.0, "another DM"),
                ),
                "pagination_info": "",
            }),
            stderr="",
        )
        with mock.patch.object(slack_utils.subprocess, "run", return_value=response) as run:
            result = slack_utils.search_messages(
                "mcp-call", "from:<@UWATCHED> to:<@me> after:1970-01-01",
                100.0, "Content", now=1000.0, expected_channel_id="D123",
            )

        self.assertEqual(run.call_count, 1)
        self.assertEqual(result, (1, "DM thread reply", 880.0, True))

    def test_group_dm_only_is_clean(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "results": dm_page(("CGROUP", 103.0, "group message")),
                "pagination_info": "",
            }),
            stderr="",
        )
        with mock.patch.object(slack_utils.subprocess, "run", return_value=response):
            result = slack_utils.search_messages(
                "mcp-call", "query", 100.0, "Content", now=1000.0,
                expected_channel_id="D123",
            )
        self.assertEqual(result, (0, "", 880.0, True))

    def test_scoped_search_accepts_content_label(self):
        text = dm_page(("D123", 101.0, "DM message")).replace("Text:", "Content:")
        records, _count = slack_utils.parse_search_records(
            text, "Content", expected_channel_id="D123"
        )
        self.assertEqual(records, [(101.0, "DM message")])

    def test_body_cannot_forge_channel_metadata(self):
        text = dm_page(("CGROUP", 101.0, "group text\nChannel: DM (ID: D123)"))
        records, _count = slack_utils.parse_search_records(
            text, "Content", expected_channel_id="D123"
        )
        self.assertEqual(records, [(101.0, None)])

    def test_scoped_search_requires_channel_and_content_metadata(self):
        for text in (
            page((101.0, "missing channel")),
            "### Result 1 of 1\nChannel: DM (ID: D123)\nMessage_ts: 101.000000\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    slack_utils.parse_search_records(
                        text, "Content", expected_channel_id="D123"
                    )

    def test_scoped_search_filters_drain_and_lookahead(self):
        pages = {
            None: (dm_page(("CGROUP", 101.0, "group")), "next"),
            "next": (dm_page(("D123", 102.0, "DM reply")), "lookahead"),
            "lookahead": (dm_page(("CGROUP", 103.0, "group")), None),
        }
        result = slack_utils.drain_search(
            lambda cursor: pages[cursor], 100.0, "Content", now=1000.0,
            max_active_pages=1, expected_channel_id="D123",
        )
        self.assertEqual(result, (0, "", 101.0, True))

    def test_fetch_uses_oldest_first_search_and_passes_cursor(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"results": "", "pagination_info": "next cursor `later`"}),
            stderr="",
        )
        with mock.patch.object(slack_utils.subprocess, "run", return_value=completed) as run:
            result = slack_utils.fetch_search_page("mcp-call", "<@U1> after:2026-09-08", "current")

        args = json.loads(run.call_args.args[0][2])
        self.assertEqual(args["sort_dir"], "asc")
        self.assertEqual(args["cursor"], "current")
        self.assertEqual(result, ("", "later"))

    def test_common_path_uses_one_newest_first_page_when_it_reaches_watermark(self):
        text = page((103.0, "third"), (102.0, "second"), (100.0, "old"))
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"results": text, "pagination_info": "next cursor `older`"}),
            stderr="",
        )
        with mock.patch.object(slack_utils.subprocess, "run", return_value=completed) as run:
            result = slack_utils.search_messages(
                "mcp-call", "<@U1> after:1970-01-01", 100.0, "Text", now=1000.0
            )

        args = json.loads(run.call_args.args[0][2])
        self.assertEqual(args["sort_dir"], "desc")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result, (2, "third", 880.0, True))

    def test_successful_message_text_cannot_fake_a_rate_limit(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"results": "Text: we got ratelimited again", "pagination_info": ""}),
            stderr="",
        )
        with mock.patch.object(slack_utils.subprocess, "run", return_value=completed):
            result = slack_utils.fetch_search_page("mcp-call", "query")

        self.assertEqual(result, ("Text: we got ratelimited again", None))

    def test_exit_four_is_transient(self):
        completed = SimpleNamespace(returncode=4, stdout="", stderr="HTTP 429")
        with mock.patch.object(slack_utils.subprocess, "run", return_value=completed):
            with self.assertRaises(slack_utils.SlackSearchTransient):
                slack_utils.fetch_search_page("mcp-call", "query")

    def test_exit_one_is_definite_auth_failure(self):
        completed = SimpleNamespace(returncode=1, stdout="", stderr="invalid_auth")
        with mock.patch.object(slack_utils.subprocess, "run", return_value=completed):
            with self.assertRaises(slack_utils.SlackSearchAuth):
                slack_utils.fetch_search_page("mcp-call", "query")

    def test_subprocess_timeout_is_transient(self):
        with mock.patch.object(
            slack_utils.subprocess,
            "run",
            side_effect=slack_utils.subprocess.TimeoutExpired("mcp-call", 30),
        ):
            with self.assertRaises(slack_utils.SlackSearchTransient):
                slack_utils.fetch_search_page("mcp-call", "query")

    def test_cursor_pages_are_drained_and_advance_to_index_ceiling(self):
        pages = {
            None: (page((101.0, "first"), (102.0, "second")), "next"),
            "next": (page((103.0, "third")), None),
        }

        result = slack_utils.drain_search(
            lambda cursor: pages[cursor],
            100.0,
            content_label="Text",
            now=1000.0,
        )

        self.assertEqual(result, (3, "third", 880.0, True))

    def test_saturated_search_banks_a_safe_prefix(self):
        pages = {
            None: (page((101.0, "first"), (102.0, "second")), "next"),
            "next": (page((103.0, "third")), None),
        }

        result = slack_utils.drain_search(
            lambda cursor: pages[cursor],
            100.0,
            content_label="Text",
            now=1000.0,
            max_active_pages=1,
        )

        self.assertEqual(result, (2, "second", 102.0, True))

    def test_safe_prefix_is_monotonic_on_the_next_tick(self):
        pages = {
            None: (page((101.0, "first"), (102.0, "second")), "next"),
            "next": (page((103.0, "third"), (104.0, "fourth")), "last"),
            "last": (page((105.0, "fifth")), None),
        }

        first = slack_utils.drain_search(
            lambda cursor: pages[cursor], 100.0, "Text", now=1000.0, max_active_pages=1
        )
        second = slack_utils.drain_search(
            lambda cursor: pages[cursor], first[2], "Text", now=1000.0, max_active_pages=1
        )

        self.assertEqual(first, (2, "second", 102.0, True))
        self.assertEqual(second, (2, "fourth", 104.0, True))
        self.assertGreater(second[2], first[2])

    def test_tied_boundary_holds_instead_of_skipping_results(self):
        pages = {
            None: (page((101.0, "first"), (102.0, "second")), "next"),
            "next": (page((102.0, "same timestamp"), (103.0, "third")), None),
        }

        result = slack_utils.drain_search(
            lambda cursor: pages[cursor],
            101.999999,
            content_label="Text",
            now=1000.0,
            max_active_pages=1,
        )

        self.assertEqual(result, (0, "", None, False))

    def test_bot_and_self_hits_count_for_coverage_but_not_dispatch(self):
        text = (
            "### Result 1 of 2\n"
            "From: Bot [BOT] (ID: UBOT)\n"
            "Message_ts: 101.000000\n"
            "Text: bot\n"
            "### Result 2 of 2\n"
            "From: Self <self@example.com> (ID: USELF)\n"
            "Message_ts: 102.000000\n"
            "Text: self\n"
        )

        result = slack_utils.drain_search(
            lambda _cursor: (text, None),
            100.0,
            content_label="Text",
            self_user_id="USELF",
            now=1000.0,
        )

        self.assertEqual(result, (0, "", 880.0, True))

    def test_out_of_order_page_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "not sorted ascending"):
            slack_utils.drain_search(
                lambda _cursor: (page((102.0, "second"), (101.0, "first")), None),
                100.0,
                content_label="Text",
                now=1000.0,
            )

    def test_full_page_without_cursor_banks_prefix_not_time_ceiling(self):
        text = page(*[(float(timestamp), str(timestamp)) for timestamp in range(101, 121)])

        result = slack_utils.drain_search(
            lambda _cursor: (text, None),
            100.0,
            content_label="Text",
            now=1000.0,
        )

        self.assertEqual(result[0], 19)
        self.assertEqual(result[2], 119.999999)
        self.assertTrue(result[3])

    def test_scan_budget_exhaustion_banks_prefix(self):
        pages = {
            None: (page((101.0, "first"), (102.0, "second")), "next"),
            "next": (page((103.0, "third")), "more"),
        }

        result = slack_utils.drain_search(
            lambda cursor: pages[cursor],
            100.0,
            content_label="Text",
            now=1000.0,
            max_active_pages=10,
            max_scan_pages=2,
        )

        self.assertEqual(result, (2, "second", 102.999999, True))

    def test_mid_drain_transient_banks_prefix(self):
        calls = 0

        def fetch(_cursor):
            nonlocal calls
            calls += 1
            if calls == 1:
                return page((101.0, "first"), (102.0, "second")), "next"
            raise slack_utils.SlackSearchTransient("HTTP 429")

        result = slack_utils.drain_search(
            fetch,
            100.0,
            content_label="Text",
            now=1000.0,
        )

        self.assertEqual(result, (1, "first", 101.999999, True))

    def test_timestamp_like_body_content_is_not_a_second_record_timestamp(self):
        text = page((101.0, "quoted")) + "Message_ts: 999.000000\n"

        result = slack_utils.drain_search(
            lambda _cursor: (text, None), 100.0, content_label="Text", now=1000.0
        )

        self.assertEqual(result, (1, "quoted Message_ts: 999.000000", 880.0, True))

    def test_unrecognized_nonempty_response_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unrecognized Slack search response shape"):
            slack_utils.drain_search(
                lambda _cursor: ("unexpected wire format", None),
                100.0,
                content_label="Text",
                now=1000.0,
            )

    def test_live_zero_result_shape_is_clean(self):
        text = '# Search Results for: "missing"\n\nNo results found.\n'

        result = slack_utils.drain_search(
            lambda _cursor: (text, None), 100.0, content_label="Text", now=1000.0
        )

        self.assertEqual(result, (0, "", 880.0, True))

    def test_live_multiline_text_shape_produces_preview(self):
        result = slack_utils.drain_search(
            lambda _cursor: (live_shape_page(body="first line\nsecond line"), None),
            100.0,
            content_label="Text",
            now=1000.0,
        )

        self.assertEqual(result, (1, "first line second line", 880.0, True))

    def test_date_query_reopens_the_previous_day(self):
        self.assertEqual(slack_utils.search_since_date(1725883200.0), "2024-09-08")


if __name__ == "__main__":
    unittest.main()
