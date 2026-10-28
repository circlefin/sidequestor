from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor import onboarding
from sidequestor.native import RUNTIME_ROOT, _environment
from sidequestor.quest_terminal import QuestTerminalError
from sidequestor.resources import sync_resources
from sidequestor.setup import offer_onboarding
from sidequestor.workspace import init_workspace


NEW_QUEST = RUNTIME_ROOT / "yaas-triage" / "skills" / "yaas-quest-creation" / "new-quest.py"


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-onboarding-")
        self.addCleanup(self.temp.cleanup)
        # Starting the agent enters the workspace, which cleanup then deletes.
        self.addCleanup(os.chdir, os.getcwd())
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"YAAS_CONFIG_HOME": str(root / "config")})
        env.start()
        self.addCleanup(env.stop)
        self.workspace = init_workspace(root / "ws")
        sync_resources(self.workspace)
        self.workspace.env_file.write_text("SIDEQUESTOR_SLACK_CHECKERS_ENABLED=1\nSIDEQUESTOR_CHECKER_CONNECTORS=slack\n")
        self.exec_calls: list[tuple] = []

    def _execve(self, binary, argv, environment) -> None:
        self.exec_calls.append((binary, argv, environment))

    def _meta(self, quest_dir: Path) -> dict:
        return json.loads((quest_dir / "meta.json").read_text())

    def _watches(self, quest_dir: Path) -> list[dict]:
        return json.loads((quest_dir / "watch.json").read_text())["watches"]

    def test_slack_start_creates_a_bootstrap_quest_with_no_send_authority(self) -> None:
        with redirect_stdout(StringIO()) as out:
            code = onboarding.run_onboard(self.workspace, "slack", now=1_800_000_000)

        self.assertEqual(code, 0)
        quest_dir = onboarding.find_quest(self.workspace)
        self.assertIsNotNone(quest_dir)
        meta = self._meta(quest_dir)
        self.assertIs(meta["allow_send"], False)
        self.assertEqual(meta["allow_send_channels"], [])
        self.assertIs(meta["sidequestor_bootstrap"], True)
        self.assertEqual(self._watches(quest_dir), [])
        marker = onboarding.read_marker(quest_dir)
        self.assertEqual((marker["channel"], marker["stage"]), ("slack", "0"))
        self.assertIn("yaas-onboarding/SKILL.md", (quest_dir / "context.md").read_text())
        self.assertIn("Slack DM", out.getvalue())
        self.assertEqual(self.exec_calls, [])

    def test_terminal_start_schedules_a_heartbeat_and_starts_the_agent(self) -> None:
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "terminal", "claude", execve=self._execve)

        quest_dir = onboarding.find_quest(self.workspace)
        self.assertNotIn("sidequestor_bootstrap", self._meta(quest_dir))
        [watch] = self._watches(quest_dir)
        self.assertEqual((watch["type"], watch["cron"]), ("schedule", "0 9 * * *"))
        [(binary, argv, environment)] = self.exec_calls
        self.assertEqual(binary, "/bin/agent")
        self.assertIn("Start my Sidequestor onboarding", argv[1])
        self.assertEqual(environment["SQ_QUEST_ID"], quest_dir.name)

    def test_second_start_resumes_and_switching_to_slack_wakes_the_quest(self) -> None:
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "terminal", "codex", execve=self._execve)
            onboarding.run_onboard(self.workspace, None, "codex", execve=self._execve)
        quest_dir = onboarding.find_quest(self.workspace)
        active = list((self.workspace.state / "quests" / "active").iterdir())
        self.assertEqual(active, [quest_dir])
        self.assertIn("Resume my Sidequestor onboarding", self.exec_calls[-1][1][1])

        with redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "slack", now=1_800_000_000)
        self.assertEqual(onboarding.read_marker(quest_dir)["channel"], "slack")
        one_shots = [w for w in self._watches(quest_dir) if "next_fire_ts" in w]
        self.assertEqual([w["next_fire_ts"] for w in one_shots], ["1800000060"])

    def test_concurrent_starts_create_only_one_quest(self) -> None:
        import threading
        barrier = threading.Barrier(2)
        errors = []
        def start():
            try:
                barrier.wait(timeout=5)
                onboarding.run_onboard(self.workspace, "slack")
            except Exception as exc:
                errors.append(exc)
        with redirect_stdout(StringIO()):
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)
        self.assertEqual(len(list((self.workspace.state / "quests" / "active").iterdir())), 1)

    def test_missing_onboarding_skill_is_materialized_before_start(self) -> None:
        (self.workspace.root / onboarding.SKILL).unlink()
        with redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "slack")
        self.assertTrue((self.workspace.root / onboarding.SKILL).is_file())

    def test_slack_to_terminal_installs_one_heartbeat_and_keeps_stage(self) -> None:
        with redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "slack")
        quest_dir = onboarding.find_quest(self.workspace)
        marker = onboarding.read_marker(quest_dir)
        marker["stage"] = "B"
        onboarding._write_marker(quest_dir, marker)
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "terminal", "claude", execve=self._execve)
            onboarding.run_onboard(self.workspace, "terminal", "claude", execve=self._execve)
        self.assertEqual(onboarding.read_marker(quest_dir)["stage"], "B")
        self.assertEqual(onboarding.read_marker(quest_dir)["channel"], "terminal")
        self.assertEqual(len([watch for watch in self._watches(quest_dir) if watch.get("cron")]), 1)

    def test_failed_slack_handoff_does_not_change_channel(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "terminal")
        failing = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom")
        with self.assertRaisesRegex(QuestTerminalError, "boom"):
            onboarding.run_onboard(self.workspace, "slack", run=failing)
        self.assertEqual(onboarding.read_marker(quest_dir)["channel"], "terminal")

    def test_helper_timeouts_are_user_facing_errors(self) -> None:
        for failure in (subprocess.TimeoutExpired("helper", 30), OSError("missing")):
            with patch("sidequestor.onboarding.subprocess.run", side_effect=failure) as run:
                with self.assertRaisesRegex(QuestTerminalError, "helper could not finish"):
                    onboarding.create_quest(self.workspace, "slack", run=run)

    def test_setup_propagates_onboarding_failure(self) -> None:
        from sidequestor.setup import run_setup
        with patch("sidequestor.setup.provision_env"), \
             patch("sidequestor.setup.check_worker_clis", return_value=True), \
             patch("sidequestor.setup.setup_worker", return_value=True), \
             patch("sidequestor.setup.install_production", return_value={"jobs": {}}), \
             patch("sidequestor.setup.wait_for_dashboard_url", return_value=None), \
             patch("sidequestor.setup.print_worker_instructions"), \
             patch("sidequestor.setup.offer_onboarding", return_value=1), \
             redirect_stdout(StringIO()):
            self.assertEqual(run_setup(self.workspace, Path(sys.executable), input_fn=lambda _: "n"), 1)

    def test_disabled_slack_is_rejected_and_bare_command_uses_terminal(self) -> None:
        self.workspace.env_file.write_text("SIDEQUESTOR_SLACK_CHECKERS_ENABLED=0\n")
        with self.assertRaisesRegex(QuestTerminalError, "Slack checkers are disabled"):
            onboarding.run_onboard(self.workspace, "slack")
        self.assertIsNone(onboarding.find_quest(self.workspace))
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, None, execve=self._execve)
        self.assertEqual(onboarding.read_marker(onboarding.find_quest(self.workspace))["channel"], "terminal")

    def test_stale_progress_write_cannot_undo_channel_switch(self) -> None:
        with redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "slack")
        quest_dir = onboarding.find_quest(self.workspace)
        stale = onboarding.read_marker(quest_dir)
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "terminal", execve=self._execve)
        stale["stage"] = "C"
        onboarding._write_marker(quest_dir, stale)
        self.assertEqual(onboarding.read_marker(quest_dir)["channel"], "terminal")
        self.assertEqual(onboarding.read_marker(quest_dir)["stage"], "C")

    def test_repeated_slack_resume_deduplicates_future_handoff(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "terminal")
            onboarding.run_onboard(self.workspace, "slack", now=1_800_000_000)
            onboarding.run_onboard(self.workspace, "slack", now=1_800_000_001)
        self.assertEqual(len([w for w in self._watches(quest_dir) if "next_fire_ts" in w]), 1)

    def test_marker_write_failure_does_not_allow_duplicate_creation(self) -> None:
        with patch.object(onboarding, "_write_marker", side_effect=OSError("disk full")):
            with self.assertRaises(QuestTerminalError):
                onboarding.run_onboard(self.workspace, "slack")
        self.assertIsNotNone(onboarding.find_quest(self.workspace))
        with self.assertRaisesRegex(QuestTerminalError, "restore its progress file"):
            onboarding.run_onboard(self.workspace, "slack")
        self.assertEqual(len(list((self.workspace.state / "quests" / "active").iterdir())), 1)

    def test_finished_onboarding_is_not_recreated_or_offered_again(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "terminal")
        completed = self.workspace.state / "quests" / "completed"
        completed.mkdir(exist_ok=True)
        quest_dir.rename(completed / quest_dir.name)
        with redirect_stdout(StringIO()):
            self.assertEqual(onboarding.run_onboard(self.workspace, None), 0)
            offer_onboarding(self.workspace, slack_ready=False,
                             input_fn=lambda _: self.fail("completed onboarding was re-offered"))
        self.assertIsNone(onboarding.find_quest(self.workspace))

    def test_invalid_timezone_falls_back_to_valid_zone(self) -> None:
        from zoneinfo import ZoneInfo
        with patch.dict(os.environ, {"TZ": ":/etc/localtime"}):
            ZoneInfo(onboarding.local_timezone())

    def test_switched_terminal_heartbeat_can_complete_pending_bootstrap(self) -> None:
        from unittest import mock
        spec = importlib.util.spec_from_file_location(
            "onboarding_bootstrap_fixture", Path(__file__).with_name("test_initial_run_gate.py"))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        original_path = sys.path[:]
        try:
            tick = fixture._load_tick()
            with redirect_stdout(StringIO()):
                onboarding.run_onboard(self.workspace, "slack")
            quest_dir = onboarding.find_quest(self.workspace)
            t = fixture._FakeTick(Path(self.temp.name) / "tick-fixture")
            t.repo_root = self.workspace.root
            t.quests_dir = self.workspace.state / "quests" / "active"
            self.assertEqual(tick.prepare_bootstrap_quests(t, [quest_dir]), {quest_dir.name})
            with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
                 redirect_stdout(StringIO()):
                onboarding.run_onboard(self.workspace, "terminal", execve=self._execve)
            with mock.patch.object(tick, "_record_progress"):
                tick.commit_bootstrap(t, quest_dir.name, [tick.BOOTSTRAP_ITEM_ID])
            self.assertNotIn("sidequestor_bootstrap", self._meta(quest_dir))
            self.assertEqual(tick.prepare_bootstrap_quests(t, [quest_dir]), set())
        finally:
            sys.path[:] = original_path

    def test_terminal_agent_os_failure_is_user_facing(self) -> None:
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            with self.assertRaisesRegex(QuestTerminalError, "could not start the onboarding agent"):
                onboarding.run_onboard(self.workspace, "terminal", execve=lambda *args: (_ for _ in ()).throw(OSError("missing")))

    def test_setup_refuses_unavailable_slack_selection(self) -> None:
        answers = iter(["slack", "later"])
        with redirect_stdout(StringIO()) as output:
            offer_onboarding(self.workspace, slack_ready=False, input_fn=lambda _: next(answers),
                             start=lambda *args: self.fail("disabled Slack was started"))
        self.assertIn("Slack checkers are disabled", output.getvalue())

    def _grant_interface(self, quest_dir, channel_id="C0INTERFACE", *, watch=True):
        meta = self._meta(quest_dir)
        meta["allow_send_channels"] = [channel_id]
        (quest_dir / "meta.json").write_text(json.dumps(meta))
        if watch:
            helper = RUNTIME_ROOT / "yaas-triage" / "ledger" / "add-watch.py"
            result = subprocess.run([sys.executable, str(helper), quest_dir.name,
                                     json.dumps({"type":"slack_channel", "channel_id":channel_id,
                                                 "reason":"approved owner interface"})],
                                    env=_environment(self.workspace), cwd=self.workspace.root,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_approved_watched_interface_persists_across_channel_switches(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "terminal")
            self._grant_interface(quest_dir)
            onboarding.run_onboard(self.workspace, None, slack_channel_id="C0INTERFACE")
        self.assertEqual(onboarding.read_marker(quest_dir)["slack_channel_id"], "C0INTERFACE")
        with patch("sidequestor.quest_terminal.find_agent", return_value="/bin/agent"), \
             redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, "terminal", execve=self._execve)
            onboarding.run_onboard(self.workspace, "slack")
        marker = onboarding.read_marker(quest_dir)
        self.assertEqual((marker["channel"], marker["slack_channel_id"]), ("slack", "C0INTERFACE"))
        self.assertIs(self._meta(quest_dir)["allow_send"], False)

    def test_interface_selection_requires_existing_grant_and_watch(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "terminal")
        before = (quest_dir / onboarding.CHANNEL_MARKER).read_text()
        with self.assertRaisesRegex(QuestTerminalError, "send grant"):
            onboarding.run_onboard(self.workspace, None, slack_channel_id="C0INTERFACE")
        self._grant_interface(quest_dir, watch=False)
        with self.assertRaisesRegex(QuestTerminalError, "slack_channel watch"):
            onboarding.run_onboard(self.workspace, None, slack_channel_id="C0INTERFACE")
        self.assertEqual((quest_dir / onboarding.CHANNEL_MARKER).read_text(), before)

    def test_interface_id_validation_and_cli_mapping(self) -> None:
        from sidequestor.cli import _cmd_onboard
        for invalid in ("#my-interface", "D0DM", "https://example.test", 12):
            with self.assertRaises(QuestTerminalError):
                onboarding.run_onboard(self.workspace, None, slack_channel_id=invalid)
        with patch("sidequestor.cli.onboarding.run_onboard", return_value=0) as run:
            self.assertEqual(_cmd_onboard(self.workspace, ["--slack-channel", "C0INTERFACE"]), 0)
        run.assert_called_once_with(self.workspace, None, None, slack_channel_id="C0INTERFACE")

    def test_progress_cannot_inject_a_different_interface_destination(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "slack")
        stale = onboarding.read_marker(quest_dir)
        stale["slack_channel_id"] = "C0UNAPPROVED"
        onboarding._write_marker(quest_dir, stale)
        self.assertNotIn("slack_channel_id", onboarding.read_marker(quest_dir))
        self._grant_interface(quest_dir)
        with redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, None, slack_channel_id="C0INTERFACE")
        onboarding._write_marker(quest_dir, stale)
        self.assertEqual(onboarding.read_marker(quest_dir)["slack_channel_id"], "C0INTERFACE")

    def test_slack_dm_recovers_after_interface_grant_is_removed(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "slack")
            self._grant_interface(quest_dir)
            onboarding.run_onboard(self.workspace, None, slack_channel_id="C0INTERFACE")
        meta = self._meta(quest_dir)
        meta["allow_send_channels"] = []
        (quest_dir / "meta.json").write_text(json.dumps(meta))
        with self.assertRaisesRegex(QuestTerminalError, "send grant"):
            onboarding.run_onboard(self.workspace, "slack")
        with redirect_stdout(StringIO()):
            onboarding.run_onboard(self.workspace, None, slack_dm=True)
        marker = onboarding.read_marker(quest_dir)
        self.assertEqual(marker["channel"], "slack")
        self.assertNotIn("slack_channel_id", marker)
        self.assertEqual(self._meta(quest_dir)["allow_send_channels"], [])
        from sidequestor.cli import _cmd_onboard
        with patch("sidequestor.cli.onboarding.run_onboard", return_value=0) as run:
            self.assertEqual(_cmd_onboard(self.workspace, ["--slack-dm"]), 0)
        run.assert_called_once_with(self.workspace, None, None, slack_channel_id=None, slack_dm=True)

    def test_read_marker_refuses_malformed_interface_preference(self) -> None:
        with redirect_stdout(StringIO()):
            quest_dir = onboarding.create_quest(self.workspace, "slack")
        (quest_dir / onboarding.CHANNEL_MARKER).write_text(json.dumps({"channel":"slack", "slack_channel_id":"#unverified"}))
        with self.assertRaisesRegex(QuestTerminalError, "cannot read onboarding state"):
            onboarding.read_marker(quest_dir)

    def test_creation_failure_is_reported(self) -> None:
        failing = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom")
        with self.assertRaisesRegex(QuestTerminalError, "boom"):
            onboarding.create_quest(self.workspace, "slack", run=failing)

    def test_setup_offer_defaults_to_terminal_without_slack_and_honours_later(self) -> None:
        started = []
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            return ""

        with redirect_stdout(StringIO()):
            offer_onboarding(self.workspace, slack_ready=False, input_fn=answer,
                             start=lambda ws, channel: started.append(channel) or 0)
            offer_onboarding(self.workspace, slack_ready=True, input_fn=lambda _: "later",
                             start=lambda ws, channel: started.append(channel) or 0)
        self.assertIn("[terminal]", prompts[0])
        self.assertEqual(started, ["terminal"])

    def test_setup_offer_points_to_resume_when_onboarding_exists(self) -> None:
        with redirect_stdout(StringIO()):
            onboarding.create_quest(self.workspace, "slack")
        with redirect_stdout(StringIO()) as out:
            offer_onboarding(self.workspace, slack_ready=True,
                             input_fn=lambda _: self.fail("should not prompt"))
        self.assertIn("sq onboard", out.getvalue())

    def test_new_quest_rejects_malformed_send_channels(self) -> None:
        spec = {"title": "Bad", "allow_send_channels": ["U123"],
                "watches": [{"type": "schedule", "cron": "0 9 * * *", "tz": "UTC",
                             "reason": "test"}]}
        result = subprocess.run([sys.executable, str(NEW_QUEST), json.dumps(spec)],
                                env=_environment(self.workspace), capture_output=True,
                                text=True, cwd=self.workspace.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allow_send_channels", result.stderr)


if __name__ == "__main__":
    unittest.main()
