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
checkers/slack_mention.py — check for new Slack messages that @mention a watched user
since the watermark, anywhere the searcher can see (public + private).

Input:  watch entry JSON as argv[1]
        {"type":"slack_mention","user_id":"U...","last_checked_ts":"1234.567","reason":"..."}

Output: one line of JSON per checkers/result.py. Search-backed: Slack search reads an
        eventually-consistent index, so coverage cannot be proven by reading. See
        checkers/slack.py for how the watermark is capped short of now instead.

Notes:
  - Query is the raw mention token `<@USER_ID>`; Slack search indexes mentions, so this
    surfaces messages that @mention the user across channels (verified 2026-07-19).
  - Skips [BOT] authors and the watched user's own messages, so the bot posting *as*
    the watched user can never re-trigger this watch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import result
import slack


def main(entry):
    user_id = entry["user_id"]
    slack.search_backed(
        entry,
        label="slack_mention",
        query=f"<@{user_id}> after:{slack.since_date(entry)}",
        preview_field="Text",
        skip_author_id=user_id,
    )


if __name__ == "__main__":
    result.run(main)
