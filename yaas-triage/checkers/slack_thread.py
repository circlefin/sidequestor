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

"""
checkers/slack_thread.py — check a Slack thread for new replies since watermark.

Input:  watch entry JSON as argv[1]
        {"type":"slack_thread","channel_id":"C...","thread_ts":"1234.567",
         "last_checked_ts":"1234.567","reason":"..."}

Output: one line of JSON per checkers/result.py. Read-backed, so checkers/slack.py pages
        the source until a reply at or below the watermark proves the gap is covered;
        if it saturates first, emits complete=false so triage refuses to advance the
        cursor past replies it never saw.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
import slack


def main(entry):
    slack.read_backed(
        entry,
        tool="slack_read_thread",
        identity_args={"channel_id": entry["channel_id"],
                       "message_ts": entry["thread_ts"]},
        not_found_tokens=("thread_not_found", "channel_not_found"),
    )


if __name__ == "__main__":
    result.run(main)
