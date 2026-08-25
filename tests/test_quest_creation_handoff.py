"""Dashboard creation must enter the explicit, schedule-free bootstrap path."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "dashboard.html"
DASHBOARD_SERVER = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
                    / "ops" / "dashboard-server.py")


class QuestCreationBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.html = DASHBOARD_HTML.read_text()
        self.server = DASHBOARD_SERVER.read_text()
        match = re.search(r'<dialog id="quest-dialog">.*?</dialog>', self.html, re.S)
        self.assertIsNotNone(match)
        self.dialog = match.group(0)

    def test_dashboard_posts_bootstrap_creation_form(self):
        self.assertIn('id="quest-form"', self.dialog)
        self.assertIn('id="quest-prompt"', self.dialog)
        self.assertIn("post('/api/quests'", self.html)

    def test_endpoint_creates_flagged_empty_quest_without_schedule(self):
        handler = re.search(r"def _handle_create_quest\(self, payload: dict\):.*?\n    def ",
                            self.server, re.S).group(0)
        self.assertIn('"sidequestor_bootstrap": True', handler)
        self.assertIn('"watches": []', handler)
        self.assertNotIn("next_fire_ts", handler)
        self.assertNotIn("requires_initial_run", handler)

    def test_initialising_state_reads_only_explicit_flag(self):
        match = re.search(r"function isInitialising\(q\)\{.*?\}", self.html)
        self.assertEqual(match.group(0),
                         "function isInitialising(q){return q.sidequestor_bootstrap===true}")

    def test_terminal_fallback_shell_quotes_workspace_path(self):
        self.assertIn("function shellQuote", self.html)
        self.assertIn("'cd '+shellQuote(p)", self.html)
        self.assertNotIn("'cd '+p", self.html)

    def test_field_guide_describes_dashboard_bootstrap_creation(self):
        self.assertIn("Sidequestor visibly bootstraps exact watches", self.html)


if __name__ == "__main__":
    unittest.main()
