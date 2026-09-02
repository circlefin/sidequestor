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

    def test_doctor_does_not_source_workspace_dotenv(self) -> None:
        self.assertNotIn('source "$ENV_FILE"', DOCTOR.read_text())
        self.assertNotIn('. "$ENV_FILE"', DOCTOR.read_text())


if __name__ == "__main__":
    unittest.main()
