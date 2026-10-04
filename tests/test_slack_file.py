from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SURFACES = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "surfaces"
SLACK_FILE = SURFACES / "slack-file.py"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
UPLOAD_URL = "https://files.slack.com/upload/v1/abc"
DOWNLOAD_URL = "https://files.slack.com/files-pri/T1-F1/download/pic.png"


def _load_module():
    spec = importlib.util.spec_from_file_location("slack_file_under_test", SLACK_FILE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, ctype: str = "image/png"):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSlack:
    """Routes client.slack_request by Web API method and records every call."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, method="GET", headers=None, body=None, timeout=None):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, body))
        reply = self.responses[name]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, payload = reply
        return status, payload if isinstance(payload, str) else json.dumps(payload)

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


def _shared_info(channel="D123", ts="1790000000.000100"):
    return (200, {"ok": True, "file": {
        "id": "F1", "permalink": "https://x.slack.com/files/U1/F1/pic.png",
        "shares": {"private": {channel: [{"ts": ts}]}}}})


class TransportTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()
        cls.sf = cls.mod.slack_files

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-slack-file-")
        self.dir = Path(self.temp.name)
        self.file = self.dir / "pic.png"
        self.file.write_bytes(PNG)

    def tearDown(self) -> None:
        self.temp.cleanup()


class UploadTest(TransportTestCase):
    def _upload(self, slack: FakeSlack, opened: MagicMock | None = None, **kwargs):
        opened = opened or MagicMock(return_value=FakeResponse(status=200))
        with patch.object(self.sf.client, "slack_request", slack), \
             patch.object(self.sf, "_open", opened):
            return self.sf.upload(self.file, "D123", sleep=lambda _: None, **kwargs), opened

    def test_three_steps_in_order_with_exact_length_and_no_bearer_on_upload_url(self) -> None:
        slack = FakeSlack({
            "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F1"}),
            "files.completeUploadExternal": (200, {"ok": True, "files": [{"id": "F1"}]}),
            "files.info": _shared_info(),
        })
        result, opened = self._upload(slack, thread_ts="1.000001", initial_comment="hi")

        self.assertEqual(slack.methods()[:2],
                         ["files.getUploadURLExternal", "files.completeUploadExternal"])
        self.assertIn(f"length={len(PNG)}", slack.calls[0][1])
        self.assertIn("filename=pic.png", slack.calls[0][1])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, UPLOAD_URL)
        self.assertEqual(request.data, PNG)
        self.assertNotIn("Authorization", request.headers)
        complete = json.loads(slack.calls[1][1])
        self.assertEqual(complete["files"], [{"id": "F1", "title": "pic.png"}])
        self.assertEqual(complete["channel_id"], "D123")
        self.assertEqual(complete["thread_ts"], "1.000001")
        self.assertEqual(complete["initial_comment"], "hi")
        self.assertEqual(result["response_ts"], "1790000000.000100")
        self.assertEqual(result["file_id"], "F1")

    def test_refuses_an_upload_url_outside_slack(self) -> None:
        slack = FakeSlack({"files.getUploadURLExternal": (
            200, {"ok": True, "upload_url": "https://evil.example/u", "file_id": "F1"})})
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._upload(slack)
        self.assertNotIn("files.completeUploadExternal", slack.methods())
        self.assertEqual(caught.exception.code, self.sf.ERROR)

    def test_bytes_swapped_after_authorization_are_not_uploaded(self) -> None:
        slack = FakeSlack({"files.getUploadURLExternal": (
            200, {"ok": True, "upload_url": UPLOAD_URL, "file_id": "F1"})})
        authorized = self.sf.sha256_file(self.file)
        self.file.write_bytes(b"\x00" * len(PNG))  # same size, different bytes
        opened = MagicMock(return_value=FakeResponse(status=200))
        with self.assertRaises(self.sf.SlackFileError):
            self._upload(slack, opened, sha256=authorized)
        opened.assert_not_called()
        self.assertNotIn("files.completeUploadExternal", slack.methods())

    def test_rate_limited_ticket_is_transient(self) -> None:
        slack = FakeSlack({"files.getUploadURLExternal": (429, {"ok": False,
                                                                "error": "ratelimited"})})
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._upload(slack)
        self.assertEqual(caught.exception.code, self.sf.TRANSIENT)

    def test_definitive_completion_error_is_not_reported_as_unknown(self) -> None:
        slack = FakeSlack({
            "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F1"}),
            "files.completeUploadExternal": (200, {"ok": False, "error": "invalid_channel"}),
        })
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._upload(slack)
        self.assertEqual(caught.exception.code, self.sf.ERROR)
        self.assertNotIn("files.info", slack.methods())

    def test_completion_timeout_is_reconciled_not_retried(self) -> None:
        slack = FakeSlack({
            "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F1"}),
            "files.completeUploadExternal": TimeoutError("read timed out"),
            "files.info": _shared_info(),
        })
        result, _ = self._upload(slack)
        self.assertEqual(slack.methods().count("files.completeUploadExternal"), 1)
        self.assertEqual(result["response_ts"], "1790000000.000100")

    def test_completion_server_errors_reconcile_a_visible_share(self) -> None:
        for error in ("internal_error", "fatal_error"):
            with self.subTest(error=error):
                slack = FakeSlack({
                    "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                         "file_id": "F1"}),
                    "files.completeUploadExternal": (200, {"ok": False, "error": error}),
                    "files.info": _shared_info(),
                })
                result, _ = self._upload(slack)
                self.assertEqual(result["response_ts"], "1790000000.000100")
                self.assertEqual(slack.methods().count("files.completeUploadExternal"), 1)

    def test_unsettled_completion_server_errors_are_not_safe_to_retry(self) -> None:
        for error in ("internal_error", "fatal_error"):
            for info in ((200, {"ok": True, "file": {"id": "F1", "shares": {}}}),
                         (200, {"ok": False, "error": "file_deleted"})):
                with self.subTest(error=error, info=info):
                    slack = FakeSlack({
                        "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                             "file_id": "F1"}),
                        "files.completeUploadExternal": (200, {"ok": False, "error": error}),
                        "files.info": info,
                    })
                    with self.assertRaises(self.sf.SlackFileError) as caught:
                        self._upload(slack)
                    self.assertEqual(caught.exception.code, self.sf.OUTCOME_UNKNOWN)
                    self.assertEqual(self.mod.EXIT_FOR[caught.exception.code], 3)
                    self.assertEqual(caught.exception.file_id, "F1")
                    self.assertEqual(slack.methods().count("files.completeUploadExternal"), 1)

    def test_unsettled_completion_reports_outcome_unknown(self) -> None:
        slack = FakeSlack({
            "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F1"}),
            "files.completeUploadExternal": (502, "bad gateway"),
            "files.info": (200, {"ok": True, "file": {"id": "F1", "shares": {}}}),
        })
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._upload(slack)
        self.assertEqual(caught.exception.code, self.sf.OUTCOME_UNKNOWN)
        self.assertEqual(slack.methods().count("files.completeUploadExternal"), 1)

    def test_deleted_file_after_uncertain_completion_stays_unknown(self) -> None:
        slack = FakeSlack({
            "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F1"}),
            "files.completeUploadExternal": (503, "unavailable"),
            "files.info": (200, {"ok": False, "error": "file_deleted"}),
        })
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._upload(slack)
        self.assertEqual(caught.exception.code, self.sf.OUTCOME_UNKNOWN)

    def test_2xx_without_ok_true_is_unknown_unless_the_share_is_visible(self) -> None:
        for body in ("", "not json", json.dumps({"files": []})):
            slack = FakeSlack({
                "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                     "file_id": "F1"}),
                "files.completeUploadExternal": (200, body),
                "files.info": (200, {"ok": True, "file": {"id": "F1", "shares": {}}}),
            })
            with self.assertRaises(self.sf.SlackFileError) as caught:
                self._upload(slack)
            self.assertEqual(caught.exception.code, self.sf.OUTCOME_UNKNOWN, body)
        slack = FakeSlack({
            "files.getUploadURLExternal": (200, {"ok": True, "upload_url": UPLOAD_URL,
                                                 "file_id": "F1"}),
            "files.completeUploadExternal": (200, ""),
            "files.info": _shared_info(),
        })
        result, _ = self._upload(slack)
        self.assertEqual(result["response_ts"], "1790000000.000100")


class DownloadTest(TransportTestCase):
    def _info(self, **overrides) -> dict:
        info = {"id": "F1", "name": "pic.png", "mimetype": "image/png", "size": len(PNG),
                "url_private_download": DOWNLOAD_URL,
                "url_private": "https://files.slack.com/files-pri/T1-F1/pic.png"}
        info.update(overrides)
        return info

    def _download(self, info: dict, responses: list, max_bytes: int = 1024):
        opened = MagicMock(side_effect=responses)
        tokens = MagicMock(side_effect=["tok-1", "tok-2"])
        with patch.object(self.sf, "_open", opened), \
             patch.object(self.sf.client, "get_slack_access_token", tokens):
            return self.sf.download(info, self.dir, max_bytes), opened, tokens

    def test_writes_exact_bytes_privately_with_the_bearer_token(self) -> None:
        path, opened, _ = self._download(self._info(), [FakeResponse(PNG)])
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(path.name, "F1.png")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, DOWNLOAD_URL)
        self.assertEqual(request.headers["Authorization"], "Bearer tok-1")

    def test_falls_back_to_url_private(self) -> None:
        info = self._info(url_private_download=None)
        _, opened, _ = self._download(info, [FakeResponse(PNG)])
        self.assertEqual(opened.call_args.args[0].full_url, info["url_private"])

    def test_login_page_refreshes_once_then_fails_as_auth_without_leftovers(self) -> None:
        html = [FakeResponse(b"<html>", ctype="text/html; charset=utf-8") for _ in range(2)]
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._download(self._info(), html)
        self.assertEqual(caught.exception.code, self.sf.AUTH)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["pic.png"])

    def test_login_page_then_success_after_refresh(self) -> None:
        responses = [FakeResponse(b"<html>", ctype="text/html"), FakeResponse(PNG)]
        path, opened, tokens = self._download(self._info(), responses)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(tokens.call_args_list[1].kwargs, {"rejected_token": "tok-1"})
        self.assertEqual(opened.call_args.args[0].headers["Authorization"], "Bearer tok-2")

    def test_login_page_is_detected_whatever_the_header_case(self) -> None:
        html = [FakeResponse(b"<html>", ctype="Text/HTML; Charset=UTF-8") for _ in range(2)]
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._download(self._info(), html)
        self.assertEqual(caught.exception.code, self.sf.AUTH)

    def test_an_error_page_of_another_type_is_not_saved_as_the_image(self) -> None:
        with self.assertRaises(self.sf.SlackFileError) as caught:
            self._download(self._info(), [FakeResponse(b'{"ok":false}', ctype="application/json")])
        self.assertEqual(caught.exception.code, self.sf.ERROR)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["pic.png"])

    def test_generic_binary_and_matching_types_are_accepted(self) -> None:
        for ctype in ("application/octet-stream", "IMAGE/PNG"):
            path, _, _ = self._download(self._info(), [FakeResponse(PNG, ctype=ctype)])
            self.assertEqual(path.read_bytes(), PNG)
            path.unlink()

    def test_stream_over_the_cap_is_aborted_and_removed(self) -> None:
        with self.assertRaises(self.sf.SlackFileError):
            self._download(self._info(), [FakeResponse(b"x" * 5000)], max_bytes=100)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["pic.png"])

    def test_redirect_off_slack_is_refused(self) -> None:
        handler = self.sf._SlackOnlyRedirects()
        with self.assertRaises(self.sf.SlackFileError):
            handler.redirect_request(MagicMock(), None, 302, "Found", {},
                                     "https://attacker.example/steal")

    def test_slack_host_matching(self) -> None:
        self.assertTrue(self.sf.is_slack_host("https://files.slack.com/x"))
        self.assertTrue(self.sf.is_slack_host("https://a.slack-edge.com/x"))
        self.assertFalse(self.sf.is_slack_host("http://files.slack.com/x"))
        self.assertFalse(self.sf.is_slack_host("https://slack.com.evil.example/x"))
        self.assertFalse(self.sf.is_slack_host("https://evilslack.com/x"))

    def test_skip_reasons(self) -> None:
        self.assertIsNone(self.sf.skip_reason(self._info()))
        self.assertIn("not a Slack-hosted", self.sf.skip_reason(self._info(is_external=True)))
        self.assertIn("not allowed", self.sf.skip_reason(self._info(mimetype="text/plain")))
        self.assertIsNone(self.sf.skip_reason(self._info(mimetype="text/plain"), types=()))
        self.assertIn("exceeds", self.sf.skip_reason(self._info(size=10**9)))
        self.assertIn("no downloadable", self.sf.skip_reason(
            self._info(url_private_download=None, url_private=None)))
        self.assertIsNone(self.sf.skip_reason(self._info(mimetype="application/pdf")))

    def test_local_name_ignores_slack_path_text(self) -> None:
        self.assertEqual(self.sf.safe_name({"id": "F1", "name": "../../etc/passwd"}), "F1")
        self.assertEqual(self.sf.safe_name({"id": "F/1", "name": "a.JPEG"}), "F1.JPEG")

    def test_message_files_reads_a_thread_reply_and_falls_back_to_history(self) -> None:
        reply = FakeSlack({"conversations.replies": (200, {"ok": True, "messages": [
            {"ts": "1.0"}, {"ts": "2.0", "files": [{"id": "F1"}]}]})})
        with patch.object(self.sf.client, "slack_request", reply):
            self.assertEqual(self.sf.message_files("C1", "2.0"), [{"id": "F1"}])
        top = FakeSlack({
            "conversations.replies": (200, {"ok": False, "error": "thread_not_found"}),
            "conversations.history": (200, {"ok": True, "messages": [
                {"ts": "3.0", "files": [{"id": "F2"}]}]}),
        })
        with patch.object(self.sf.client, "slack_request", top):
            self.assertEqual(self.sf.message_files("C1", "3.0"), [{"id": "F2"}])


class SendPolicyTest(TransportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.dir / "ws"
        (self.root / "state" / "quests" / "active").mkdir(parents=True)

    def _quest(self, allow_send: bool) -> str:
        quest = self.root / "state" / "quests" / "active" / "q1"
        quest.mkdir()
        (quest / "meta.json").write_text(json.dumps({"id": "q1", "allow_send": allow_send}))
        (quest / "timeline.ndjson").touch()
        return "q1"

    def _payload(self, **overrides) -> dict:
        payload = {"quest_id": "q1", "channel_id": "C123", "file": str(self.file),
                   "initial_comment": "here is the chart"}
        payload.update(overrides)
        return payload

    def _approval(self, payload: dict, **overrides) -> dict:
        job = self.mod._prepare(payload)
        item = {"id": "appr-1", "quest_id": "q1", "status": "executing",
                "lease_expires_at": "2999-01-01T00:00:00+00:00",
                **self.mod.approval_spec(job)}
        item.update(overrides)
        return item

    def _run(self, command: str, payload: dict, *, target: str | None = "q1",
             approvals: list | None = None, stale: str | None = None,
             upload_result=None, upload_error=None, queue=None):
        upload = MagicMock(return_value=upload_result or {
            "file_id": "F1", "response_ts": "9.000001", "permalink": "https://x/F1"})
        if upload_error is not None:
            upload.side_effect = upload_error
        env = {} if target is None else {"SIDEQUESTOR_DISPATCH_TARGET": target}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod.slack_files, "upload", upload), \
             patch.object(self.mod, "_stale_reason", return_value=stale), \
             patch.object(self.mod, "_queue_for_review",
                          queue or MagicMock(return_value="appr-held")), \
             patch.object(self.mod.approval_store, "read_queue",
                          return_value={"items": approvals or []}), \
             patch.dict(os.environ, env, clear=True), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.mod.main([command, json.dumps(payload)])
        return code, stdout.getvalue(), stderr.getvalue(), upload

    def _timeline(self) -> list[dict]:
        text = (self.root / "state" / "quests" / "active" / "q1" / "timeline.ndjson").read_text()
        return [json.loads(line) for line in text.splitlines() if line]

    def test_allow_send_false_denies_before_any_upload(self) -> None:
        self._quest(allow_send=False)
        code, _, error, upload = self._run("send", self._payload())
        self.assertEqual(code, 1)
        self.assertIn("approval-spec", error)
        upload.assert_not_called()

    def test_allow_send_true_uploads_and_logs_the_comment_and_digest(self) -> None:
        self._quest(allow_send=True)
        code, output, error, upload = self._run("send", self._payload())
        self.assertEqual(code, 0, error)
        self.assertEqual(upload.call_args.kwargs["initial_comment"], "here is the chart")
        self.assertEqual(len(upload.call_args.kwargs["sha256"]), 64)
        self.assertEqual(json.loads(output)["response_ts"], "9.000001")
        [event] = self._timeline()
        self.assertEqual(event["event"], "message_sent")
        self.assertEqual(event["message_text"], "here is the chart")
        self.assertEqual(event["files"][0]["file_id"], "F1")
        self.assertEqual(len(event["files"][0]["sha256"]), 64)

    def test_matching_claimed_approval_authorizes(self) -> None:
        self._quest(allow_send=False)
        payload = self._payload(approval_id="appr-1")
        code, _, error, upload = self._run("send", payload, approvals=[self._approval(payload)])
        self.assertEqual(code, 0, error)
        upload.assert_called_once()

    def test_approval_for_different_bytes_does_not_authorize(self) -> None:
        self._quest(allow_send=False)
        payload = self._payload(approval_id="appr-1")
        approval = self._approval(payload)
        self.file.write_bytes(PNG + b"tampered")
        code, _, _, upload = self._run("send", payload, approvals=[approval])
        self.assertEqual(code, 1)
        upload.assert_not_called()

    def test_approval_for_a_different_comment_or_thread_does_not_authorize(self) -> None:
        self._quest(allow_send=False)
        payload = self._payload(approval_id="appr-1")
        for approval in (self._approval(payload, message_text="other words"),
                         self._approval(self._payload(thread_ts="1.000001"))):
            code, _, _, upload = self._run("send", payload, approvals=[approval])
            self.assertEqual(code, 1)
            upload.assert_not_called()

    def test_approval_for_a_different_title_does_not_authorize(self) -> None:
        self._quest(allow_send=False)
        reviewed = self._payload(approval_id="appr-1", title="Q3 chart")
        code, _, _, upload = self._run("send", self._payload(approval_id="appr-1",
                                                             title="Something else"),
                                       approvals=[self._approval(reviewed)])
        self.assertEqual(code, 1)
        upload.assert_not_called()

    def test_expired_or_slack_message_approval_does_not_authorize(self) -> None:
        self._quest(allow_send=False)
        payload = self._payload(approval_id="appr-1")
        for approval in (self._approval(payload, lease_expires_at="2000-01-01T00:00:00+00:00"),
                         self._approval(payload, action_type="slack_message")):
            code, _, _, upload = self._run("send", payload, approvals=[approval])
            self.assertEqual(code, 1)
            upload.assert_not_called()

    def test_dispatch_target_must_match_the_quest(self) -> None:
        self._quest(allow_send=True)
        code, _, error, upload = self._run("send", self._payload(), target="other-quest")
        self.assertEqual(code, 1)
        self.assertIn("does not match dispatch target", error)
        upload.assert_not_called()

    def test_reactions_dispatch_may_upload_without_a_quest(self) -> None:
        payload = self._payload()
        del payload["quest_id"]
        code, _, error, upload = self._run("send", payload, target="reactions")
        self.assertEqual(code, 0, error)
        upload.assert_called_once()

    def test_explicit_interactive_upload_needs_no_quest(self) -> None:
        payload = self._payload(interactive=True)
        del payload["quest_id"]
        code, output, error, upload = self._run("send", payload, target=None)
        self.assertEqual(code, 0, error)
        upload.assert_called_once()
        self.assertFalse(json.loads(output)["logged"])

    def test_unscoped_upload_without_interactive_flag_is_denied(self) -> None:
        payload = self._payload()
        del payload["quest_id"]
        code, _, error, upload = self._run("send", payload, target=None)
        self.assertEqual(code, 1)
        self.assertIn("interactive is true", error)
        upload.assert_not_called()

    def test_interactive_upload_is_denied_in_dispatch_or_with_quest(self) -> None:
        payload = self._payload(interactive=True)
        unscoped = dict(payload)
        del unscoped["quest_id"]
        for request, target in ((unscoped, "reactions"), (unscoped, "q1"),
                                (payload, None)):
            code, _, _, upload = self._run("send", request, target=target)
            self.assertEqual(code, 1)
            upload.assert_not_called()

    def test_interactive_stale_thread_is_denied_without_review_queue(self) -> None:
        payload = self._payload(interactive=True, thread_ts="1.000001")
        del payload["quest_id"]
        queue = MagicMock()
        code, _, error, upload = self._run("send", payload, target=None,
                                           stale="thread is stale", queue=queue)
        self.assertEqual(code, 1)
        self.assertIn("thread is stale", error)
        upload.assert_not_called()
        queue.assert_not_called()

    def test_member_id_destination_is_refused(self) -> None:
        self._quest(allow_send=True)
        code, _, _, upload = self._run("send", self._payload(channel_id="U123"))
        self.assertEqual(code, 1)
        upload.assert_not_called()

    def test_stale_thread_is_held_for_review_not_uploaded(self) -> None:
        self._quest(allow_send=True)
        code, output, _, upload = self._run("send", self._payload(thread_ts="1.000001"),
                                            stale="thread is stale")
        self.assertEqual(code, 0)
        upload.assert_not_called()
        self.assertTrue(json.loads(output)["held"])
        [event] = self._timeline()
        self.assertEqual(event["event"], "draft_posted")
        self.assertEqual(event["approval_id"], "appr-held")

    def test_a_failed_timeline_write_still_reports_the_posted_upload(self) -> None:
        self._quest(allow_send=True)
        with patch.object(self.mod, "append_timeline", side_effect=OSError("disk full")):
            code, output, error, _ = self._run("send", self._payload())
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["file_id"], "F1")
        self.assertFalse(result["logged"])
        self.assertIn("disk full", result["log_error"])
        self.assertIn("do not retry", error)

    def test_a_hold_that_cannot_be_queued_fails_without_uploading(self) -> None:
        self._quest(allow_send=True)
        code, output, error, upload = self._run(
            "send", self._payload(thread_ts="1.000001"), stale="thread is stale",
            queue=MagicMock(side_effect=self.mod.Denied("helper exited 2")))
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        upload.assert_not_called()

    def test_queue_reports_the_existing_item_when_the_helper_dedupes(self) -> None:
        self._quest(allow_send=True)
        payload = self._payload(thread_ts="1.000001")
        job = self.mod._prepare(payload)
        pending = {"id": "appr-existing", "quest_id": "q1", "status": "pending_review",
                   "target": self.mod.approval_target(job), "message_text": job["comment"]}
        done = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(self.mod.subprocess, "run", return_value=done), \
             patch.object(self.mod.approval_store, "read_queue",
                          return_value={"items": [pending]}):
            self.assertEqual(self.mod._queue_for_review(job, "stale"), "appr-existing")
        failed = MagicMock(returncode=2, stdout="", stderr="quest not active")
        with patch.object(self.mod.subprocess, "run", return_value=failed):
            with self.assertRaises(self.mod.Denied):
                self.mod._queue_for_review(job, "stale")
        with patch.object(self.mod.subprocess, "run", return_value=done), \
             patch.object(self.mod.approval_store, "read_queue", return_value={"items": []}):
            with self.assertRaises(self.mod.Denied):
                self.mod._queue_for_review(job, "stale")

    def test_outcome_unknown_exits_3_and_logs_nothing(self) -> None:
        self._quest(allow_send=True)
        error = self.mod.slack_files.SlackFileError(
            "unknown", self.mod.slack_files.OUTCOME_UNKNOWN, "F1")
        code, _, _, _ = self._run("send", self._payload(), upload_error=error)
        self.assertEqual(code, 3)
        self.assertEqual(self._timeline(), [])

    def test_transient_failure_exits_4(self) -> None:
        self._quest(allow_send=True)
        error = self.mod.slack_files.SlackFileError("429", self.mod.slack_files.TRANSIENT)
        code, _, _, _ = self._run("send", self._payload(), upload_error=error)
        self.assertEqual(code, 4)

    def test_approval_spec_pins_the_file(self) -> None:
        code, output, error, _ = self._run("approval-spec", self._payload(thread_ts="1.0"))
        self.assertEqual(code, 0, error)
        spec = json.loads(output)
        self.assertEqual(spec["action_type"], "remote_request")
        self.assertEqual(spec["message_text"], "here is the chart")
        target = spec["target"]
        self.assertEqual((target["surface"], target["action"]), ("slack", "file_upload"))
        self.assertEqual(target["size"], len(PNG))
        self.assertEqual(target["thread_ts"], "1.0")
        self.assertEqual(len(target["sha256"]), 64)


class ApprovalDedupTest(unittest.TestCase):
    """approval-helper.py dedups by thread, but a file upload only matches the same upload."""

    HELPER = PACKAGE_ROOT / "src" / "sidequestor" / "runtime" / "yaas-triage" / "ledger" / "approval-helper.py"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-approval-dedup-")
        self.root = Path(self.temp.name)
        quest = self.root / "state" / "quests" / "active" / "q1"
        quest.mkdir(parents=True)
        (quest / "meta.json").write_text(json.dumps({"id": "q1", "status": "active"}))
        (quest / "watch.json").write_text(json.dumps({"watches": []}))
        (quest / "timeline.ndjson").touch()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, action_type: str, target: dict, message_text: str = "x") -> str:
        payload = {"quest_id": "q1", "action_type": action_type, "target": target,
                   "message_text": message_text, "context": "c", "risk_reason": "r"}
        env = {**os.environ, "SIDEQUESTOR_WORKSPACE": str(self.root)}
        result = subprocess.run([sys.executable, str(self.HELPER), "write", json.dumps(payload)],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_file_uploads_dedup_only_on_the_identical_upload(self) -> None:
        thread = {"channel_id": "C1", "thread_ts": "1.0"}
        upload = {"surface": "slack", "action": "file_upload", **thread, "sha256": "a" * 64}
        self.assertTrue(self._write("remote_request", upload))
        self.assertEqual(self._write("remote_request", upload), "")
        self.assertTrue(self._write("remote_request", upload, message_text="different comment"))
        self.assertTrue(self._write("remote_request", {**upload, "sha256": "b" * 64}))
        self.assertTrue(self._write("slack_message", thread))
        self.assertEqual(self._write("slack_message", thread), "")


class FetchCommandTest(TransportTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.dir / "ws"
        (self.root / "state").mkdir(parents=True)

    def _run(self, payload: dict, files: list, download=None):
        download = download or MagicMock(side_effect=lambda info, out, cap: out / "F1.png")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(self.mod, "REPO_ROOT", self.root), \
             patch.object(self.mod.slack_files, "message_files", MagicMock(return_value=files)), \
             patch.object(self.mod.slack_files, "download", download), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.mod.main(["fetch", json.dumps(payload)])
        return code, stdout.getvalue(), stderr.getvalue(), download

    def test_refuses_an_out_dir_inside_state(self) -> None:
        code, _, error, download = self._run(
            {"channel_id": "C1", "ts": "1.0", "out_dir": str(self.root / "state" / "x")}, [])
        self.assertEqual(code, 1)
        self.assertIn("state/", error)
        download.assert_not_called()

    def test_fetches_images_and_reports_skips(self) -> None:
        files = [
            {"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 10,
             "url_private_download": DOWNLOAD_URL},
            {"id": "F2", "name": "b.txt", "mimetype": "text/plain", "size": 10,
             "url_private_download": DOWNLOAD_URL},
        ]
        out = self.dir / "out"
        code, output, error, download = self._run(
            {"channel_id": "C1", "ts": "1.0", "out_dir": str(out)}, files)
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual([f["file_id"] for f in result["files"]], ["F1"])
        self.assertEqual([f["file_id"] for f in result["skipped"]], ["F2"])
        download.assert_called_once()

    def test_a_failed_download_exits_2(self) -> None:
        files = [{"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 10,
                  "url_private_download": DOWNLOAD_URL}]
        failing = MagicMock(side_effect=self.mod.slack_files.SlackFileError("boom"))
        code, output, _, _ = self._run({"channel_id": "C1", "ts": "1.0",
                                        "out_dir": str(self.dir / "out")}, files, failing)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["skipped"][0]["reason"], "boom")

    def test_needs_a_message_or_a_file_id(self) -> None:
        code, _, _, _ = self._run({"channel_id": "C1"}, [])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
