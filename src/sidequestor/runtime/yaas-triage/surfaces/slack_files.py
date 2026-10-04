# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
slack_files.py — Slack file transport: upload a file into a conversation, and fetch the
files attached to a message. No policy lives here; slack-file.py owns authorization,
review holds and timeline logging, and is the only sanctioned caller.

Why not the Slack MCP: it has no upload tool, and slack_read_file does not hand back
image bytes. Both directions go to the Slack Web API with the same rotating user token
client.py already uses, so the refresh-and-retry-once-on-rejection behaviour is shared.

Upload is Slack's three-step external flow, verified live 2026-10-01:
  1. files.getUploadURLExternal(filename, length)   -> upload_url, file_id
  2. POST the raw bytes to upload_url                (pre-signed: NO bearer token)
  3. files.completeUploadExternal(files, channel_id, thread_ts?, initial_comment?)

Step 3 may only be called once per file and is what makes the file visible. A transport
failure there leaves the outcome unknown, so it is never retried blindly: the file is
looked up by id first (`reconcile_share`), and if that cannot settle it the caller is
told OUTCOME_UNKNOWN. A reservation that never completes is discarded by Slack (observed
as `file_deleted` from files.info), so failures in steps 1-2 are safe to retry.

Download reads `files[]` off the message and GETs `url_private_download` (falling back to
`url_private`) with the bearer token. Slack answers an unauthenticated or wrongly
authenticated file GET with HTTP 200 and its HTML login page, not a 401, so an HTML body
for a non-HTML file is treated as an auth rejection. The token is only ever sent to
Slack-owned HTTPS hosts, including across redirects.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import client
from client import AUTH, BAD_ARGS, ERROR, OK, TRANSIENT  # noqa: F401  (re-exported)

# Not in client.py's taxonomy because only a non-idempotent write can produce it.
OUTCOME_UNKNOWN = 5

CHUNK = 64 * 1024
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
# Slack's own ceiling for a single uploaded file.
UPLOAD_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_TYPES = ("image/", "application/pdf")
SHARE_POLL_SECONDS = 5

# Files are served from files.slack.com; enterprise workspaces and CDN redirects stay
# under these suffixes. Anything else never sees the token.
SLACK_FILE_HOSTS = ("slack.com", "slack-edge.com")


class SlackFileError(Exception):
    """A failed Slack file operation. `code` uses client.py's taxonomy plus OUTCOME_UNKNOWN."""

    def __init__(self, message, code=ERROR, file_id=""):
        super().__init__(message)
        self.code = code
        self.file_id = file_id


def is_slack_host(url):
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in SLACK_FILE_HOSTS)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# ── Web API ───────────────────────────────────────────────────────────────────

def web_api(method, form=None, json_body=None):
    """Call one Slack Web API method; return its payload or raise SlackFileError."""
    if json_body is not None:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        body = json.dumps(json_body)
    else:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        body = urllib.parse.urlencode(form or {})
    try:
        status, text = client.slack_request(f"{client.SLACK_WEB_URL}/{method}", "POST",
                                            headers, body)
    except client.SlackCredentialError as exc:
        raise SlackFileError(str(exc), client.classify_credential_exception(exc)) from exc
    except Exception as exc:
        raise SlackFileError(f"slack {method}: {type(exc).__name__}: {exc}",
                             client.classify_exception(exc)) from exc
    verdict = client.classify_status(status)
    if verdict == OK:
        verdict = client.classify_body(text)
    if verdict != OK:
        raise SlackFileError(f"slack {method}: HTTP {status}: {str(text)[:200]}", verdict)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise SlackFileError(f"slack {method}: unexpected response: {str(text)[:200]}", ERROR)
    return data


# ── upload ────────────────────────────────────────────────────────────────────

def _open(request, timeout=client.TIMEOUT):
    """The single network seam for non-Web-API requests (upload bytes, file GETs)."""
    return _OPENER.open(request, timeout=timeout)


