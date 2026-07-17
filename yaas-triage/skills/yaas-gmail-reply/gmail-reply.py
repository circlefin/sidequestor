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

"""
gmail-reply.py — fetch a Gmail message, build a reply, and send it.

Usage:
  echo "<body text>" | ./gmail-reply.py <message_id_to_reply_to>
  ./gmail-reply.py <message_id_to_reply_to> --body "<text>"

Reads reply body from stdin (if no --body flag) or from --body argument.
Fetches the original message to extract threadId, From, Subject, Message-ID,
and References — builds a proper RFC 2822 threaded reply and sends it.

Prints the sent Gmail message ID on success.
Exit: 0 on success, 1 on failure.
"""
import sys
import os
import json
import base64
import subprocess
import argparse
from email.mime.text import MIMEText


GWS = os.environ.get("GWS_BIN", "gws")


def gws(*args, body=None):
    cmd = [GWS] + list(args)
    kwargs = dict(capture_output=True, text=True, timeout=20)
    if body is not None:
        kwargs["input"] = body
    r = subprocess.run(cmd, **kwargs)
    if r.returncode != 0:
        raise RuntimeError(f"gws {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def header_value(payload, name):
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("message_id")
    parser.add_argument("--body", default=None)
    args = parser.parse_args()

    reply_body = args.body if args.body else sys.stdin.read().strip()
    if not reply_body:
        print("ERROR: no reply body provided", file=sys.stderr)
        sys.exit(1)

    # Fetch original message metadata
    msg = gws("gmail", "users", "messages", "get",
              "--params", json.dumps({
                  "userId": "me",
                  "id": args.message_id,
                  "format": "metadata",
              }))

    thread_id = msg["threadId"]
    payload = msg.get("payload", {})
    from_addr = header_value(payload, "From")
    orig_subject = header_value(payload, "Subject")
    orig_msg_id = header_value(payload, "Message-ID")
    orig_refs = header_value(payload, "References")

    subject = orig_subject if orig_subject.startswith("Re:") else f"Re: {orig_subject}"
    references = f"{orig_refs} {orig_msg_id}".strip()

    # Build RFC 2822 reply
    mime = MIMEText(reply_body, "plain", "utf-8")
    mime["To"] = from_addr
    mime["From"] = os.environ.get("YAAS_FROM_EMAIL", "")
    mime["Subject"] = subject
    mime["In-Reply-To"] = orig_msg_id
    mime["References"] = references

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    # Send — userId goes in --params (URL path), message body in --json
    sent = gws("gmail", "users", "messages", "send",
               "--params", json.dumps({"userId": "me"}),
               "--json", json.dumps({"raw": raw, "threadId": thread_id}))

    print(sent.get("id", ""))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
