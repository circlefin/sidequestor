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
SKILLS_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "skills"
FORBIDDEN_SKILL_PATTERNS = (
    "python3 yaas-triage/",
    "bash yaas-triage/",
    "./yaas-triage/",
    "MCP_CALL=yaas-triage/",
    "\nyaas-triage/tests/",
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

    def test_shipped_skills_do_not_use_workspace_relative_helper_commands(self) -> None:
        offenders: list[str] = []
        for path in sorted(SKILLS_ROOT.rglob("SKILL.md")):
            text = path.read_text()
            for pattern in FORBIDDEN_SKILL_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {pattern}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
