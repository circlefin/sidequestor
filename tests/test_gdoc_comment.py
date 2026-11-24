from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/sidequestor/runtime/yaas-triage/skills/yaas-gdoc-anchored-comments/gdoc-comment.py"


class GDocCommentTests(unittest.TestCase):
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

    def workspace(self, root: Path, *, allow_send: bool) -> Path:
        quest = root / "state/quests/active/review-doc"
        quest.mkdir(parents=True)
        (quest / "meta.json").write_text(json.dumps({"allow_send": allow_send}))
        (quest / "timeline.ndjson").write_text("")
        return quest

    def fake_gws(self, root: Path) -> tuple[Path, Path]:
        calls = root / "gws-calls.ndjson"
        script = root / "fake-gws"
        script.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, os, sys
            from pathlib import Path

            args = sys.argv[1:]
            calls = Path(os.environ["FAKE_GWS_CALLS"])
            with calls.open("a") as stream:
                stream.write(json.dumps(args) + "\\n")
            if args[:3] == ["drive", "files", "get"]:
                params = json.loads(args[args.index("--params") + 1])
                if os.environ.get("FAKE_SHARED_DRIVE") == "1" and params.get("supportsAllDrives") is not True:
                    print("error[api]: File not found: doc-123", file=sys.stderr)
                    raise SystemExit(2)
                result = {"capabilities": {
                    "canComment": os.environ.get("FAKE_CAN_COMMENT", "1") == "1",
                    "canEdit": os.environ.get("FAKE_CAN_EDIT", "1") == "1",
                }}
            elif args[:3] == ["docs", "documents", "get"]:
                result = {
                    "documentId": "doc-123", "revisionId": "rev-1",
                    "tabs": [{"tabProperties": {"tabId": "t.0"}, "documentTab": {
                        "body": {"content": [{"paragraph": {"elements": [
                            {"startIndex": 1, "endIndex": 8, "textRun": {"content": "Unique "}},
                            {"startIndex": 8, "endIndex": 15, "textRun": {"content": "heading"}},
                            {"startIndex": 15, "endIndex": 16, "textRun": {"content": "\\n"}},
                        ]}}]}}}, {"tabProperties": {"tabId": "t.1"}, "documentTab": {
                        "body": {"content": [{"table": {"tableRows": [{"tableCells": [
                            {"content": [{"paragraph": {"elements": [
                                {"startIndex": 2, "endIndex": 6, "textRun": {"content": "A😀B"}},
                            ]}}]}
                        ]}]}}]}}}],
                }
                if os.environ.get("FAKE_DUPLICATE") == "1":
                    result["tabs"][1]["documentTab"]["body"]["content"] = (
                        result["tabs"][0]["documentTab"]["body"]["content"])
                if os.environ.get("FAKE_DOCUMENT"):
                    result = json.loads(os.environ["FAKE_DOCUMENT"])
                if os.environ.get("FAKE_NO_REVISION") == "1":
                    result.pop("revisionId", None)
            elif args[:3] == ["docs", "documents", "batchUpdate"]:
                if os.environ.get("FAKE_WRITE_MODE") == "error":
                    print("write request timed out", file=sys.stderr)
                    raise SystemExit(2)
                body = json.loads(args[args.index("--json") + 1])
                request = body["requests"][0]["insertComment"]
                quote = os.environ.get("FAKE_QUOTE", "Unique heading")
                result = {
                    "documentId": "doc-123",
                    "commentUpdateState": "ALL_SAVED" if os.environ.get("FAKE_WRITE_MODE") != "unsaved" else "ALL_FAILED_UNKNOWN_REASON",
                    "replies": [{"insertComment": {"commentThread": {
                        "commentId": "comment-1", "anchorId": "kix.anchor-1",
                        "plainTextQuote": quote,
                        "headPost": {"content": request["content"]},
                    }}}],
                }
            else:
                print("unexpected gws call", file=sys.stderr)
                raise SystemExit(2)
            print("Using keyring backend: keyring")
            print(json.dumps(result))
        """))
        script.chmod(0o755)
        return script, calls

    def run_helper(self, workspace: Path, gws: Path, payload: dict, **extra_env):
        env = dict(os.environ)
        env.update({
            "SIDEQUESTOR_WORKSPACE": str(workspace),
            "SIDEQUESTOR_DISPATCH_TARGET": "review-doc",
            "SIDEQUESTOR_GDOC_COMMENTS_ENABLED": "1",
            "GWS_BIN": str(gws),
            "FAKE_GWS_CALLS": str(workspace / "gws-calls.ndjson"),
        })
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(HELPER), json.dumps(payload)],
            text=True, capture_output=True, env=env,
        )

    @staticmethod
    def writes(calls: Path):
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()
                if json.loads(line)[:3] == ["docs", "documents", "batchUpdate"]]

    def test_successful_write_is_logged_and_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            quest = self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)

            first = self.run_helper(root, gws, self.payload())
            gws.unlink()  # A completed key can replay even when Google is temporarily unavailable.
            second = self.run_helper(root, gws, self.payload())

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(1, len(self.writes(calls)))
            self.assertTrue(json.loads(second.stdout)["idempotent_replay"])
            write = self.writes(calls)[0]
            body = json.loads(write[write.index("--json") + 1])
            self.assertEqual({"startIndex": 1, "endIndex": 15, "tabId": "t.0"},
                             body["requests"][0]["insertComment"]["range"])
            self.assertEqual("rev-1", body["writeControl"]["requiredRevisionId"])
            event = json.loads(quest.joinpath("timeline.ndjson").read_text().splitlines()[-1])
            self.assertEqual("gdoc_comments_added", event["event"])
            self.assertEqual(["comment-1"], event["comment_ids"])

    def test_shared_drive_document_can_be_commented_and_verified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            result = self.run_helper(root, gws, self.payload(), FAKE_SHARED_DRIVE="1")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("anchored", json.loads(result.stdout)["results"][0]["status"])
            self.assertEqual(1, len(self.writes(calls)))

    def test_non_bmp_anchor_uses_utf16_offsets_and_tab_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            result = self.run_helper(root, gws, self.payload(
                anchors=[{"text": "😀B", "comment": "Emoji anchor."}]), FAKE_QUOTE="😀B")
            self.assertEqual(0, result.returncode, result.stderr)
            write = self.writes(calls)[0]
            body = json.loads(write[write.index("--json") + 1])
            self.assertEqual({"startIndex": 3, "endIndex": 6, "tabId": "t.1"},
                             body["requests"][0]["insertComment"]["range"])

    def test_ambiguous_anchor_fails_before_reserving_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            result = self.run_helper(root, gws, self.payload(), FAKE_DUPLICATE="1")
            self.assertEqual(1, result.returncode)
            self.assertIn("ambiguous", result.stderr)
            self.assertEqual([], self.writes(calls))
            self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_disabled_and_unapproved_writes_never_call_gws(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=False)
            gws, calls = self.fake_gws(root)
            disabled = self.run_helper(root, gws, self.payload(), SIDEQUESTOR_GDOC_COMMENTS_ENABLED="0")
            unapproved = self.run_helper(root, gws, self.payload())
            self.assertEqual(1, disabled.returncode)
            self.assertIn("disabled", disabled.stderr)
            self.assertEqual(1, unapproved.returncode)
            self.assertIn("allow_send false", unapproved.stderr)
            self.assertFalse(calls.exists())

    def test_comment_only_access_writes_without_revision_control(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            accepted = self.run_helper(root, gws, self.payload(),
                                       FAKE_CAN_COMMENT="1", FAKE_CAN_EDIT="0",
                                       FAKE_NO_REVISION="1")
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(1, len(self.writes(calls)))
            write = self.writes(calls)[0]
            body = json.loads(write[write.index("--json") + 1])
            self.assertNotIn("writeControl", body)
            self.assertEqual("Unique heading", json.loads(accepted.stdout)["results"][0]["quoted_text"])

    def test_segment_anchors_include_segment_and_nested_tab_ids(self):
        content = [{"paragraph": {"elements": [{
            "startIndex": 0, "endIndex": 5, "textRun": {"content": "A😀B\n"},
        }]}}]
        for kind in ("headers", "footers", "footnotes"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.workspace(root, allow_send=True)
                gws, calls = self.fake_gws(root)
                document = {
                    "documentId": "doc-123", "revisionId": "rev-1", "tabs": [{
                        "tabProperties": {"tabId": "parent"}, "childTabs": [{
                            "tabProperties": {"tabId": "child"},
                            "documentTab": {kind: {"segment-1": {"content": content}}},
                        }],
                    }],
                }
                result = self.run_helper(root, gws, self.payload(
                    anchors=[{"text": "😀B", "comment": "Segment comment."}]),
                    FAKE_DOCUMENT=json.dumps(document), FAKE_QUOTE="😀B")
                self.assertEqual(0, result.returncode, result.stderr)
                write = self.writes(calls)[0]
                body = json.loads(write[write.index("--json") + 1])
                self.assertEqual({"startIndex": 1, "endIndex": 4, "tabId": "child",
                                  "segmentId": "segment-1"},
                                 body["requests"][0]["insertComment"]["range"])

    def test_body_and_segment_duplicates_fail_before_reserving_key(self):
        content = [{"paragraph": {"elements": [{
            "startIndex": 1, "endIndex": 16,
            "textRun": {"content": "Unique heading\n"},
        }]}}]
        for kind in ("headers", "footers", "footnotes"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.workspace(root, allow_send=True)
                gws, calls = self.fake_gws(root)
                document = {
                    "documentId": "doc-123", "revisionId": "rev-1", "tabs": [{
                        "tabProperties": {"tabId": "t.0"}, "documentTab": {
                            "body": {"content": content},
                            kind: {"segment-1": {"content": content}},
                        },
                    }],
                }
                result = self.run_helper(root, gws, self.payload(),
                                         FAKE_DOCUMENT=json.dumps(document))
                self.assertEqual(1, result.returncode)
                self.assertIn("ambiguous (2 matches)", result.stderr)
                self.assertEqual([], self.writes(calls))
                self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_comment_utf8_limit_is_checked_before_network_or_reserving_key(self):
        for comment in ("x" * 2049, "😀" * 513):
            with self.subTest(bytes=len(comment.encode("utf-8"))), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.workspace(root, allow_send=True)
                gws, calls = self.fake_gws(root)
                result = self.run_helper(root, gws, self.payload(
                    anchors=[{"text": "Unique heading", "comment": comment}]))
                self.assertEqual(1, result.returncode)
                self.assertIn("2048 UTF-8 bytes", result.stderr)
                self.assertFalse(calls.exists())
                self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_comment_at_utf8_limit_can_be_written(self):
        for comment in ("x" * 2048, "😀" * 512):
            with self.subTest(bytes=len(comment.encode("utf-8"))), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.workspace(root, allow_send=True)
                gws, calls = self.fake_gws(root)
                result = self.run_helper(root, gws, self.payload(
                    anchors=[{"text": "Unique heading", "comment": comment}]))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(1, len(self.writes(calls)))

    def test_claimed_approval_is_bound_to_exact_document_and_comment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=False)
            gws, calls = self.fake_gws(root)
            payload = self.payload(approval_id="approval-1")
            spec = subprocess.run(
                [sys.executable, str(HELPER), "approval-spec", json.dumps(self.payload())],
                text=True, capture_output=True,
                env={**os.environ, "SIDEQUESTOR_WORKSPACE": str(root)},
            )
            self.assertEqual(0, spec.returncode, spec.stderr)
            (root / "state/pending-approvals.json").write_text(json.dumps({
                "version": 1, "items": [{
                    "id": "approval-1", "quest_id": "review-doc", "status": "executing",
                    "lease_expires_at": "2999-01-01T00:00:00Z", **json.loads(spec.stdout),
                }],
            }))
            accepted = self.run_helper(root, gws, payload)
            changed = self.run_helper(root, gws, self.payload(
                approval_id="approval-1", idempotency_key="another-key",
                anchors=[{"text": "Unique heading", "comment": "Different comment."}],
            ))
            self.assertEqual(0, accepted.returncode, accepted.stderr)
            self.assertEqual(1, changed.returncode)
            self.assertIn("no claimed Google Doc approval", changed.stderr)
            self.assertEqual(1, len(self.writes(calls)))

    def test_missing_gws_does_not_consume_idempotency_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            result = self.run_helper(root, root / "missing-gws", self.payload())
            self.assertEqual(1, result.returncode)
            self.assertIn("gws CLI is not installed", result.stderr)
            self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_comment_permission_is_checked_before_reserving_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            result = self.run_helper(root, gws, self.payload(), FAKE_CAN_COMMENT="0")
            self.assertEqual(1, result.returncode)
            self.assertIn("cannot comment", result.stderr)
            self.assertEqual([], self.writes(calls))
            self.assertFalse((root / "state/gdoc-comment-idempotency.json").exists())

    def test_gws_write_error_is_indeterminate_and_not_retried(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            first = self.run_helper(root, gws, self.payload(), FAKE_WRITE_MODE="error")
            second = self.run_helper(root, gws, self.payload())
            self.assertEqual(2, first.returncode)
            self.assertIn("indeterminate", first.stderr)
            self.assertEqual(2, second.returncode)
            self.assertIn("indeterminate prior attempt", second.stderr)
            self.assertEqual(1, len(self.writes(calls)))

    def test_unsaved_or_wrong_quote_is_never_reported_as_success(self):
        for extra in ({"FAKE_WRITE_MODE": "unsaved"}, {"FAKE_QUOTE": "Other text"}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.workspace(root, allow_send=True)
                gws, calls = self.fake_gws(root)
                result = self.run_helper(root, gws, self.payload(), **extra)
                self.assertEqual(2, result.returncode)
                self.assertIn("did not verify", result.stderr)
                self.assertEqual(1, len(self.writes(calls)))

    def test_completed_write_can_finish_timeline_logging_on_replay(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            quest = self.workspace(root, allow_send=True)
            gws, calls = self.fake_gws(root)
            timeline = quest / "timeline.ndjson"
            timeline.unlink()
            timeline.mkdir()
            first = self.run_helper(root, gws, self.payload())
            self.assertEqual(2, first.returncode)
            self.assertIn("completed but timeline logging failed", first.stderr)
            timeline.rmdir()
            timeline.write_text("")
            second = self.run_helper(root, gws, self.payload())
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(1, len(self.writes(calls)))
            self.assertTrue(json.loads(second.stdout)["logged"])


if __name__ == "__main__":
    unittest.main()
