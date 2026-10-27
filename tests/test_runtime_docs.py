from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sidequestor.workspace import init_workspace


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DOCTOR = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "ops" / "doctor.sh"
TRIAGE_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
SKILLS_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "skills"
ENV_EXAMPLE = PACKAGE_ROOT / "src" / "sidequestor" / "package_data" / "env.example"
OPERATING = PACKAGE_ROOT / "src" / "sidequestor" / "package_data" / "OPERATING.md"
SETTINGS_EXAMPLE = PACKAGE_ROOT / "src" / "sidequestor" / "package_data" / "settings.json.example"
FORBIDDEN_SKILL_PATTERNS = (
    "python3 yaas-triage/",
    "bash yaas-triage/",
    "./yaas-triage/",
    "MCP_CALL=yaas-triage/",
    "\nyaas-triage/tests/",
    "`yaas-triage/surfaces/jira-call.sh",
    "via `yaas-triage/skills/yaas-gmail-reply/gmail-reply.py`",
    "`yaas-triage/ledger/approval-helper.py write",
    "yaas-triage/tests/behaviour/doc-contracts.test.sh",
    "python3 checkers/",
    "com.yaas.triage.plist",
    "com.yaas.heartbeat",
)


class RuntimeDocsTest(unittest.TestCase):
    def test_doctor_uses_the_workspace_root_from_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-doctor-") as raw, \
                tempfile.TemporaryDirectory(prefix="sidequestor-config-") as config_home:
            with patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": config_home}, clear=False):
                workspace = init_workspace(raw)
                workspace.env_file.unlink()
                result = subprocess.run(
                    ["bash", str(DOCTOR), "--quiet"],
                    text=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "SIDEQUESTOR_CONFIG_HOME": config_home,
                        "SIDEQUESTOR_WORKSPACE": str(workspace.root),
                    },
                )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(workspace.root / ".env"), result.stdout)
        self.assertNotIn("src/sidequestor/runtime/.env", result.stdout)

    def _doctor(self, connectors: str) -> str:
        with tempfile.TemporaryDirectory(prefix="sidequestor-doctor-connectors-") as raw, \
                tempfile.TemporaryDirectory(prefix="sidequestor-config-") as config_home:
            with patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": config_home}, clear=False):
                workspace = init_workspace(raw)
                result = subprocess.run(
                    ["bash", str(DOCTOR)],
                    text=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "SIDEQUESTOR_CONFIG_HOME": config_home,
                        "SIDEQUESTOR_WORKSPACE": str(workspace.root),
                        "SIDEQUESTOR_CHECKER_CONNECTORS": connectors,
                    },
                )
        return result.stdout

    def test_doctor_fails_a_connector_list_that_tick_would_exit_2_on(self) -> None:
        """doctor used to print this green. It is the tool an operator reaches for when
        triage has stopped, so a green line here sent them looking anywhere but the cause."""
        report = self._doctor("slack,twitter")
        self.assertIn("unknown connector", report)
        self.assertIn("SIDEQUESTOR_CHECKER_CONNECTORS", report)
        self.assertIn("tick.py exits 2", report)

    def test_doctor_fails_a_duplicated_connector(self) -> None:
        self.assertIn("duplicate connectors", self._doctor("slack,slack"))

    def test_doctor_accepts_the_default_and_the_opt_in_lists(self) -> None:
        for connectors in ("slack,email,github,jira", "slack,email,github,jira,telegram,x", ""):
            report = self._doctor(connectors)
            self.assertNotIn("unknown connector", report, connectors)
            self.assertNotIn("duplicate connectors", report, connectors)
            self.assertNotIn("could not validate", report, connectors)

    def _offenders(self, paths) -> list[str]:
        offenders: list[str] = []
        for path in paths:
            text = path.read_text()
            for pattern in FORBIDDEN_SKILL_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {pattern}")
        return offenders

    def test_shipped_skills_do_not_use_workspace_relative_helper_commands(self) -> None:
        offenders = self._offenders(sorted(SKILLS_ROOT.rglob("SKILL.md")))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_process_reaction_has_slack_connect_draft_fallback(self) -> None:
        skill = (SKILLS_ROOT / "yaas-reactions" / "SKILL.md").read_text()
        process_row = next(
            line for line in skill.splitlines() if line.startswith("| `process` |")
        )
        self.assertIn("mcp_externally_shared_channel_restricted", process_row)
        self.assertIn('"draft": true', process_row)
        self.assertIn("ack it `blocked`", process_row)

    def test_shipped_config_and_operating_docs_use_resolvable_commands(self) -> None:
        # `<workspace>/yaas-triage/` does not exist in a pip install, so any command
        # spelled relative to it fails the moment a user copy-pastes it. The skills were
        # swept for this; env.example and OPERATING.md ship to the same workspaces and
        # were missed the first time, which is why they are pinned here too.
        offenders = self._offenders([ENV_EXAMPLE, OPERATING, SETTINGS_EXAMPLE])
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_doctor_remediation_advice_is_runnable_from_a_workspace(self) -> None:
        # doctor.sh tells the user how to fix what it found. Advice naming a path that
        # cannot exist is worse than no advice: it reads as authoritative.
        text = DOCTOR.read_text()
        for dead in ("./setup/install-launchd.sh",
                     "./setup/install-launchd-heartbeat.sh",
                     "python3 yaas-triage/"):
            self.assertNotIn(dead, text, f"doctor.sh still advises {dead!r}")

    def test_runtime_operator_advice_has_no_legacy_launchd_labels(self) -> None:
        for path in (DOCTOR, TRIAGE_ROOT / "ops" / "health-monitor.py"):
            text = path.read_text()
            self.assertNotIn("com.yaas.triage", text, str(path))
            self.assertNotIn("com.yaas.heartbeat", text, str(path))

    def test_quest_docs_keep_summary_in_context_and_history_in_timeline(self) -> None:
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        creation = (SKILLS_ROOT / "yaas-quest-creation" / "SKILL.md").read_text()
        scaffold = (SKILLS_ROOT / "yaas-quest-creation" / "new-quest.py").read_text()
        for text in (dispatch, creation, scaffold, OPERATING.read_text()):
            self.assertIn("latest summary of things", text)
        self.assertIn("timeline.ndjson` is the chronological record", dispatch)
        self.assertIn("Do not append dated updates", dispatch)

    def test_managed_decision_policy_skills_are_wired_into_runtime(self) -> None:
        draft = (SKILLS_ROOT / "yaas-draft-readiness" / "SKILL.md").read_text()
        closure = (SKILLS_ROOT / "yaas-action-closure" / "SKILL.md").read_text()
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        reactions = (SKILLS_ROOT / "yaas-reactions" / "SKILL.md").read_text()
        gmail = (SKILLS_ROOT / "yaas-gmail-reply" / "SKILL.md").read_text()
        creation = (SKILLS_ROOT / "yaas-quest-creation" / "SKILL.md").read_text()
        operating = OPERATING.read_text()

        self.assertIn("Map the decision state", draft)
        self.assertIn("Closure routes", closure)
        for text in (operating, dispatch):
            self.assertIn("yaas-draft-readiness", text)
            self.assertIn("yaas-action-closure", text)
        self.assertIn("yaas-draft-readiness", reactions)
        self.assertIn("yaas-draft-readiness", gmail)
        self.assertIn("yaas-action-closure", creation)
        self.assertIn("Use a `schedule` watch only for a due time", closure)
        self.assertIn("use the matching surface watch", closure)

    def test_approval_timeline_preserves_pre_review_and_final_bodies(self) -> None:
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        self.assertIn('"event":"draft_posted"', dispatch)
        self.assertIn('"message_text":"<exact pre-review body>"', dispatch)
        self.assertIn("exact final `message_text`", dispatch)
        self.assertIn("mutable approval record is not evidence", dispatch)

    def test_quest_dispatch_waits_for_a_conversational_turn_across_surfaces(self) -> None:
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        answering = (SKILLS_ROOT / "yaas-answering-quality" / "SKILL.md").read_text()
        self.assertIn("Wait for your turn", dispatch)
        self.assertIn("read the complete conversation", dispatch)
        self.assertIn("read the complete thread without an `oldest` boundary", dispatch)
        self.assertIn("watermark identifies what is new", dispatch)
        self.assertIn("watermark-truncated excerpt alone", dispatch)
        self.assertIn("who is talking to whom", dispatch)
        self.assertIn("Routing-only messages", dispatch)
        self.assertIn("for visibility", dispatch)
        self.assertIn("ack the watch `nothing_to_do`", dispatch)
        self.assertIn("Silence is a successful outcome", dispatch)
        self.assertIn("does not create a conversational turn", dispatch)
        self.assertNotIn("Reply first on every conversational surface", dispatch)
        self.assertIn("Turn-taking comes before answering", answering)
        self.assertIn("If humans are talking to each other, wait for their conclusion", answering)
        self.assertIn("Do not repeat someone else's mention", answering)
        self.assertIn("silence is correct", answering)
        for surface in ("Slack", "Jira", "Telegram", "X conversations", "email", "GitHub"):
            self.assertIn(surface, dispatch)
        self.assertIn("legacy `watch_mode` field as inert metadata", dispatch)

    def test_quest_dispatch_isolates_blocked_slack_mentions(self) -> None:
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        operating = OPERATING.read_text()
        self.assertIn("Slack mention fan-out exception", dispatch)
        self.assertIn("one microsecond", dispatch)
        self.assertIn("prints `skip:duplicate`", dispatch)
        self.assertIn("dispatched mention item says `complete: true`", dispatch)
        self.assertIn('with `"ephemeral": true`', dispatch)
        self.assertIn('`"include_parent": true`', dispatch)
        self.assertIn('`"one_shot_until_ts":"<blocked_message_ts>"`', dispatch)
        self.assertIn("confirm that the returned conversation contains", dispatch)
        self.assertIn("housekeeping retires the bounded-purpose watch", dispatch)
        self.assertIn("ack the original\n   `slack_mention` item as `handled`", dispatch)
        self.assertIn("exact thread retries independently", dispatch)
        self.assertIn("safely transferred to a one-shot exact", operating)

    def test_quest_dispatch_ignores_its_own_github_pr_writes(self) -> None:
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        self.assertIn(
            "If the only update after the recorded action is the quest's own GitHub comment or\n"
            "review, ack `nothing_to_do` and do not write again. Act only when a later human "
            "comment, review,\nstate change, or head commit adds new information. This guard is "
            "mandatory for `involves:<self>`",
            dispatch,
        )

    def test_runtime_docs_reference_telegram_send_helper_and_limits(self) -> None:
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        ops = (SKILLS_ROOT / "yaas-ops" / "SKILL.md").read_text()
        operating = OPERATING.read_text()
        self.assertIn("telegram-send.py", dispatch)
        self.assertIn('"send":true', dispatch)
        self.assertIn("idempotency_key", dispatch)
        self.assertIn("sq telegram-send", ops)
        self.assertIn("Pass `--send` to deliver", ops)
        self.assertIn("telegram-send.py", operating)

    def test_gmail_reply_docs_require_an_explicit_native_reply_mode(self) -> None:
        helper = (SKILLS_ROOT / "yaas-gmail-reply" / "gmail-reply.py").read_text()
        gmail = (SKILLS_ROOT / "yaas-gmail-reply" / "SKILL.md").read_text()
        dispatch = (SKILLS_ROOT / "yaas-quest-dispatch" / "SKILL.md").read_text()
        ops = (SKILLS_ROOT / "yaas-ops" / "SKILL.md").read_text()
        for text in (gmail, dispatch, ops):
            self.assertIn("--reply-sender", text)
            self.assertIn("--reply-all", text)
        self.assertIn("Exactly one reply mode is required", gmail)
        self.assertIn("`gws gmail +reply`", gmail)
        self.assertIn("`gws gmail +reply-all`", gmail)
        self.assertIn("quotes the original message", gmail)
        self.assertIn("--cc` and `--remove` are valid only with `--reply-all`", gmail)
        self.assertIn("allow_send: true", gmail)
        self.assertIn("Draft first", gmail)
        self.assertIn("allow_send: true", dispatch)
        self.assertIn("--draft", dispatch)
        self.assertIn("googleworkspace/cli#911", gmail)
        self.assertIn("googleworkspace/cli#911", helper)
        self.assertNotIn("MIMEText", helper)

    def test_doctor_does_not_source_workspace_dotenv(self) -> None:
        self.assertNotIn('source "$ENV_FILE"', DOCTOR.read_text())
        self.assertNotIn('. "$ENV_FILE"', DOCTOR.read_text())


if __name__ == "__main__":
    unittest.main()
