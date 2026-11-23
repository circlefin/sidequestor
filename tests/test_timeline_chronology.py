"""Historical information retains source time without backdating performed work."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TRIAGE = ROOT / "src/sidequestor/runtime/yaas-triage"
sys.path.insert(0, str(TRIAGE))
from timeline_time import display_timestamp, source_event_time
sys.path.remove(str(TRIAGE))


class TimelineChronologyTests(unittest.TestCase):
    def test_source_time_accepts_seconds_and_explicit_timezone(self):
        for source in (1767225600, 1767225600.125, "1767225600.125"):
            value = source_event_time(source)
            self.assertTrue(value.startswith("2026-01-01T00:00:00"), value)
        self.assertEqual(source_event_time("2026-01-01T08:30:00+08:00"),
                         "2026-01-01T00:30:00Z")

    def test_invalid_or_future_source_time_cannot_replace_recording_time(self):
        recorded = "2026-11-07T12:00:00Z"
        for source in (True, {}, [], 0, -1, float("nan"), float("inf"),
                       "not-a-time", "2026-01-01", "2026-01-01T08:30:00",
                       "2026-13-01T08:30:00Z", "2027-01-01T00:00:00Z",
                       1767225600000):
            with self.subTest(source=source):
                self.assertIsNone(source_event_time(source, recorded))
                self.assertEqual(display_timestamp({"event": "info_received", "ts": recorded,
                                                     "source_event_ts": source}), recorded)

    def test_recording_second_precision_preserves_legitimate_fractional_message_time(self):
        recorded = "2026-11-07T12:00:00Z"
        self.assertEqual(source_event_time("2026-11-07T12:00:00.999999Z", recorded),
                         "2026-11-07T12:00:00.999999Z")
        self.assertIsNone(source_event_time("2026-11-07T12:00:01Z", recorded))
        self.assertIsNone(source_event_time("2026-11-07T12:00:00.600Z",
                                           "2026-11-07T12:00:00.500Z"))

    def test_only_explicit_information_source_time_changes_chronology(self):
        recorded = "2026-11-07T12:00:00Z"
        source = "2026-10-01T08:30:00+08:00"
        for event in ("info_received", "info_logged", "message_received", "email_received"):
            self.assertEqual(display_timestamp({"event": event, "ts": recorded,
                                                "source_event_ts": source}), "2026-10-01T00:30:00Z")
        for event in ("message_sent", "draft_posted", "executed", "watch_fired", "blocked",
                      "dispatch_started", "message_delivery_uncertain", "note"):
            self.assertEqual(display_timestamp({"event": event, "ts": recorded,
                                                "source_event_ts": source}), recorded)
        # These existing fields have other meanings, so old logs keep their dates.
        for field in ("source_ts", "message_ts", "thread_ts", "response_ts"):
            self.assertEqual(display_timestamp({"event": "info_received", "ts": recorded,
                                                field: source}), recorded)
        self.assertIsNone(display_timestamp({"event": "info_received", "source_event_ts": source}))

    def _log(self, payload, flags=False):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "state/quests/active/quest-test"
            folder.mkdir(parents=True)
            args = (["--quest-id", "quest-test", "--event", "info_received",
                     "--source-event-ts", payload["source_event_ts"]] if flags else
                    [json.dumps(dict(payload, quest_id="quest-test"))])
            result = subprocess.run([sys.executable, str(TRIAGE / "surfaces/log-event.py"), *args],
                env={**os.environ, "SIDEQUESTOR_WORKSPACE": raw}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads((folder / "timeline.ndjson").read_text()), result

    def test_logger_keeps_clock_stamp_and_normalizes_explicit_source_time(self):
        row, result = self._log({"event": "info_received", "ts": "1999-01-01T00:00:00Z",
                                "source_event_ts": "2026-01-01T08:30:00+08:00",
                                "message_text": "historical message"})
        self.assertNotEqual(row["ts"], "1999-01-01T00:00:00Z")
        self.assertEqual(row["ts"], json.loads(result.stdout)["ts"])
        self.assertEqual(row["source_event_ts"], "2026-01-01T00:30:00Z")
        self.assertEqual(row["message_text"], "historical message")
        self.assertEqual(json.loads(result.stdout)["ts_overridden"], "1999-01-01T00:00:00Z")

    def test_invalid_source_time_does_not_lose_the_log(self):
        row, result = self._log({"event": "info_received", "source_event_ts": "tomorrow",
                                "note": "still log this"})
        self.assertNotIn("source_event_ts", row)
        self.assertEqual(row["note"], "still log this")
        self.assertIn("source_event_ts ignored", result.stderr)

    def test_source_time_flag_matches_json_contract(self):
        row, _ = self._log({"source_event_ts": "1767225600.125"}, flags=True)
        self.assertEqual(row["source_event_ts"], "2026-01-01T00:00:00.125000Z")

    def test_dashboard_activity_uses_source_time_but_identity_and_audit_use_recording(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
                "SIDEQUESTOR_WORKSPACE": raw, "YAAS_WORKSPACE": raw,
                "SIDEQUESTOR_RUNTIME_ROOT": str(TRIAGE.parent), "YAAS_RUNTIME_ROOT": str(TRIAGE.parent)}):
            spec = importlib.util.spec_from_file_location("dashboard_chronology", TRIAGE / "ops/dashboard-server.py")
            server = importlib.util.module_from_spec(spec)
            with patch.object(sys, "argv", ["dashboard-server.py"]):
                spec.loader.exec_module(server)
            recorded = "2026-11-07T12:00:00Z"
            source = "2026-10-01T00:30:00Z"
            folder = Path(raw) / "state/quests/active/quest-test"
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text('{"title":"Historical messages"}')
            row = {"event": "info_received", "ts": recorded, "source_event_ts": source,
                   "message_text": "historical message"}
            (folder / "timeline.ndjson").write_text(json.dumps(row) + "\n")
            projected = server.build_messages()["recent_activity"][0]
            self.assertEqual(projected["ts"], recorded)
            self.assertEqual(projected["source_event_ts"], source)
            activity = server._control_activity(projected)
            self.assertEqual(activity["timestamp"], source)
            self.assertEqual(activity["recorded_at"], recorded)
            self.assertEqual(activity["id"], f"quest-test:{recorded}:info_received")
            # Lexical ISO ordering puts .900 before Z; source chronology must use instants.
            action = {"event": "executed", "ts": source, "note": "work then"}
            row["source_event_ts"] = "2026-10-01T00:30:00.900Z"
            (folder / "timeline.ndjson").write_text(json.dumps(action) + "\n" + json.dumps(row) + "\n")
            recent = server.build_messages()["recent_activity"]
            self.assertEqual([r["event"] for r in recent], ["info_received", "executed"])

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for the actual quest-trail check")
    def test_quest_detail_to_trail_preserves_validated_source_time_without_replay(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
                "SIDEQUESTOR_WORKSPACE": raw, "YAAS_WORKSPACE": raw,
                "SIDEQUESTOR_RUNTIME_ROOT": str(TRIAGE.parent), "YAAS_RUNTIME_ROOT": str(TRIAGE.parent)}):
            spec = importlib.util.spec_from_file_location("dashboard_detail_chronology", TRIAGE / "ops/dashboard-server.py")
            server = importlib.util.module_from_spec(spec)
            with patch.object(sys, "argv", ["dashboard-server.py"]):
                spec.loader.exec_module(server)
            folder = Path(raw) / "state/quests/active/quest-test"
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text('{"title":"Historical messages"}')
            source = "2026-01-01T00:30:00Z"
            rows = [
                {"event": "info_received", "ts": "2026-11-07T12:00:00Z", "source_event_ts": source,
                 "note": "historical"},
                {"event": "info_received", "ts": "2026-11-07T12:00:01Z", "source_event_ts": "not-a-time",
                 "note": "invalid source"},
                {"event": "executed", "ts": "2026-11-07T12:00:02Z", "source_event_ts": source,
                 "display_timestamp": "2099-01-01T00:00:00Z", "note": "later action"},
                {"event": "executed", "ts": 1767312000, "note": "numeric action"},
                {"event": "executed", "ts": "2026-11-07T12:00:03", "note": "legacy naive action"},
            ]
            (folder / "timeline.ndjson").write_text("\n".join(map(json.dumps, rows)) + "\n")
            before = (folder / "timeline.ndjson").read_bytes()
            for replay in ("1", "0"):
                with self.subTest(replay=replay), patch.dict(os.environ, {"SIDEQUESTOR_QUEST_VISUALIZER": replay}):
                    detail = server.build_quest_detail("quest-test")
                    projected = {row["note"]: row for row in detail["timeline"]}
                    self.assertEqual(projected["historical"]["source_event_ts"], source)
                    self.assertEqual(projected["historical"]["display_timestamp"], source)
                    self.assertEqual(projected["historical"]["ts"], rows[0]["ts"])
                    self.assertEqual(projected["invalid source"]["display_timestamp"], rows[1]["ts"])
                    self.assertEqual(projected["later action"]["display_timestamp"], rows[2]["ts"])
                    activity = [server._control_activity(r) for r in server.build_messages()["recent_activity"]]
                    fixture = {"replay": replay == "1", "detail": detail, "activity": activity}
                    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),fixture=JSON.parse(fs.readFileSync(0,'utf8'));
const html=fs.readFileSync('src/sidequestor/runtime/dashboard.html','utf8');
const definitions=['normalizeTimeline','trailEvents'].map(name=>html.split('\n').find(l=>l.startsWith('function '+name+'(')));
const ctx={state:{selected:'quest-test',detail:fixture.detail,control:{quests:[{id:'quest-test',title:'Historical messages'}],activity:fixture.activity}}};
if(fixture.replay)ctx.SQQuestVisualizer=require('./src/sidequestor/runtime/dashboard-quest-visualizer.js');
vm.createContext(ctx);vm.runInContext(definitions.join('\n'),ctx);
process.stdout.write(JSON.stringify(ctx.trailEvents()));
'''
                    result = subprocess.run([shutil.which("node"), "-e", script], input=json.dumps(fixture),
                                            cwd=ROOT, env={**os.environ, "TZ": "Asia/Singapore"},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    trail = json.loads(result.stdout)
                    expected = (["legacy naive action", "later action", "invalid source", "numeric action", "historical"]
                                if replay == "1" else
                                ["later action", "invalid source", "legacy naive action", "numeric action", "historical"])
                    # Preserve the existing optional parser's UTC interpretation
                    # of hand-written timezone-less rows when replay is loaded;
                    # the old local-time fallback remains when it is absent.
                    self.assertEqual([row["detail"] for row in trail], expected)
                    self.assertEqual(trail[-1]["timestamp"], source)
                    self.assertEqual(trail[-1]["recorded_at"], rows[0]["ts"])
                    self.assertEqual(trail[-1]["id"], "quest-test:2026-11-07T12:00:00Z:info_received")
            self.assertEqual((folder / "timeline.ndjson").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
