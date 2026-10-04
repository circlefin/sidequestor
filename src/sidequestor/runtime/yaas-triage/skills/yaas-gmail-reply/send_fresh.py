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

"""Send a fresh, non-reply Gmail message through the native gws helper."""

import argparse
import os
import subprocess
import sys
from email.utils import parseaddr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body-file", required=True)
    args = parser.parse_args()

    sender = os.environ.get("SIDEQUESTOR_FROM_EMAIL") or os.environ.get("YAAS_FROM_EMAIL")
    if not sender:
        parser.error("SIDEQUESTOR_FROM_EMAIL not set")
    sender_address = parseaddr(sender)[1]
    if not sender_address:
        parser.error("SIDEQUESTOR_FROM_EMAIL must contain a valid email address")

    with open(args.body_file, "r", encoding="utf-8") as body_file:
        body = body_file.read()

    gws = os.environ.get("GWS_BIN", "gws")
    command = [
        gws, "gmail", "+send", "--to", args.to, "--subject", args.subject,
        "--body", body, "--from", sender_address,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        print(f"ERROR: gws executable not found: {exc.filename}", file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print(f"ERROR: could not run gws: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        print(
            "ERROR: gws send timed out; delivery outcome may be unknown, do not retry blindly",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if result.returncode != 0:
        if result.stdout.strip():
            print(result.stdout.strip(), file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)

    print(result.stdout.strip())


if __name__ == "__main__":
    main()