def put_bytes(upload_url, path, length, sha256=None):
    """Step 2. The URL is pre-signed, so the bearer token is deliberately not attached.

    The bytes sent are the bytes read here, so they are re-hashed against the digest the
    caller authorized: a file swapped after approval (even at the same size) is refused.
    """
    if not is_slack_host(upload_url):
        raise SlackFileError(f"refusing upload to non-Slack URL host: {upload_url[:80]}", ERROR)
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) != length or (sha256 and hashlib.sha256(data).hexdigest() != sha256):
        raise SlackFileError("file changed between authorization and upload", ERROR)
    request = urllib.request.Request(upload_url, data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
    try:
        with _open(request) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        raise SlackFileError(f"upload bytes: {type(exc).__name__}: {exc}",
                             client.classify_exception(exc)) from exc
    verdict = client.classify_status(status)
    if verdict != OK:
        raise SlackFileError(f"upload bytes: HTTP {status}", verdict)


def share_ts(info, channel_id):
    """The ts of this file's share message in channel_id, from a files.info file object."""
    shares = info.get("shares") if isinstance(info, dict) else None
    if not isinstance(shares, dict):
        return ""
    for bucket in ("public", "private"):
        entries = shares.get(bucket)
        entries = entries.get(channel_id) if isinstance(entries, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("ts"), str):
                    return entry["ts"]
    return ""


def file_info(file_id):
    return web_api("files.info", {"file": file_id}).get("file") or {}


def reconcile_share(file_id, channel_id, seconds=SHARE_POLL_SECONDS, sleep=time.sleep):
    """Poll files.info until the share shows up. Returns (ts, file) or ("", file)."""
    info = {}
    for attempt in range(max(1, int(seconds))):
        info = file_info(file_id)
        ts = share_ts(info, channel_id)
        if ts:
            return ts, info
        if attempt + 1 < seconds:
            sleep(1)
    return "", info


def upload(path, channel_id, *, filename=None, title=None, thread_ts=None,
           initial_comment=None, sha256=None, sleep=time.sleep):
    """Upload `path` into `channel_id`. Returns {file_id, response_ts, permalink}.

    response_ts is the share message's ts, read back from files.info because
    completeUploadExternal does not return it. It can be "" if Slack has not finished
    sharing within SHARE_POLL_SECONDS; the file is posted either way.
    """
    path = Path(path)
    length = path.stat().st_size
    filename = filename or path.name

    ticket = web_api("files.getUploadURLExternal",
                     {"filename": filename, "length": str(length)})
    upload_url, file_id = ticket.get("upload_url"), ticket.get("file_id")
    if not isinstance(upload_url, str) or not isinstance(file_id, str) or not file_id:
        raise SlackFileError(f"files.getUploadURLExternal: malformed ticket: {ticket}", ERROR)

    put_bytes(upload_url, path, length, sha256)

    body = {"files": [{"id": file_id, "title": title or filename}], "channel_id": channel_id}
    if thread_ts:
        body["thread_ts"] = thread_ts
    if initial_comment:
        body["initial_comment"] = initial_comment
    try:
        status, text = client.slack_request(
            f"{client.SLACK_WEB_URL}/files.completeUploadExternal", "POST",
            {"Content-Type": "application/json; charset=utf-8"}, json.dumps(body))
    except client.SlackCredentialError as exc:
        # The token was rejected before Slack acted, so nothing was shared.
        raise SlackFileError(str(exc), client.classify_credential_exception(exc),
                             file_id) from exc
    except Exception as exc:
        status, text = 0, f"{type(exc).__name__}: {exc}"

    verdict = client.classify_status(status)
    if status == 0 or status >= 500:
        # The request may have been processed. Look before anyone retries.
        return _settle_unknown(file_id, channel_id, f"HTTP {status}: {text[:200]}", sleep)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    payload = payload if isinstance(payload, dict) else None
    if (payload is not None and payload.get("ok") is False
            and payload.get("error") in ("internal_error", "fatal_error")):
        # Slack documents partial success for these errors, even with HTTP 200.
        # Reconcile the existing file instead of inviting a duplicate upload.
        return _settle_unknown(file_id, channel_id, str(text)[:200], sleep)
    if verdict == OK and payload is not None and payload.get("ok") is False:
        verdict = client.classify_body(text)
    if verdict != OK:
        # A 4xx/429 or a definitive API rejection means Slack did not share the file.
        raise SlackFileError(f"files.completeUploadExternal: {str(text)[:200]}", verdict, file_id)
    if payload is None or payload.get("ok") is not True:
        # Only an explicit ok:true proves the share happened. An empty or garbled 2xx
        # settles only if the share is visible.
        return _settle_unknown(file_id, channel_id,
                               f"HTTP {status} without ok:true: {str(text)[:200]}", sleep)

    try:
        ts, info = reconcile_share(file_id, channel_id, sleep=sleep)
    except SlackFileError:
        ts, info = "", {}
    return {"file_id": file_id, "response_ts": ts, "permalink": info.get("permalink", "")}


def _settle_unknown(file_id, channel_id, detail, sleep):
    """Return the posted result only if the share is visible; otherwise OUTCOME_UNKNOWN.

    A lookup error, including `file_deleted`, is not proof that nothing was posted: the
    share may have existed briefly, or the lookup may lag. Reporting "nothing posted"
    there would invite a retry that duplicates the file and its comment.
    """
    try:
        ts, info = reconcile_share(file_id, channel_id, sleep=sleep)
    except SlackFileError:
        ts, info = "", {}
    if ts:
        return {"file_id": file_id, "response_ts": ts, "permalink": info.get("permalink", "")}
    raise SlackFileError(
        f"files.completeUploadExternal outcome unknown ({detail}); check file {file_id} "
        f"in Slack before retrying", OUTCOME_UNKNOWN, file_id)


# ── download ──────────────────────────────────────────────────────────────────

class _SlackOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a Slack-owned HTTPS host, so the token never leaks."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_slack_host(newurl):
            raise SlackFileError(f"refusing redirect to non-Slack host: {newurl[:80]}", ERROR)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SlackOnlyRedirects)


