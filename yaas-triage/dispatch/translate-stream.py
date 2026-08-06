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
translate-stream.py — normalize an agent's raw event stream into the facts
YaaS's post-run logic needs, independent of backend: did it exit cleanly, what
did it cost (raw tokens), and the final agent message.

Usage:
  translate-stream.py <backend> <raw_ndjson_path> <exit_code>

Emits one JSON line to stdout:
  {"backend","exit","is_error","input_tokens","output_tokens","final"}

Notes:
- Tokens are RAW (no $ conversion — that needs per-model pricing and is out of
  scope here; triage just reports the counts).
- Slack/tool HEALTH is intentionally NOT derived here. It is unreliable from the
  stream (Cursor sends via a shell call to mcp-call.sh, which is invisible as an
  "MCP call"; Codex emits no server-status event). YaaS detects a Slack outage
  with a pre-dispatch mcp-call.sh ping in triage.sh instead.
"""
import sys
import json


def load(path):
    events = []
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if not s.startswith("{"):
                    continue
                try:
                    events.append(json.loads(s))
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return events


def translate_codex(ev):
    """Codex --json: thread.started / turn.started / item.completed / turn.completed."""
    final = ""
    inp = out = 0
    for e in ev:
        t = e.get("type")
        if t == "turn.completed":
            u = e.get("usage", {}) or {}
            inp = u.get("input_tokens", inp)   # per-turn (last wins)
            out += u.get("output_tokens", 0)   # summed across turns
        elif t == "item.completed":
            item = e.get("item", {}) or {}
            if item.get("type") == "agent_message":
                final = item.get("text", final)
    # No reliable hard-error field on Codex; rely on exit code upstream.
    return final, inp, out, False


def translate_cursor(ev):
    """Cursor stream-json: system / assistant / user / tool_call / result."""
    final = ""
    inp = out = 0
    hard_error = False
    for e in ev:
        if e.get("type") == "result":
            final = e.get("result", final)
            hard_error = bool(e.get("is_error", False))
            u = e.get("usage", {}) or {}
            inp = u.get("inputTokens", inp)
            out = u.get("outputTokens", out)
    return final, inp, out, hard_error


def translate_claude(ev):
    """Claude stream-json: result event carries usage."""
    final = ""
    inp = out = 0
    hard_error = False
    for e in ev:
        if e.get("type") == "result":
            final = e.get("result", final) or final
            hard_error = bool(e.get("is_error", False))
            u = e.get("usage", {}) or {}
            inp = u.get("input_tokens", inp)
            out = u.get("output_tokens", out)
    return final, inp, out, hard_error


def main():
    backend, path, exit_code = sys.argv[1], sys.argv[2], sys.argv[3]
    ev = load(path)
    if backend == "codex":
        final, inp, out, err = translate_codex(ev)
    elif backend == "cursor":
        final, inp, out, err = translate_cursor(ev)
    else:
        final, inp, out, err = translate_claude(ev)

    try:
        exit_int = int(exit_code)
    except (TypeError, ValueError):
        exit_int = -1
    print(json.dumps({
        "backend": backend,
        "exit": exit_int,
        "is_error": err,
        "input_tokens": inp,
        "output_tokens": out,
        "final": (final or "")[:400],
    }))


if __name__ == "__main__":
    main()
