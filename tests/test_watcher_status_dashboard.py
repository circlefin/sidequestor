from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TRIAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sidequestor" / "runtime" / "yaas-triage"
RUNTIME_ROOT = TRIAGE_ROOT.parent


class WatcherStatusDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-watch-status-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "YAAS_WORKSPACE": str(self.workspace),
            "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
            "SIDEQUESTOR_CHECKER_CONNECTORS": "slack,email,github,jira,x",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        sys.path.insert(0, str(TRIAGE_ROOT))
        self.addCleanup(lambda: sys.path.remove(str(TRIAGE_ROOT)))
        spec = importlib.util.spec_from_file_location(
            "sidequestor_dashboard_watch_status_test", TRIAGE_ROOT / "ops" / "dashboard-server.py")
        self.server = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.object(sys, "argv", ["dashboard-server.py"]):
            spec.loader.exec_module(self.server)

    def _quest(self) -> Path:
        quest = self.workspace / "state" / "quests" / "active" / "quest-one"
        quest.mkdir(parents=True)
        (quest / "meta.json").write_text(json.dumps({"id": "quest-one", "title": "Quest one",
                                                    "status": "active"}))
        (quest / "watch.json").write_text(json.dumps({"watches": [
            {"watch_id": "watch-abcd", "type": "x_mentions", "last_checked_ts": "0"},
        ]}))
        return quest

    def _snapshot(self, issues: list[dict]) -> None:
        triage = self.workspace / "state" / "triage"
        triage.mkdir(parents=True, exist_ok=True)
        (triage / "last-run.json").write_text(json.dumps({"watch_issues": issues}))

    def test_transient_skip_is_visible_but_does_not_request_a_call(self) -> None:
        self._quest()
        self._snapshot([{"quest": "quest-one", "watch_id": "watch-abcd",
                         "type": "x_mentions", "status": "skip", "reason": "timeout"}])
        quest = self.server.build_dashboard()["quests"][0]
        self.assertTrue(quest["ratelimited"])
        self.assertEqual(self.server._quest_watch_issues(quest)[0]["title"],
                         "Temporary read interruption")
        with patch.object(self.server, "build_dashboard", return_value={
                "quests": [quest], "workspace": {}, "triage": {}, "live_run": {}}), \
                patch.object(self.server, "build_messages", return_value={
                    "needs_you": [], "other_actions": [], "queued_items": [],
                    "recent_activity": []}), \
                patch.object(self.server, "build_history", return_value={}), \
                patch.object(self.server, "build_reaction_emojis", return_value={}):
            self.assertEqual(self.server.build_control()["attention"], [])

    def test_explicit_misconfig_clears_on_next_successful_snapshot(self) -> None:
        self._quest()
        self._snapshot([{"quest": "quest-one", "watch_id": "watch-abcd",
                         "type": "x_mentions", "status": "misconfig",
                         "reason": "token revoked"}])
        quest = self.server.build_dashboard()["quests"][0]
        issue = self.server._quest_watch_issues(quest)[0]
        self.assertEqual(issue["severity"], "action")
        self.assertIn("sq x-auth status", issue["next"])
        detail = self.server.build_quest_detail("quest-one")
        self.assertEqual(detail["open_items"]["watch_issues"][0]["severity"], "action")
        with patch.object(self.server, "build_dashboard", return_value={
                "quests": [quest], "workspace": {}, "triage": {}, "live_run": {}}), \
                patch.object(self.server, "build_messages", return_value={
                    "needs_you": [], "other_actions": [], "queued_items": [],
                    "recent_activity": []}), \
                patch.object(self.server, "build_history", return_value={}), \
                patch.object(self.server, "build_reaction_emojis", return_value={}):
            attention = self.server.build_control()["attention"]
        self.assertEqual(attention[0]["priority"], "high")
        self._snapshot([])
        recovered = self.server.build_dashboard()["quests"][0]
        self.assertEqual(recovered["misconfigured_watches"], [])

    def test_repeated_checker_error_keeps_retry_language(self) -> None:
        self._quest()
        self._snapshot([{"quest": "quest-one", "watch_id": "watch-abcd",
                         "type": "x_mentions", "status": "misconfig", "error": True,
                         "reason": "6 consecutive checker errors"}])
        self.assertEqual(self.server.build_dashboard()["quests"][0]["misconfigured_watches"], [])
        issue = self.server._watch_issue("backoff", {
            "type": "slack_thread", "source": "checker", "count": 27,
            "last_error": "invalid_auth", "next_retry_ts": "9999999999",
        }, now=0)
        self.assertEqual(issue["severity"], "uncertain")
        self.assertIn("Automatic retry", issue["next"])

    def test_recovered_watcher_hold_is_not_a_current_quest_blocker(self) -> None:
        quest = self._quest()
        (quest / "watch.json").write_text(json.dumps({"watches": [{
            "watch_id": "watch-dm",
            "type": "slack_dm",
            "channel_id": "D0A0L38D18V",
            "last_checked_ts": "1790738206.609046",
        }]}))
        (quest / "timeline.ndjson").write_text(json.dumps({
            "ts": "2026-09-30T00:02:39Z",
            "event": "blocked",
            "channel_id": "D0A0L38D18V",
            "reason": "Holding watermark for retry rather than acknowledging an unobserved event.",
        }) + "\n")

        dashboard_quest = self.server.build_dashboard()["quests"][0]
        self.assertIsNone(dashboard_quest["last_blocked"])
        detail = self.server.build_quest_detail("quest-one")
        self.assertIsNotNone(detail)
        self.assertIsNone(detail["open_items"]["blocked"])

    def test_unrecovered_watcher_hold_remains_visible(self) -> None:
        quest = self._quest()
        (quest / "watch.json").write_text(json.dumps({"watches": [{
            "watch_id": "watch-dm",
            "type": "slack_dm",
            "channel_id": "D0A0L38D18V",
            "last_checked_ts": "1790720000.000000",
        }]}))
        (quest / "timeline.ndjson").write_text(json.dumps({
            "ts": "2026-09-30T00:02:39Z",
            "event": "blocked",
            "channel_id": "D0A0L38D18V",
            "reason": "Holding watermark for retry rather than acknowledging an unobserved event.",
        }) + "\n")

        dashboard_quest = self.server.build_dashboard()["quests"][0]
        self.assertEqual(
            dashboard_quest["last_blocked"]["reason"],
            "Holding watermark for retry rather than acknowledging an unobserved event.",
        )

    def test_advanced_watermark_does_not_clear_a_business_blocker(self) -> None:
        quest = self._quest()
        (quest / "watch.json").write_text(json.dumps({"watches": [{
            "watch_id": "watch-dm",
            "type": "slack_dm",
            "channel_id": "D0A0L38D18V",
            "last_checked_ts": "1790738206.609046",
        }]}))
        (quest / "timeline.ndjson").write_text(json.dumps({
            "ts": "2026-09-30T00:02:39Z",
            "event": "blocked",
            "channel_id": "D0A0L38D18V",
            "reason": "Waiting for the API owner to confirm the production key scope.",
        }) + "\n")

        dashboard_quest = self.server.build_dashboard()["quests"][0]
        self.assertEqual(
            dashboard_quest["last_blocked"]["reason"],
            "Waiting for the API owner to confirm the production key scope.",
        )

    def test_recovered_watcher_hold_does_not_hide_earlier_business_blocker(self) -> None:
        quest = self._quest()
        (quest / "watch.json").write_text(json.dumps({"watches": [{
            "watch_id": "watch-dm",
            "type": "slack_dm",
            "channel_id": "D0A0L38D18V",
            "last_checked_ts": "1790738206.609046",
        }]}))
        business_reason = "Waiting for the API owner to confirm the production key scope."
        events = [
            {"ts": "2026-09-30T00:01:00Z", "event": "blocked",
             "reason": business_reason},
            {"ts": "2026-09-30T00:02:39Z", "event": "blocked",
             "channel_id": "D0A0L38D18V",
             "reason": "Holding watermark for retry rather than acknowledging an unobserved event."},
        ]
        (quest / "timeline.ndjson").write_text(
            "".join(json.dumps(event) + "\n" for event in events))

        dashboard_quest = self.server.build_dashboard()["quests"][0]
        self.assertEqual(dashboard_quest["last_blocked"]["reason"], business_reason)
        detail = self.server.build_quest_detail("quest-one")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["open_items"]["blocked"]["reason"], business_reason)
        self.assertEqual(self.server.build_open_items()["blocked"][0]["reason"],
                         business_reason)

    def test_jira_surface_auth_exit_is_explicit_misconfig(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "sidequestor_jira_watch_status_test", TRIAGE_ROOT / "checkers" / "jira.py")
        jira = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(jira)
        completed = SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Jira HTTP 401")
        with patch.object(jira.subprocess, "run", return_value=completed):
            with self.assertRaises(jira.Misconfig):
                jira.jira_get("/rest/api/3/search")


if __name__ == "__main__":
    unittest.main()