def message_files(channel_id, ts):
    """The files[] of the message at (channel_id, ts), top-level or a thread reply."""
    window = {"channel": channel_id, "ts": ts, "oldest": ts, "latest": ts,
              "inclusive": "true", "limit": "2"}
    found = None
    try:
        for message in web_api("conversations.replies", window).get("messages") or []:
            if isinstance(message, dict) and message.get("ts") == ts:
                found = message
                break
    except SlackFileError as exc:
        if "thread_not_found" not in str(exc):
            raise
    if found is None:
        history = web_api("conversations.history", {
            "channel": channel_id, "oldest": ts, "latest": ts, "inclusive": "true",
            "limit": "1"})
        for message in history.get("messages") or []:
            if isinstance(message, dict) and message.get("ts") == ts:
                found = message
                break
    if found is None:
        raise SlackFileError(f"no message {ts} in {channel_id}", ERROR)
    return [f for f in found.get("files") or [] if isinstance(f, dict)]


def skip_reason(info, max_bytes=DEFAULT_MAX_BYTES, types=DEFAULT_TYPES):
    """Why this file will not be fetched, or None to fetch it."""
    if info.get("is_external") or info.get("mode") in ("external", "tombstone",
                                                       "hidden_by_limit"):
        return f"not a Slack-hosted file (mode {info.get('mode')!r})"
    url = info.get("url_private_download") or info.get("url_private")
    if not url:
        return "no downloadable URL"
    if not is_slack_host(url):
        return "download URL is not a Slack host"
    mimetype = str(info.get("mimetype") or "")
    if types and not any(mimetype.startswith(t) if t.endswith("/") else mimetype == t
                         for t in types):
        return f"type {mimetype or 'unknown'} not allowed"
    size = info.get("size")
    if isinstance(size, int) and size > max_bytes:
        return f"{size} bytes exceeds the {max_bytes}-byte limit"
    return None


def safe_name(info):
    """A local filename that never comes from Slack-controlled path text."""
    file_id = re.sub(r"[^A-Za-z0-9]", "", str(info.get("id") or "")) or "file"
    suffix = Path(str(info.get("name") or "")).suffix
    return file_id + (suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix) else "")


def download(info, out_dir, max_bytes=DEFAULT_MAX_BYTES):
    """Stream one file into out_dir. Returns the path written; leaves nothing on failure."""
    url = info.get("url_private_download") or info.get("url_private")
    if not is_slack_host(url):
        raise SlackFileError("download URL is not a Slack host", ERROR)
    expected = _media_type(info.get("mimetype"))
    token = _token()
    for attempt in (0, 1):
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            path = _stream(request, Path(out_dir), safe_name(info), max_bytes, expected)
        except _LoginPage:
            if attempt:
                raise SlackFileError("file download returned Slack's login page; the token "
                                     "lacks access to this file", AUTH)
            token = _token(rejected=token)
            continue
        return path


class _LoginPage(Exception):
    pass


def _token(rejected=None):
    try:
        return client.get_slack_access_token(rejected_token=rejected) if rejected \
            else client.get_slack_access_token()
    except client.SlackCredentialError as exc:
        raise SlackFileError(str(exc), client.classify_credential_exception(exc)) from exc


def _media_type(value):
    """`Text/HTML; charset=utf-8` -> `text/html`."""
    return str(value or "").split(";", 1)[0].strip().lower()


# Generic binary types a file server may use for any file; they carry no mismatch signal.
GENERIC_TYPES = ("application/octet-stream", "binary/octet-stream")


def check_content_type(received, expected):
    """Raise if the response is not the file. Slack serves files with their own mimetype
    (verified live), so anything else is an error page saved under an image's name."""
    received = _media_type(received)
    if received == "text/html" and expected != "text/html":
        raise _LoginPage()
    if not expected or received == expected or received in GENERIC_TYPES:
        return
    raise SlackFileError(f"file download returned {received or 'no content type'}, "
                         f"expected {expected}", ERROR)


def _stream(request, out_dir, name, max_bytes, expected):
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".partial-")
    try:
        with os.fdopen(fd, "wb") as sink:
            try:
                response = _open(request)
            except urllib.error.HTTPError as exc:
                raise SlackFileError(f"file download: HTTP {exc.code}",
                                     client.classify_status(exc.code)) from exc
            except SlackFileError:
                raise
            except Exception as exc:
                raise SlackFileError(f"file download: {type(exc).__name__}: {exc}",
                                     client.classify_exception(exc)) from exc
            with response:
                check_content_type(response.headers.get("Content-Type", ""), expected)
                total = 0
                for block in iter(lambda: response.read(CHUNK), b""):
                    total += len(block)
                    if total > max_bytes:
                        raise SlackFileError(f"file exceeds the {max_bytes}-byte limit", ERROR)
                    sink.write(block)
        os.chmod(tmp, 0o600)
        final = out_dir / name
        os.replace(tmp, final)
        return final
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
