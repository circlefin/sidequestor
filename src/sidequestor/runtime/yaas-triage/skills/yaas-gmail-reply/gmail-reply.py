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
from email.utils import getaddresses, parseaddr


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


def _mailboxes(values):
    addresses = []
    seen = set()
    for _, address in getaddresses(values):
        key = address.casefold()
        if address and key not in seen:
            seen.add(key)
            addresses.append(address)
    return addresses


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
    delivery = parser.add_mutually_exclusive_group(required=True)
    delivery.add_argument("--send", action="store_true")
    delivery.add_argument("--draft", action="store_true")
    args = parser.parse_args()

    if args.cc and not args.reply_all:
        parser.error("--cc requires --reply-all")
    if args.remove and not args.reply_all:
        parser.error("--remove requires --reply-all")

    reply_body = args.body if args.body is not None else sys.stdin.read().strip()
    if not reply_body:
        parser.error("no reply body provided")

    command = [
        "gmail",
        "+reply-all" if args.reply_all else "+reply",
        "--message-id",
        args.message_id,
        "--body",
        reply_body,
    ]

    sender = os.environ.get("SIDEQUESTOR_FROM_EMAIL") or os.environ.get("YAAS_FROM_EMAIL")
    if sender:
        sender_address = parseaddr(sender)[1]
        if not sender_address:
            parser.error("SIDEQUESTOR_FROM_EMAIL must contain a valid email address")
        command.extend(["--from", sender_address])

    cc = []
    if args.reply_all:
        cc.extend(_reply_all_cc_workaround(args.message_id))
        cc.extend(_mailboxes(args.cc))
    cc = _mailboxes(cc)
    if cc:
        command.extend(["--cc", ",".join(cc)])
    if args.remove:
        command.extend(["--remove", ",".join(_mailboxes(args.remove))])
    if args.html:
        command.append("--html")
    for attachment in args.attach:
        command.extend(["--attach", attachment])
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
