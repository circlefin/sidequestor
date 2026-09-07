from __future__ import annotations

import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "dashboard.html"


def function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"could not find the end of {name}")


class DashboardComposerPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = DASHBOARD_HTML.read_text()

    def test_composer_is_static_and_outside_the_polled_detail_subtree(self) -> None:
        static_html = self.html.split("<script>", 1)[0]
        render_focus = function_body(self.html, "renderFocus")

        self.assertEqual(self.html.count('id="instruction"'), 1)
        self.assertIn('id="quest-focus-content"', static_html)
        self.assertIn('class="focus-content"', static_html)
        self.assertIn('id="instruction-form"', static_html)
        self.assertIn("$('#quest-focus-content').htmlOnce=", render_focus)
        self.assertNotIn('id="instruction"', render_focus)
        self.assertNotIn('id="instruction-form"', render_focus)
        self.assertNotIn('class="focus-footer"', render_focus)

    def test_drafts_are_scoped_to_the_selected_quest(self) -> None:
        sync_composer = function_body(self.html, "syncInstructionComposer")

        self.assertIn("instructionDrafts.set", sync_composer)
        self.assertIn("instructionDrafts.get", sync_composer)
        self.assertIn("form.dataset.quest=questId", sync_composer)
        render = function_body(self.html, "render")
        self.assertIn("syncInstructionComposer(state.selected)", render)
        self.assertIn("state.detail?.id===state.selected", render)

    def test_submit_stays_bound_to_the_quest_that_owned_the_draft(self) -> None:
        start = self.html.index("document.addEventListener('submit'")
        end = self.html.index("$('#review-dialog').addEventListener", start)
        submit_handler = self.html[start:end]

        self.assertIn("form.dataset.quest!==questId", submit_handler)
        self.assertIn("form.dataset.quest===questId", submit_handler)

    def test_poll_restore_no_longer_touches_the_instruction_composer(self) -> None:
        self.assertNotIn("#instruction", function_body(self.html, "captureUi"))
        self.assertNotIn("#instruction", function_body(self.html, "restoreUi"))
        self.assertNotIn("instruction", function_body(self.html, "captureFocus"))


if __name__ == "__main__":
    unittest.main()
