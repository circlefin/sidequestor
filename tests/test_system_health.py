from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TRIAGE = Path(__file__).resolve().parents[1] / "src/sidequestor/runtime/yaas-triage"
sys.path.insert(0, str(TRIAGE))
from inbox import ensure_inbox, log_inbox, write_json
from system_health import Health, collect, diagnosis, harness_problem, sync_updates
from system_health import dependency_paused, dispatch_result, RECURRENCE_COOLDOWN_SEC


class SystemHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-health-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())  # dispatch_loop deliberately enters its workspace.
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, SIDEQUESTOR_WORKSPACE=str(self.root), YAAS_AGENT="codex")
        ensure_inbox(self.root)
        self.h = Health(self.root, self.env, now=1000)
        self.qdir = self.root / "state/quests/active/quest-test"
        self.qdir.mkdir()
        write_json(self.qdir / "meta.json", {"id": "quest-test", "status": "active"})
        write_json(self.qdir / "watch.json", {"watches": [{"watch_id": "watch-abc", "type": "github_pr"}]})

    def queue(self):
        return json.loads((self.root / "state/pending-approvals.json").read_text())["items"]

    def helper(self, command, *args):
        return subprocess.run([sys.executable, str(TRIAGE / "ledger/approval-helper.py"), command, *args],
                              env=self.env, capture_output=True, text=True, check=True)

    def set_status(self, approval_id, status):
        path = self.root / "state/pending-approvals.json"
        data = json.loads(path.read_text())
        next(i for i in data["items"] if i["id"] == approval_id)["status"] = status
        write_json(path, data)

    def test_missing_harness_creates_direct_review_and_deduplicates(self):
        with patch("system_health.shutil.which", return_value=None):
            problem = harness_problem(self.env)
        self.assertIn("command -v codex", problem)
        self.h.observe("harness", kind="harness", reason=problem)
        self.h.reconcile(can_launch=False)
        self.h.reconcile(can_launch=False)
        items = self.queue()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["quest_id"], "quest-inbox")
        self.assertEqual(items[0]["target"]["repair_mode"], "interactive")
        self.assertIn("service PATH", items[0]["message_text"])
        self.assertIsNone(diagnosis(self.root, {"issue_id": next(iter(self.h.issues))}, now=1000))

    def test_checker_issue_arms_diagnosis_on_its_quest(self):
        self.h.observe("quest-test|watch-abc", quest="quest-test", reason="Malformed checker result", watch_type="github_pr")
        self.h.reconcile()
        watches = json.loads((self.qdir / "watch.json").read_text())["watches"]
        health = next(w for w in watches if w["type"] == "health_issue")
        text = diagnosis(self.root, health, now=1000)
        self.assertIn("HEALTH DIAGNOSIS ONLY", text)
        self.assertIn("quest-test", text)
        issue = self.h.issues[health["issue_id"]]
        payload = {"quest_id": "quest-test", "action_type": "remote_request",
                   "target": {"system_key": issue["system_key"], "action": "health_repair", "repair_mode": "headless"},
                   "message_text": "Proposed checker repair"}
        self.helper("write", json.dumps(payload))
        self.assertIsNone(diagnosis(self.root, health, now=1000))
        item = self.queue()[0]
        self.set_status(item["id"], "cancelled")
        payload["message_text"] = "Different wording must not undo dismissal"
        self.helper("write", json.dumps(payload))
        self.assertEqual(len(self.queue()), 1)
        self.assertIsNone(diagnosis(self.root, health, now=1000))
        self.h = Health(self.root, self.env, now=1000)
        self.h.reconcile()
        write_json(self.root / "state/pending-approvals.json", {"items": []})
        self.h = Health(self.root, self.env, now=1000)
        self.h.reconcile()
        self.assertIsNone(diagnosis(self.root, health, now=1000))

    def test_shared_auth_failure_is_one_inbox_issue_and_redacts_secrets(self):
        for qid in ("quest-test", "quest-other"):
            self.h.observe(qid + "|watch-abc", quest=qid, watch_type="github_pr",
                           reason="401 token=very-secret-value expired")
        self.h.reconcile(can_launch=False)
        self.assertEqual(len(self.queue()), 1)
        self.assertNotIn("very-secret-value", json.dumps(self.queue()))
        self.assertIn("quest-other", self.queue()[0]["message_text"])

    def test_changing_failure_text_is_not_treated_as_recovery(self):
        source = "quest-test|watch-abc"
        self.h.observe(source, quest="quest-test", watch_type="github_pr", reason="401 Bad credentials")
        self.h.reconcile(can_launch=False)
        key = self.queue()[0]["target"]["system_key"]
        for reason in ("request timed out", "401 Login required", "another timeout"):
            self.h.observe(source, quest="quest-test", watch_type="github_pr", reason=reason)
            self.h.reconcile(can_launch=False)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.queue()[0]["status"], "pending_review")
        self.assertEqual(self.queue()[0]["target"]["system_key"], key)
        self.assertFalse(next(iter(self.h.issues.values())).get("pending_recurrence"))

    def test_recovery_closes_notice_and_recurrence_has_new_episode(self):
        self.h.observe("harness", kind="harness", reason="Codex unavailable")
        self.h.reconcile(can_launch=False)
        first = self.queue()[0]
        self.h.observe("harness", healthy=True)
        self.h.reconcile()
        self.assertEqual(self.queue()[0]["status"], "cancelled")
        self.h.observe("harness", kind="harness", reason="Codex unavailable")
        self.h.reconcile(can_launch=False)
        self.assertEqual(len(self.queue()), 1)  # Flapping inside the cooldown stays quiet.
        self.h.now += RECURRENCE_COOLDOWN_SEC + 1
        self.h.reconcile(can_launch=False)
        self.assertEqual(len(self.queue()), 2)
        self.assertNotEqual(first["target"]["system_key"], self.queue()[1]["target"]["system_key"])

    def test_deleted_and_archived_inbox_recovers_outstanding_watches(self):
        self.helper("write", json.dumps({"quest_id": "quest-inbox", "message_text": "review this", "target": {}}))
        inbox = self.root / "state/quests/active/quest-inbox"
        (inbox / "watch.json").unlink()
        self.helper("ensure-inbox")
        watches = json.loads((inbox / "watch.json").read_text())["watches"]
        self.assertEqual(watches[0]["approval_id"], self.queue()[0]["id"])
        (inbox / "timeline.ndjson").write_text('{"event":"note","note":"retain me"}\n')
        archived = self.root / "state/quests/archived"
        archived.mkdir()
        inbox.rename(archived / "quest-inbox")
        self.helper("ensure-inbox")
        self.assertIn("retain me", (inbox / "timeline.ndjson").read_text())
        meta = json.loads((inbox / "meta.json").read_text())
        meta["status"] = "retired"
        write_json(inbox / "meta.json", meta)
        self.helper("ensure-inbox")
        self.assertEqual(json.loads((inbox / "meta.json").read_text())["status"], "active")
        (inbox / "watch.json").write_text("corrupt watch")
        (inbox / "meta.json").write_text("corrupt metadata")
        self.helper("ensure-inbox")
        self.assertTrue(list(inbox.glob("watch.json.invalid.*")))
        self.assertTrue(list(inbox.glob("meta.json.invalid.*")))
        self.assertTrue(json.loads((inbox / "watch.json").read_text())["watches"])

    def test_reaction_ack_history_is_idempotent(self):
        ack = TRIAGE / "ledger/ack-watch.py"
        def run(*args):
            return subprocess.run([sys.executable, str(ack), *args], env=self.env, text=True, capture_output=True, check=True)
        for run_id in ("one", "two"):
            run("open", run_id, "reactions", "reactions", '[{"item_id":"robot_face:123.456","type":"robot_face"}]')
            run("ack", run_id, "robot_face:123.456", "handled", "Replied in source thread")
        events = [json.loads(line) for line in (self.root / "state/quests/active/quest-inbox/timeline.ndjson").read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reaction_item"], "robot_face:123.456")

    def test_new_inbox_record_survives_an_existing_truncated_line(self):
        path = self.root / "state/quests/active/quest-inbox/timeline.ndjson"
        path.write_text('{"event":"truncated"')
        log_inbox(self.root, "receipt", {"event": "note", "note": "visible"})
        lines = path.read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])["note"], "visible")

    def test_unreadable_quest_manifest_routes_diagnosis_through_inbox(self):
        (self.qdir / "watch.json").write_text("invalid json")
        t = self.tick([{"qid": "quest-test", "status": "misconfig", "reason": "watch.json invalid"}])
        collect(t)
        row = next(r for r in t.results if r.get("type") == "health_issue")
        self.assertEqual(row["qid"], "quest-inbox")
        self.assertIn("quest-test", row["reason"])

    def tick(self, rows):
        return SimpleNamespace(repo_root=self.root, env=dict(self.env, YAAS_RUN_AGENT="fixture"), now_ts=1000,
            quests_dir=self.root / "state/quests/active", checker_health=self.root / "state/triage/checker-health.json",
            unacked_file=self.root / "state/triage/unacked-counts.json", unacked_promote=3, results=rows,
            cfg=SimpleNamespace(checker_enabled=lambda _: True))

    def test_generic_failures_wait_for_threshold_and_backoff_does_not_clear(self):
        row = {"qid": "quest-test", "watch_id": "watch-abc", "type": "github_pr", "status": "backoff", "reason": "network failed"}
        t = self.tick([row])
        write_json(t.checker_health, {"watch-abc": {"consecutive_errors": 1, "last_error": "network failed"}})
        collect(t)
        self.assertFalse(Health(self.root, self.env).issues)
        write_json(t.checker_health, {"watch-abc": {"consecutive_errors": 3, "last_error": "network failed"}})
        t.results = [row]
        collect(t)
        self.assertTrue(any(r["type"] == "health_issue" and r["status"] == "dirty" for r in t.results))
        t.results = [dict(row, reason="in checker backoff")]
        collect(t)
        self.assertTrue(next(iter(Health(self.root, self.env).issues.values()))["active"])
        t.results = [dict(row, status="ok")]
        collect(t)
        self.assertFalse(next(iter(Health(self.root, self.env).issues.values()))["active"])
        self.assertFalse(any(w["type"] == "health_issue" for w in json.loads((self.qdir / "watch.json").read_text())["watches"]))

    def test_business_blocked_ack_backoff_does_not_trigger_health_diagnosis(self):
        t = self.tick([{"qid": "quest-test", "watch_id": "watch-abc", "type": "github_pr",
                        "status": "backoff", "reason": "3 dispatches with no progress"}])
        write_json(t.unacked_file, {"quest-test|watch-abc": {"count": 3, "last_status": "blocked"}})
        collect(t)
        self.assertFalse(Health(self.root, self.env).issues)
        self.assertFalse(any(r.get("type") == "health_issue" for r in t.results))

    def test_executed_repair_reopens_after_fresh_failure_not_old_backoff(self):
        self.h.observe("harness", kind="harness", reason="Unavailable")
        self.h.reconcile(can_launch=False)
        item = self.queue()[0]
        path = self.root / "state/pending-approvals.json"
        data = json.loads(path.read_text())
        data["items"][0].update(status="executed", sent_at="1970-01-01T00:20:00+00:00")
        write_json(path, data)
        self.h.reconcile(can_launch=False)  # Failure at t=1000 predates repair at t=1200.
        self.assertEqual(self.queue()[0]["status"], "executed")
        self.h.now = 1300
        self.h.observe("harness", kind="harness", reason="Still unavailable")
        self.h.reconcile(can_launch=False)
        self.assertEqual(self.queue()[0]["status"], "pending_review")
        self.assertIn("subsequent check", self.queue()[0]["processing_error"])
        self.assertEqual(self.queue()[0]["id"], item["id"])

    def test_one_failed_watch_append_does_not_stop_other_health_issues(self):
        self.h.observe("quest-test|watch-abc", quest="quest-test", reason="broken checker")
        self.h.observe("another", kind="reactions", reason="broken reaction checker")
        run = subprocess.run
        def selectively_fail(argv, **kwargs):
            if argv[1].endswith("add-watch.py") and argv[2] == "quest-test":
                raise subprocess.CalledProcessError(2, argv, stderr="cannot append watch")
            return run(argv, **kwargs)
        with patch("system_health.subprocess.run", side_effect=selectively_fail):
            self.h.reconcile()
        self.assertEqual(len(Health(self.root, self.env).issues), 2)
        self.assertEqual(self.queue()[0]["quest_id"], "quest-test")
        watches = json.loads((self.root / "state/quests/active/quest-inbox/watch.json").read_text())["watches"]
        self.assertTrue(any(w["type"] == "health_issue" for w in watches))

    def test_dispatch_launch_failure_and_persistent_errors_create_direct_reviews(self):
        t = self.tick([])
        t.dispatch_exit, t.dispatch_last_error, t.dispatch_launch_failed = 127, "command not found", True
        dispatch_result(t, "quest-test")
        self.assertEqual(self.queue()[0]["quest_id"], "quest-inbox")
        self.assertEqual(self.queue()[0]["target"]["action"], "health_repair")
        other = self.root / "state/quests/active/quest-other"
        other.mkdir()
        write_json(other / "watch.json", {"watches": []})
        t.dispatch_exit, t.dispatch_last_error, t.dispatch_launch_failed = 1, "provider unavailable", False
        for _ in range(3):
            dispatch_result(t, "quest-other")
        self.assertTrue(any(i["quest_id"] == "quest-other" for i in self.queue()))

    def test_setup_block_supersedes_existing_generic_worker_review(self):
        t = self.tick([])
        t.dispatch_exit, t.dispatch_last_error = 9, ""
        t.dispatch_worker_log = str(self.root / "logs/worker-reactions.log")
        for _ in range(3):
            dispatch_result(t, "reactions")
        self.assertEqual(self.queue()[0]["status"], "pending_review")
        self.assertIn("What failed", self.queue()[0]["message_text"])
        self.assertIn(t.dispatch_worker_log, self.queue()[0]["message_text"])

        # The next readiness preflight identifies a setup block; the old review
        # is retired without claiming the worker has successfully recovered.
        dependency_paused(t, "reactions")
        self.assertEqual(self.queue()[0]["status"], "cancelled")
        health = Health(self.root, self.env)
        self.assertNotIn("dispatch:reactions", health.sources)
        self.assertFalse(next(iter(health.issues.values()))["active"])

        # A newly detected MCP/auth block must never create another generic item.
        t.dispatch_dependency_blocked = True
        dispatch_result(t, "reactions")
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.queue()[0]["status"], "cancelled")

    def test_diagnosis_attempts_are_per_issue_and_fallback_after_three_empty_runs(self):
        for source in ("first", "second"):
            self.h.observe(source, quest="quest-test", reason="broken checker")
        self.h.reconcile()
        t = self.tick([])
        t.dispatch_exit, t.dispatch_last_error = 0, ""
        watches = json.loads((self.qdir / "watch.json").read_text())["watches"]
        t.dirty_watches = [dict(w, quest_id="quest-test") for w in watches if w["type"] == "health_issue"]
        dispatch_result(t, "quest-test")
        self.assertEqual([i["attempts"] for i in Health(self.root, self.env).issues.values()], [1, 1])
        for _ in range(2):
            dispatch_result(t, "quest-test")
        self.assertEqual(len(self.queue()), 2)
        self.assertTrue(all("could not identify a safe automatic fix" in i["context"] for i in self.queue()))

    def test_update_notice_respects_dismissal_and_installed_version(self):
        write_json(self.root / "state/update-status.json", {"latest": "9.0.0"})
        t = self.tick([])
        sync_updates(t)
        sync_updates(t)
        self.assertEqual(len(self.queue()), 1)
        item = self.queue()[0]
        self.assertEqual(item["target"]["action"], "sidequestor_upgrade")
        import approval_state
        from datetime import datetime, timezone
        self.assertEqual(approval_state.available_actions(item, datetime.now(timezone.utc)), ["review", "cancel"])
        self.assertEqual(approval_state.apply_transition(item, "review", {}, datetime.now(timezone.utc))["status"], "reviewed")
        watches = json.loads((self.root / "state/quests/active/quest-inbox/watch.json").read_text())["watches"]
        self.assertFalse(any(w.get("approval_id") == item["id"] for w in watches))
        self.set_status(item["id"], "cancelled")
        sync_updates(t)
        self.assertEqual(len(self.queue()), 1)
        self.set_status(item["id"], "pending_review")
        self.helper("resolve-system-items", json.dumps({"installed": "9.0.0"}))
        self.assertEqual(self.queue()[0]["status"], "cancelled")

    def test_package_bug_report_uses_verified_version_and_stable_health_identity(self):
        from sidequestor import __version__
        self.h.observe('quest-test|watch-abc', quest='quest-test', reason='incomplete slack thread response record')
        self.h.reconcile()
        issue_id, issue = next(iter(self.h.issues.items()))
        text = diagnosis(self.root, {'issue_id': issue_id}, now=1000)
        self.assertIn(f'Verified running Sidequestor version: {__version__}', text)
        self.assertIn('minimal synthetic reproduction', text)
        payload = {'quest_id': 'quest-test', 'action_type': 'remote_request',
                   'target': {'system_key': issue['system_key'], 'surface': 'gmail', 'action': 'send',
                              'to': 'guangmian.kung@circle.com', 'subject': 'Parser bug',
                              'report_kind': 'package_bug', 'installed_version': __version__},
                   'message_text': 'Synthetic reproduction and verified facts'}
        self.helper('write', json.dumps(payload))
        self.assertIsNone(diagnosis(self.root, {'issue_id': issue_id}, now=1000))
        self.helper('write', json.dumps(payload))
        self.assertEqual(len(self.queue()), 1)
        for field, value in [('to', 'other@example.com'), ('installed_version', '0.0.1')]:
            bad = json.loads(json.dumps(payload)); bad['target'][field] = value
            with self.assertRaises(subprocess.CalledProcessError):
                self.helper('write', json.dumps(bad))
        item = self.queue()[0]
        self.set_status(item['id'], 'executed')
        self.helper('resolve-system-items', json.dumps({'failed_health': {issue['system_key']: 999999999999}}))
        self.assertEqual(self.queue()[0]['status'], 'executed')

    def test_newer_update_supersedes_the_previous_notice(self):
        t = self.tick([])
        for version in ("9.0", "10.0"):
            write_json(self.root / "state/update-status.json", {"latest": version})
            sync_updates(t)
        pending = [i for i in self.queue() if i["status"] == "pending_review"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["target"]["version"], "10.0")

    def test_approved_upgrade_survives_newer_release_until_the_job_runs(self):
        write_json(self.root / 'state/update-status.json', {'latest': '9.0'})
        t = self.tick([])
        sync_updates(t)
        item = self.queue()[0]
        self.set_status(item['id'], 'reviewed')
        write_json(self.root / 'state/update-status.json', {'latest': '10.0'})
        def check_after_reconcile(*_):
            current = next(i for i in self.queue() if i['id'] == item['id'])
            self.assertEqual(current['status'], 'reviewed')
        with patch('sidequestor.upgrade_job.schedule_reviewed', side_effect=check_after_reconcile) as schedule:
            sync_updates(t)
        schedule.assert_called_once()

    def test_tick_dispatches_diagnosis_then_stops_after_review_exists(self):
        spec = importlib.util.spec_from_file_location("health_tick_integration", TRIAGE / "tick.py")
        tick = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)
        runner = self.root / "fake-worker.py"
        runner.write_text('''import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['SIDEQUESTOR_WORKSPACE'])
runtime = pathlib.Path(os.environ['YAAS_RUNTIME_ROOT']) / 'yaas-triage'
issue = next(i for i in json.loads((root / 'state/triage/health-issues.json').read_text())['issues'].values() if i['active'])
prompt = sys.argv[sys.argv.index('--prompt') + 1]
assert 'HEALTH DIAGNOSIS ONLY' in prompt
subprocess.run([sys.executable, str(runtime / 'ledger/approval-helper.py'), 'write', json.dumps({
    'quest_id': issue['owner'], 'action_type': 'remote_request',
    'target': {'system_key': issue['system_key'], 'action': 'health_repair', 'repair_mode': 'headless'},
    'message_text': 'Propose a package repair; do not mutate packaged runtime.'})], check=True, capture_output=True)
manifest = max((root / 'state/triage').glob('dispatch-*.json'), key=lambda p: p.stat().st_mtime)
data = json.loads(manifest.read_text())
for item in data['items']:
    subprocess.run([sys.executable, str(runtime / 'ledger/ack-watch.py'), 'ack', data['run_id'], item['item_id'], 'handled', 'Repair proposal queued'], check=True, capture_output=True)
print(json.dumps({'exit': 0, 'wall_sec': 0, 'ndjson': '', 'log': ''}))
''')
        write_json(self.qdir / "watch.json", {"watches": [{"watch_id": "watch-abc", "type": "broken_checker", "reason": "fixture", "last_checked_ts": "1234"}]})
        env = dict(self.env, YAAS_RUN_AGENT=str(runner), YAAS_RUNTIME_ROOT=str(TRIAGE.parent), YAAS_SLACK_CHECKERS_ENABLED="0")
        with patch.dict(os.environ, env), patch.object(tick, "have_network", return_value=True):
            first = tick.Tick()
            first.log_dir.mkdir(exist_ok=True)
            self.assertEqual(tick.run_tick(first), 0)
            self.assertEqual(first.dispatched, 1)
            self.assertEqual(self.queue()[0]["quest_id"], "quest-test")
            second = tick.Tick()
            self.assertEqual(tick.run_tick(second), 0)
            self.assertEqual(second.dispatched, 0)
        watches = json.loads((self.qdir / "watch.json").read_text())["watches"]
        original = next(w for w in watches if w["type"] == "broken_checker")
        self.assertEqual(original["last_checked_ts"], "1234")

    def test_missing_harness_is_reported_even_when_offline(self):
        spec = importlib.util.spec_from_file_location("offline_health_tick", TRIAGE / "tick.py")
        tick = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)
        with patch.dict(os.environ, dict(self.env, YAAS_RUN_AGENT="")), \
             patch("system_health.shutil.which", return_value=None), \
             patch.object(tick, "have_network", return_value=False):
            t = tick.Tick()
            t.log_dir.mkdir(exist_ok=True)
            self.assertEqual(tick.run_tick(t), 0)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.queue()[0]["target"]["action"], "health_repair")

    def test_invalid_startup_setting_queues_review_and_valid_config_resolves_it(self):
        spec = importlib.util.spec_from_file_location("bad_config_health_tick", TRIAGE / "tick.py")
        tick = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)
        with patch.dict(os.environ, dict(self.env, SIDEQUESTOR_TRIAGE_MAX_PARALLEL="broken")):
            self.assertEqual(tick.main(), 2)
        self.assertIn("TRIAGE_MAX_PARALLEL", self.queue()[0]["message_text"])
        with patch.dict(os.environ, dict(self.env, YAAS_RUN_AGENT="fixture")), \
             patch.object(tick, "have_network", return_value=False):
            t = tick.Tick()
            self.assertEqual(tick.run_tick(t), 0)
        self.assertEqual(self.queue()[0]["status"], "cancelled")

    def test_health_proposal_cannot_be_written_by_another_dispatch(self):
        self.h.observe("quest-test|watch-abc", quest="quest-test", reason="broken checker")
        self.h.reconcile()
        issue = next(iter(self.h.issues.values()))
        payload = {"quest_id": "quest-test", "action_type": "remote_request", "target": {
            "system_key": issue["system_key"], "action": "health_repair", "repair_mode": "headless"}, "message_text": "repair"}
        with patch.dict(self.env, {"SIDEQUESTOR_DISPATCH_TARGET": "quest-other"}):
            with self.assertRaises(subprocess.CalledProcessError):
                self.helper("write", json.dumps(payload))

    def test_uncaught_tick_error_creates_review_without_worker(self):
        spec = importlib.util.spec_from_file_location("crashed_health_tick", TRIAGE / "tick.py")
        tick = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)
        with patch.dict(os.environ, dict(self.env, YAAS_RUN_AGENT="fixture")), \
             patch.object(tick, "run_tick", side_effect=FileNotFoundError("configured helper missing")), \
             patch.object(tick, "_on_exit"):
            self.assertEqual(tick.main(), 1)
        self.assertIn("configured helper missing", self.queue()[0]["message_text"])
        self.assertEqual(self.queue()[0]["quest_id"], "quest-inbox")


if __name__ == "__main__":
    unittest.main()
