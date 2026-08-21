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

"""Validated reaction-workflow emoji configuration shared by every runtime layer."""

import json
import os
import re
import sys


EMOJI_SETTINGS = {
    "process": ("YAAS_REACTION_PROCESS_EMOJI", "claude-intensifies"),
    "draft": ("YAAS_REACTION_DRAFT_EMOJI", "writing_hand"),
    "save": ("YAAS_REACTION_SAVE_EMOJI", "floppy_disk"),
    "adopt": ("YAAS_REACTION_ADOPT_EMOJI", "incoming_envelope"),
    "loading": ("YAAS_REACTION_LOADING_EMOJI", "claudeloading"),
    "done": ("YAAS_REACTION_DONE_EMOJI", "updatedone"),
}
EMOJI_NAME = re.compile(r"^[A-Za-z0-9_+-]+$")


def load_reaction_emojis(env=None):
    """Return semantic-name to Slack emoji-name mapping, or raise ValueError."""
    env = os.environ if env is None else env
    result = {}
    for role, (var, default) in EMOJI_SETTINGS.items():
        canonical = var.replace("YAAS_", "SIDEQUESTOR_", 1)
        value = str(env.get(canonical, env.get(var, default)) or "").strip()
        if value.startswith(":") and value.endswith(":") and len(value) > 2:
            value = value[1:-1]
        if not value or not EMOJI_NAME.fullmatch(value):
            raise ValueError(f"{var} must be a Slack emoji name without spaces (got {value!r})")
        result[role] = value

    duplicates = sorted({value for value in result.values() if list(result.values()).count(value) > 1})
    if duplicates:
        raise ValueError(f"reaction workflow emojis must be unique (duplicate: {duplicates[0]!r})")
    return result


def main():
    try:
        print(json.dumps(load_reaction_emojis(), sort_keys=True))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
