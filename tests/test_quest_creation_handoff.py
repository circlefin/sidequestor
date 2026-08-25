"""Quest creation is terminal-only.

The dashboard form could only collect one block of free text, so the watches a quest
needs had to be guessed by the first worker run. A guess that missed scaffolded a quest
with no live watch, which then read as healthy in the UI. These tests pin the hand-off:
the browser no longer offers creation, and the endpoint refuses even a stale client.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "dashboard.html"
DASHBOARD_SERVER = (PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage"
                    / "ops" / "dashboard-server.py")


class QuestCreationHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = DASHBOARD_HTML.read_text()
        self.server = DASHBOARD_SERVER.read_text()
        match = re.search(r'<dialog id="quest-dialog">.*?</dialog>', self.html, re.S)
        assert match, "quest-dialog is missing from the dashboard"
        self.dialog = match.group(0)

    def test_dashboard_no_longer_posts_a_new_quest(self) -> None:
        # The endpoint refusing is the real guard; this catches a form sneaking back in.
        self.assertNotIn("/api/quests", self.html)
        self.assertNotIn("<form", self.dialog)
        for field in ("#quest-prompt", "#quest-title", "#quest-priority"):
            self.assertNotIn(field, self.html, f"{field} should be gone with the form")

    def test_dialog_hands_the_user_to_the_terminal(self) -> None:
        self.assertIn("quest-guide-snippet", self.dialog)
        self.assertIn("copy-quest-guide", self.dialog)
        self.assertIn('id="new-quest">New quest</button>', self.html)
        # The prompt must name the skill, or the hand-off sends people nowhere useful.
        self.assertIn("yaas-quest-creation/SKILL.md", self.html)
        self.assertIn("OPERATING.md", self.html)

    def test_copy_prompt_resolves_the_packaged_runtime_path(self) -> None:
        # SKILL.md tells the agent to run ./yaas-triage/..., which does not exist in a
        # pip workspace. The loop appends the real root to every dispatch; a human
        # pasting this prompt gets no such hint, so the snippet has to carry it.
        self.assertIn("RUNTIME_ROOT", self.html)

    def test_field_guide_does_not_advertise_dashboard_creation(self) -> None:
        guide = re.search(r'<article class="help-place dashboard">.*?</article>', self.html, re.S)
        assert guide, "field guide dashboard panel is missing"
        self.assertNotIn("Create a quest", guide.group(0))

    def test_create_endpoint_is_closed(self) -> None:
        handler = re.search(r"def _handle_create_quest\(self, payload: dict\):.*?\n    def ",
                            self.server, re.S)
        assert handler, "_handle_create_quest is missing"
        body = handler.group(0)
        self.assertIn("410", body)
        # It must not scaffold anything any more.
        self.assertNotIn("new-quest.py", body)
        self.assertNotIn("subprocess", body)


if __name__ == "__main__":
    unittest.main()
