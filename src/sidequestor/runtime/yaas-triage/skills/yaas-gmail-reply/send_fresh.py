#!/usr/bin/env python3
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

"""Send a fresh (non-reply) Gmail message via gws CLI."""
import argparse
import base64
import json
import os
import subprocess
import sys
from email.message import EmailMessage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", required=True)
    args = ap.parse_args()

    sender = os.environ.get("SIDEQUESTOR_FROM_EMAIL") or os.environ.get("YAAS_FROM_EMAIL")
    if not sender:
        print("SIDEQUESTOR_FROM_EMAIL not set", file=sys.stderr)
        sys.exit(1)

    with open(args.body_file, "r", encoding="utf-8") as f:
        body = f.read()

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = args.to
    msg["Subject"] = args.subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    payload = {"userId": "me", "raw": raw}

    gws = os.environ.get("GWS_BIN", "gws")
    result = subprocess.run(
        [gws, "gmail", "users", "messages", "send", "--json", json.dumps(payload)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)

    # gws prints some banner + JSON; find first '{' line
    out = result.stdout
    print(out)


if __name__ == "__main__":
    main()
