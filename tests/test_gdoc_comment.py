from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/sidequestor/runtime/yaas-triage/skills/yaas-gdoc-anchored-comments/gdoc-comment.py"
DRIVER = ROOT / "src/sidequestor/runtime/yaas-triage/skills/yaas-gdoc-anchored-comments/playwright-driver.py"


def load_driver():
    spec = importlib.util.spec_from_file_location("sidequestor_gdoc_playwright_driver", DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GDocCommentTests(unittest.TestCase):
    def workspace(self, root: Path, *, allow_send: bool) -> Path:
        quest = root / "state/quests/active/review-doc"
        quest.mkdir(parents=True)
        (quest / "meta.json").write_text(json.dumps({"allow_send": allow_send}))
        (quest / "timeline.ndjson").write_text("")
        return quest

    def driver(self, root: Path) -> tuple[Path, Path]:
        calls = root / "driver-calls.ndjson"
        script = root / "fake-driver.py"
        script.write_text(textwrap.dedent(f"""\
            import json, sys
            payload = json.load(sys.stdin)
            with open({str(calls)!r}, "a") as stream:
                stream.write(json.dumps(payload) + "\\n")
            print(json.dumps({{
                "ok": True,
                "doc_id": payload["doc_id"],
                "results": [{{
                    "anchor": item["text"],
                    "comment": item["comment"],
                    "comment_id": f"comment-{{index}}",
                    "quoted_text": item["text"],
                    "status": "anchored",
                }} for index, item in enumerate(payload["anchors"], 1)],
            }}))
        """))
        return script, calls

    def run_helper(self, workspace: Path, driver: Path, payload: dict, **extra_env):
        env = dict(os.environ)
        env.update({
            "SIDEQUESTOR_WORKSPACE": str(workspace),
            "SIDEQUESTOR_DISPATCH_TARGET": "review-doc",
            "SIDEQUESTOR_GDOC_COMMENTS_ENABLED": "1",
            "SIDEQUESTOR_GDOC_COMMENT_DRIVER": str(driver),
            "SIDEQUESTOR_GDOC_COMMENT_TEST_MODE": "1",
            "SIDEQUESTOR_PYTHON": sys.executable,
        })
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(HELPER), json.dumps(payload)],
            text=True, capture_output=True, env=env,
        )

    @staticmethod
    def payload(**updates):
        value = {
            "quest_id": "review-doc",
            "idempotency_key": "review-doc/doc-123/revision-1",
            "doc_id": "doc-123",
            "anchors": [{"text": "Unique heading", "comment": "Make this clearer."}],
            "note": "Proposal review comments",
        }
        value.update(updates)
        return value

    def test_successful_write_is_logged_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            quest = self.workspace(root, allow_send=True)
            driver, calls = self.driver(root)

            first = self.run_helper(root, driver, self.payload())
            second = self.run_helper(root, driver, self.payload())

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(1, len(calls.read_text().splitlines()))
            replay = json.loads(second.stdout)
            self.assertTrue(replay["idempotent_replay"])
            event = json.loads(quest.joinpath("timeline.ndjson").read_text().splitlines()[-1])
            self.assertEqual("gdoc_comments_added", event["event"])
            self.assertEqual("google_docs", event["surface"])
            self.assertEqual("doc-123", event["doc_id"])
            self.assertEqual(["comment-1"], event["comment_ids"])
            self.assertEqual("Proposal review comments", event["note"])

    def test_disabled_capability_fails_before_the_driver(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            driver, calls = self.driver(root)

            result = self.run_helper(
                root, driver, self.payload(), SIDEQUESTOR_GDOC_COMMENTS_ENABLED="0")

            self.assertEqual(1, result.returncode)
            self.assertIn("disabled", result.stderr)
            self.assertFalse(calls.exists())

    def test_allow_send_false_fails_before_the_driver(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=False)
            driver, calls = self.driver(root)

            result = self.run_helper(root, driver, self.payload())

            self.assertEqual(1, result.returncode)
            self.assertIn("allow_send false", result.stderr)
            self.assertFalse(calls.exists())

    def test_claimed_approval_is_bound_to_the_exact_document_and_comments(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=False)
            driver, calls = self.driver(root)
            payload = self.payload(approval_id="approval-1")
            approval_spec = subprocess.run(
                [sys.executable, str(HELPER), "approval-spec", json.dumps(self.payload())],
                text=True, capture_output=True,
                env={**os.environ, "SIDEQUESTOR_WORKSPACE": str(root)},
            )
            self.assertEqual(0, approval_spec.returncode, approval_spec.stderr)
            approval = json.loads(approval_spec.stdout)
            (root / "state/pending-approvals.json").write_text(json.dumps({
                "version": 1,
                "items": [{
                    "id": "approval-1", "quest_id": "review-doc", "status": "executing",
                    "lease_expires_at": "2999-01-01T00:00:00Z", **approval,
                }],
            }))

            result = self.run_helper(root, driver, payload)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(1, len(calls.read_text().splitlines()))
            changed = self.run_helper(root, driver, self.payload(
                approval_id="approval-1",
                idempotency_key="review-doc/doc-123/revision-2",
                anchors=[{"text": "Unique heading", "comment": "Different comment."}],
            ))
            self.assertEqual(1, changed.returncode)
            self.assertIn("no claimed Google Doc approval", changed.stderr)
            self.assertEqual(1, len(calls.read_text().splitlines()))

    def test_every_write_requires_an_idempotency_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            driver, calls = self.driver(root)

            result = self.run_helper(
                root, driver, self.payload(idempotency_key=""), SIDEQUESTOR_DISPATCH_TARGET="")

            self.assertEqual(1, result.returncode)
            self.assertIn("idempotency_key", result.stderr)
            self.assertFalse(calls.exists())

    def test_missing_playwright_fails_without_consuming_the_idempotency_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            python = root / "python-without-playwright"
            python.write_text("#!/bin/sh\nexit 3\n")
            python.chmod(0o755)
            env = dict(os.environ)
            env.pop("SIDEQUESTOR_GDOC_COMMENT_DRIVER", None)
            env.update({
                "SIDEQUESTOR_WORKSPACE": str(root),
                "SIDEQUESTOR_DISPATCH_TARGET": "review-doc",
                "SIDEQUESTOR_GDOC_COMMENTS_ENABLED": "1",
                "SIDEQUESTOR_PYTHON": str(python),
            })

            result = subprocess.run(
                [sys.executable, str(HELPER), json.dumps(self.payload())],
                text=True, capture_output=True, env=env,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("Playwright is not installed", result.stderr)
            self.assertNotIn("indeterminate", result.stderr)
            self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_driver_failure_is_recorded_as_indeterminate_and_not_retried(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            driver = root / "failing-driver.py"
            calls = root / "calls"
            driver.write_text(textwrap.dedent(f"""\
                from pathlib import Path
                import sys
                path = Path({str(calls)!r})
                path.write_text(path.read_text() + "x" if path.exists() else "x")
                print("browser disconnected after posting", file=sys.stderr)
                raise SystemExit(2)
            """))

            first = self.run_helper(root, driver, self.payload())
            second = self.run_helper(root, driver, self.payload())

            self.assertEqual(2, first.returncode)
            self.assertEqual(2, second.returncode)
            self.assertEqual("x", calls.read_text())
            self.assertIn("indeterminate prior attempt", second.stderr)

    def test_completed_write_stays_complete_when_timeline_logging_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            quest = self.workspace(root, allow_send=True)
            driver, calls = self.driver(root)
            timeline = quest / "timeline.ndjson"
            timeline.unlink()
            timeline.mkdir()

            first = self.run_helper(root, driver, self.payload())

            self.assertEqual(2, first.returncode)
            self.assertIn("completed but timeline logging failed", first.stderr)
            records = json.loads(
                root.joinpath("state/gdoc-comment-idempotency.json").read_text())
            self.assertEqual("complete", records[self.payload()["idempotency_key"]]["status"])

            timeline.rmdir()
            timeline.write_text("")
            second = self.run_helper(root, driver, self.payload())
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(1, len(calls.read_text().splitlines()))
            self.assertTrue(json.loads(second.stdout)["logged"])

    def test_driver_override_requires_explicit_test_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            driver, calls = self.driver(root)

            result = self.run_helper(
                root, driver, self.payload(), SIDEQUESTOR_GDOC_COMMENT_TEST_MODE="0")

            self.assertEqual(1, result.returncode)
            self.assertIn("test mode", result.stderr)
            self.assertFalse(calls.exists())
            self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_driver_result_must_match_the_requested_document_and_anchor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            driver = root / "forged-driver.py"
            driver.write_text(textwrap.dedent("""\
                import json, sys
                payload = json.load(sys.stdin)
                print(json.dumps({
                    "ok": True,
                    "doc_id": "another-document",
                    "results": [{
                        "anchor": "another anchor",
                        "comment": payload["anchors"][0]["comment"],
                        "comment_id": "comment-1",
                        "quoted_text": payload["anchors"][0]["text"],
                        "status": "anchored",
                    }],
                }))
            """))

            result = self.run_helper(root, driver, self.payload())

            self.assertEqual(2, result.returncode)
            self.assertIn("unverified", result.stderr)

    def test_one_anchor_per_idempotent_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            driver, calls = self.driver(root)
            payload = self.payload(anchors=[
                {"text": "First", "comment": "One"},
                {"text": "Second", "comment": "Two"},
            ])

            result = self.run_helper(root, driver, payload)

            self.assertEqual(1, result.returncode)
            self.assertIn("exactly 1 item", result.stderr)
            self.assertFalse(calls.exists())


class PlaywrightDriverTests(unittest.TestCase):
    def test_unattended_driver_rejects_comment_only_access(self):
        driver = load_driver()
        with patch.object(driver, "_json_from_gws", return_value={
            "capabilities": {"canComment": True, "canEdit": False},
        }):
            with self.assertRaisesRegex(RuntimeError, "comment-only"):
                driver._assert_capabilities("doc")

    def test_reachable_non_loopback_cdp_endpoint_is_still_rejected(self):
        driver = load_driver()
        with patch.object(driver, "_cdp_ready", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "loopback HTTP"):
                driver._ensure_chrome("http://example.com:9222")

    def test_find_enables_match_case_and_presses_enter_on_the_find_input(self):
        driver = load_driver()
        checkbox = Mock()
        checkbox.count.return_value = 1
        checkbox.is_checked.return_value = True
        dialog = Mock()
        dialog.get_by_role.return_value = checkbox
        dialog.inner_text.return_value = "1 of 1"
        find = Mock()

        page = Mock()
        driver._select_unique_match(page, dialog, find, "Exact Anchor")

        dialog.get_by_role.assert_called_once_with("checkbox", name="Match case")
        checkbox.check.assert_called_once_with()
        find.fill.assert_called_once_with("Exact Anchor")
        find.press.assert_called_once_with("Enter")

    def test_ambiguous_find_is_rejected_before_selection(self):
        driver = load_driver()
        checkbox = Mock()
        checkbox.count.return_value = 1
        checkbox.is_checked.return_value = True
        dialog = Mock()
        dialog.get_by_role.return_value = checkbox
        dialog.inner_text.return_value = "1 of 2"
        find = Mock()

        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            driver._select_unique_match(Mock(), dialog, find, "Repeated")
        find.press.assert_not_called()

    def test_post_button_is_clicked_only_once(self):
        driver = load_driver()
        composer = Mock()
        button = Mock()
        page = Mock()
        page.locator.return_value.first = composer
        page.get_by_role.return_value = button
        verified = {
            "anchor": "Anchor", "comment": "Comment", "comment_id": "new-id",
            "quoted_text": "Anchor", "status": "anchored",
        }
        requested = {"text": "Anchor", "comment": "Comment"}

        with patch.object(driver, "_new_verified_comment", return_value=verified):
            result = driver._post_and_verify(page, "doc", {"old-id"}, requested)

        self.assertEqual(verified, result)
        composer.wait_for.assert_called_once_with(state="visible", timeout=8000)
        composer.fill.assert_called_once_with("Comment")
        button.click.assert_called_once_with()

    def test_comment_verification_requires_exact_case(self):
        driver = load_driver()
        wrong = {
            "old": {"id": "old", "content": "Comment"},
            "wrong-case": {
                "id": "wrong-case", "content": "Comment",
                "quotedFileContent": {"value": "anchor"},
            },
        }
        right = {
            **wrong,
            "right": {
                "id": "right", "content": "Comment",
                "quotedFileContent": {"value": "Anchor"},
            },
        }
        with patch.object(driver, "_comments", side_effect=[wrong, right]), \
                patch.object(driver.time, "sleep"):
            result = driver._new_verified_comment(
                "doc", {"old"}, {"text": "Anchor", "comment": "Comment"}, timeout=1)
        self.assertEqual("right", result["comment_id"])


if __name__ == "__main__":
    unittest.main()
