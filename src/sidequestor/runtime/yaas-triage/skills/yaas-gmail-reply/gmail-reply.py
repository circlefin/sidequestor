#!/usr/bin/python3
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

"""Send an explicit sender-only or reply-all Gmail reply via native gws helpers."""

import argparse
import json
import os
import subprocess
import sys
from email.utils import parseaddr

import gmail_policy


GWS = os.environ.get("GWS_BIN", "gws")


class GwsFailure(Exception):
    def __init__(self, returncode, message):
        super().__init__(message)
        self.returncode = returncode


def _run_gws(arguments, timeout, operation):
    try:
        result = subprocess.run(
            [GWS, *arguments], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise GwsFailure(1, f"ERROR: gws executable not found: {exc.filename}") from exc
    except OSError as exc:
        raise GwsFailure(1, f"ERROR: could not run gws: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        outcome = " Delivery outcome may be unknown; do not retry blindly." if operation == "reply" else ""
        raise GwsFailure(
            1, f"ERROR: gws {operation} timed out after {timeout} seconds.{outcome}"
        ) from exc

    if result.returncode != 0:
        details = []
        if result.stdout.strip():
            details.append(result.stdout.strip())
        if result.stderr.strip():
            details.append(result.stderr.strip())
        message = "\n".join(details) or f"gws {operation} failed with exit {result.returncode}"
        raise GwsFailure(result.returncode, message)
    return result.stdout


def _json_object(output):
    for index, character in enumerate(output):
        if character != "{":
            continue
        try:
            value = json.loads(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise GwsFailure(1, "ERROR: gws returned no JSON object")


def _sent_id(stdout):
    try:
        response = _json_object(stdout)
    except GwsFailure:
        return stdout.strip()
    message = response.get("message")
    nested_id = message.get("id") if isinstance(message, dict) else None
    return response.get("id") or nested_id or stdout.strip()


def _gws_mailbox(raw):
    """Match gws 0.22.5 Mailbox::parse for audience comparisons."""
    raw = raw.strip()
    start = raw.rfind("<")
    if start >= 0:
        end = raw.find(">", start)
        if end >= 0:
            raw = raw[start + 1:end].strip()
    return "".join(character for character in raw if ord(character) >= 32 and ord(character) != 127)


def _gws_mailbox_tokens(raw):
    """Split like gws: commas outside quoted strings delimit addresses."""
    start = 0
    in_quotes = False
    previous_backslash = False
    for index, character in enumerate(raw):
        if character == "\\" and in_quotes:
            previous_backslash = not previous_backslash
            continue
        if character == '"' and not previous_backslash:
            in_quotes = not in_quotes
        elif character == "," and not in_quotes:
            token = raw[start:index].strip()
            if token:
                yield token
            start = index + 1
        previous_backslash = False
    token = raw[start:].strip()
    if token:
        yield token


def _mailboxes(values):
    addresses = []
    seen = set()
    for value in values:
        for token in _gws_mailbox_tokens(value):
            address = _gws_mailbox(token)
            key = address.lower()
            if address and key not in seen:
                seen.add(key)
                addresses.append(address)
    return addresses


def _ascii_lower(value):
    return value.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def _reply_all_cc_workaround(message_id):
    # WORKAROUND(gws 0.22.5, googleworkspace/cli#911 and #642): +reply-all
    # matches header names case-sensitively and silently drops original CC/cc
    # headers. Inject only those noncanonical CC values through --cc. Remove
    # this metadata read after the minimum supported GWS version contains the
    # upstream case-insensitive header fix.
    output = _run_gws(
        [
            "gmail", "users", "messages", "get", "--params",
            json.dumps({"userId": "me", "id": message_id, "format": "metadata"}),
        ],
        timeout=60,
        operation="reply-all metadata read",
    )
    message = _json_object(output)
    headers = message.get("payload", {}).get("headers", [])
    values = [
        header.get("value", "")
        for header in headers
        if header.get("name", "").casefold() == "cc" and header.get("name") != "Cc"
    ]
    return _mailboxes(values)


def _reviewed_recipients(message_id, reply_all, added_cc, removed):
    """Resolve the audience for a review from the same Gmail message used at send time.

    The approval is checked again immediately before a reviewed send, so an edited original
    message or a changed sending identity cannot silently change its audience.
    """
    output = _run_gws(
        ["gmail", "users", "messages", "get", "--params",
         json.dumps({"userId": "me", "id": message_id, "format": "metadata"})],
        timeout=60, operation="reply audience read",
    )
    headers = _json_object(output).get("payload", {}).get("headers", [])
    if not isinstance(headers, list):
        raise GwsFailure(1, "ERROR: Gmail message has no readable recipient headers")

    def header_values(name):
        return [value for header in headers if isinstance(header, dict)
                and header.get("name") == name
                and (value := header.get("value", ""))]

    def addresses(name):
        # gws joins repeated address headers before parsing. Parsing each one separately
        # can hide recipients when quotes span two header values.
        values = header_values(name)
        return _mailboxes([", ".join(values)]) if values else []

    from_headers = [header.get("value", "") for header in headers
                    if isinstance(header, dict) and header.get("name") == "From"]
    sender = [_gws_mailbox(from_headers[-1])] if from_headers else []
    if not sender or not sender[0]:
        raise GwsFailure(1, "ERROR: Gmail message has no From header")
    reply_to = addresses("Reply-To") if header_values("Reply-To") else sender
    if not reply_to:
        raise GwsFailure(1, "ERROR: Gmail Reply-To has no usable recipient")
    original_to = addresses("To")
    if not reply_all:
        return {"to": reply_to, "cc": []}, None

    # gws 0.22.5 only recognizes canonical Cc; the existing workaround passes other
    # spellings through --cc. Include those addresses in the reviewed audience too.
    noncanonical_cc = _mailboxes([header.get("value", "") for header in headers
                                  if isinstance(header, dict)
                                  and str(header.get("name", "")).lower() == "cc"
                                  and header.get("name") != "Cc"])
    # These addresses are fed back to gws as one --cc= value. Quotes or separators
    # would make that second parse differ from the review, so reject them for review.
    reparsed = [*noncanonical_cc, *_mailboxes(added_cc), *_mailboxes(removed)]
    if any(any(character in address for character in '\",<>\\')
           or any(character.isspace() for character in address)
           for address in reparsed):
        raise GwsFailure(1, "ERROR: recipient address cannot be reviewed safely")
    original_cc = [*addresses("Cc"), *noncanonical_cc]

    profile = _json_object(_run_gws(
        ["gmail", "users", "getProfile", "--params", '{"userId":"me"}'],
        timeout=60, operation="sender profile read",
    ))
    primary = _mailboxes([profile.get("emailAddress", "")])
    if not primary:
        raise GwsFailure(1, "ERROR: Gmail profile has no emailAddress")
    configured_sender = os.environ.get("SIDEQUESTOR_FROM_EMAIL") or os.environ.get("YAAS_FROM_EMAIL")
    if configured_sender:
        alias = _mailboxes([parseaddr(configured_sender)[1]])
    else:
        send_as = _json_object(_run_gws(
            ["gmail", "users", "settings", "sendAs", "list", "--params", '{"userId":"me"}'],
            timeout=60, operation="send-as identity read",
        ))
        identities = send_as.get("sendAs", [])
        defaults = [identity.get("sendAsEmail", "") for identity in identities
                    if isinstance(identity, dict) and identity.get("isDefault")]
        if len(defaults) != 1:
            raise GwsFailure(1, "ERROR: cannot determine the default Gmail send-as address")
        alias = _mailboxes(defaults)
    if len(alias) != 1:
        raise GwsFailure(1, "ERROR: cannot determine the Gmail sending address")
    own = {address.lower() for address in [*primary, *alias]}
    excluded = own | {address.lower() for address in _mailboxes(removed)}
    self_reply = any(_ascii_lower(sender[0]) == _ascii_lower(address)
                     for address in [*primary, *alias])
    to_candidates = original_to if self_reply else reply_to
    cc_candidates = [*original_cc, *_mailboxes(added_cc)] if self_reply else [
        *original_to, *original_cc, *_mailboxes(added_cc)]

    def unique(candidates, seen):
        audience = []
        for address in candidates:
            key = address.lower()
            if key not in excluded and key not in seen:
                seen.add(key)
                audience.append(address)
        return audience

    seen = set()
    to = unique(to_candidates, seen)
    cc = unique(cc_candidates, seen)
    if not to:
        raise GwsFailure(1, "ERROR: no To recipient remains for this reply-all")
    return {"to": to, "cc": cc}, alias[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("message_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--reply-sender",
        action="store_true",
        help="reply only to the author or Reply-To of the selected message",
    )
    mode.add_argument(
        "--reply-all",
        action="store_true",
        help="reply to the author and all original To/Cc recipients",
    )
    parser.add_argument("--body", default=None)
    parser.add_argument("--cc", action="append", default=[])
    parser.add_argument("--remove", action="append", default=[])
    parser.add_argument("--html", action="store_true")
    parser.add_argument("--attach", action="append", default=[])
    parser.add_argument("--quest-id", default="")
    parser.add_argument("--approval-id", default="")
    delivery = parser.add_mutually_exclusive_group(required=True)
    delivery.add_argument("--send", action="store_true")
    delivery.add_argument("--draft", action="store_true")
    delivery.add_argument("--approval-spec", action="store_true",
                          help="print the approval a reviewer must grant for this exact reply")
    args = parser.parse_args()

    if args.cc and not args.reply_all:
        parser.error("--cc requires --reply-all")
    if args.remove and not args.reply_all:
        parser.error("--remove requires --reply-all")

    reply_body = args.body if args.body is not None else sys.stdin.read().strip()
    if not reply_body:
        parser.error("no reply body provided")

    if args.send or args.approval_spec:
        try:
            attachments = [gmail_policy.attachment_record(path) for path in args.attach]
        except OSError as exc:
            parser.error(f"cannot read attachment: {exc}")
        target = {
            "surface": "gmail", "action": "reply_all" if args.reply_all else "reply",
            "message_id": args.message_id, "cc": _mailboxes(args.cc),
            "remove": _mailboxes(args.remove), "attachments": attachments, "html": args.html,
        }
        if args.approval_spec or args.approval_id:
            target["recipients"], resolved_sender = _reviewed_recipients(
                args.message_id, args.reply_all, args.cc, args.remove)
            if resolved_sender:
                target["sender"] = resolved_sender
        if args.approval_spec:
            print(json.dumps(gmail_policy.approval_spec(target, reply_body), ensure_ascii=False))
            return
        reason = gmail_policy.send_policy_reason(
            args.quest_id, args.approval_id, target, reply_body)
        if reason:
            print(f"ERROR: send denied: {reason}", file=sys.stderr)
            raise SystemExit(1)

    command = [
        "gmail",
        "+reply-all" if args.reply_all else "+reply",
        "--message-id",
        args.message_id,
        # `--flag=value` throughout: gws rejects a separate value that starts with "-".
        f"--body={reply_body}",
    ]

    sender = os.environ.get("SIDEQUESTOR_FROM_EMAIL") or os.environ.get("YAAS_FROM_EMAIL")
    if sender:
        sender_address = parseaddr(sender)[1]
        if not sender_address:
            parser.error("SIDEQUESTOR_FROM_EMAIL must contain a valid email address")
        command.append(f"--from={sender_address}")
    elif args.approval_id and target.get("sender"):
        # Pin the reviewed default alias even if gws's own send-as lookup later fails.
        command.append(f"--from={target['sender']}")

    cc = []
    if args.reply_all:
        cc.extend(_reply_all_cc_workaround(args.message_id))
        cc.extend(_mailboxes(args.cc))
    cc = _mailboxes(cc)
    if cc:
        command.append(f"--cc={','.join(cc)}")
    if args.remove:
        command.append(f"--remove={','.join(_mailboxes(args.remove))}")
    if args.html:
        command.append("--html")
    for attachment in args.attach:
        command.append(f"--attach={attachment}")
    if args.draft:
        command.append("--draft")

    timeout = 300 if args.attach else 60
    print(_sent_id(_run_gws(command, timeout=timeout, operation="reply")))


if __name__ == "__main__":
    try:
        main()
    except GwsFailure as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode)
