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
format-stream.py — convert claude -p stream-json NDJSON on stdin into a
compact, human-readable transcript on stdout.

Usage in the original shell orchestrator:
  claude -p --output-format stream-json --verbose ... \
    | tee "$WORKER_LOG_RAW" \
    | python3 format-stream.py >> "$WORKER_LOG"

Emits one or more short lines per event:
  - system/init          → silent
  - assistant text       → prints the text as-is
  - assistant tool_use   → "→ ToolName(…short summary…)"
  - user tool_result     → "← ToolName: <preview>"  (keyed off prior tool_use_id)
  - result               → silent (handled by the metrics extractor in the original shell orchestrator)

Unknown types pass through as "<type>: <first 120 chars>" so nothing is lost.
"""
import sys, json, re

PREVIEW = 140           # max chars per tool input/output preview
tool_names_by_id = {}   # tool_use_id -> name, so tool_result can be labeled

# Worker Bash commands (e.g. curl with sandbox API keys) get previewed into the
# worker log. logs/ is gitignored, but redact obvious secret shapes anyway so a
# shared screen or repo-tree grep can't surface a live token.
_SECRET_RE = [
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]+'), r'\1***'),
    (re.compile(r'xox[baprs]-[A-Za-z0-9-]+'), 'xox***'),
    (re.compile(r'sk-[A-Za-z0-9]{8,}'), 'sk-***'),
    (re.compile(r'(?i)\b(api[_-]?key|token|secret|password)("?\s*[:=]\s*"?)[^\s"&]+'), r'\1\2***'),
]

def _redact(s):
    for rx, repl in _SECRET_RE:
        s = rx.sub(repl, s)
    return s

def short(v, n=PREVIEW):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    s = _redact(s)          # mask secrets before truncation
    return s if len(s) <= n else s[:n - 1] + "…"

for raw in sys.stdin:
    line = raw.strip()
    if not line or not line.startswith("{"):
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue

    t = ev.get("type")

    if t == "system":
        # init/setup noise — skip
        continue

    if t == "result":
        # token/cost summary is extracted separately
        continue

    if t == "assistant":
        msg = ev.get("message") or {}
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "text":
                text = (block.get("text") or "").rstrip()
                if text:
                    print(text, flush=True)
            elif bt == "tool_use":
                name = block.get("name", "tool")
                tid  = block.get("id")
                if tid: tool_names_by_id[tid] = name
                inp = block.get("input") or {}
                # Summarise the most common useful fields
                for key in ("file_path", "command", "pattern", "query", "url", "prompt"):
                    if key in inp:
                        print(f"→ {name}({key}={short(inp[key], 100)})", flush=True)
                        break
                else:
                    print(f"→ {name}({short(inp, 100)})", flush=True)
        continue

    if t == "user":
        msg = ev.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                name = tool_names_by_id.get(tid, "tool")
                content = block.get("content")
                # content may be str or list of text blocks
                if isinstance(content, list):
                    preview = " ".join(
                        (b.get("text", "") if isinstance(b, dict) else str(b))
                        for b in content
                    )
                else:
                    preview = str(content) if content is not None else ""
                is_err = block.get("is_error")
                tag = "ERR" if is_err else "ok"
                print(f"← {name} [{tag}]: {short(preview, 120)}", flush=True)
        continue

    # Unknown event type — don't drop silently
    print(f"[{t}] {short(ev, 160)}", flush=True)
