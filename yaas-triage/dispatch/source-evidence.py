#!/usr/bin/env python3
"""Detect successful source reads in a headless worker's JSONL event stream."""

import json
import re
import sys
from pathlib import Path


SLACK_READ = re.compile(r"slack(?:\.|__)[^\s\"']*(?:read|search)", re.IGNORECASE)
SHELL_SLACK_READ = re.compile(r"mcp-call\.sh\s+slack_(?:read|search)", re.IGNORECASE)
# Tool-availability / transport failures. These phrases are harness output, not
# the kind of thing a Slack message body says, so they are safe to match in
# free text.
TOOL_ERROR = re.compile(
    r"no such tool|tool[_ -]?not[_ -]?found|needs authentication|failed to connect|"
    r"mcp[^\n]*(?:unavailable|not exposed)",
    re.IGNORECASE,
)
# Slack API failure codes. Matched ONLY against a response envelope (a parsed
# `ok`/`error` field, or a JSON-shaped field in raw text) — never as bare words.
# A successful read of a thread that happens to discuss `ratelimited` or
# `invalid_auth` is still a successful read, and treating the words themselves
# as failure vetoed recovery in exactly the debugging threads where a tooling
# outage gets talked about.
API_ERROR_CODE = re.compile(
    r"invalid_auth|not_authed|token_revoked|account_inactive|ratelimited|"
    r"missing_scope|invalid_arguments",
    re.IGNORECASE,
)
ENVELOPE_ERROR = re.compile(
    r"\"ok\"\s*:\s*false|\"error\"\s*:\s*\"(?:" + API_ERROR_CODE.pattern + r")\"",
    re.IGNORECASE,
)


def blocks(value):
    return value if isinstance(value, list) else []


def is_slack_tool(name):
    return isinstance(name, str) and bool(SLACK_READ.search(name))


def failed(value):
    """True when a tool result carries a transport or Slack-API failure.

    Structure-aware on purpose: dicts and JSON-encoded strings are inspected as
    envelopes, and the free-text regexes only ever see payloads that are not
    valid JSON. This keeps message *content* from vetoing a genuine read.
    """
    if isinstance(value, dict):
        if value.get("ok") is False:
            return True
        error = value.get("error")
        if isinstance(error, str) and API_ERROR_CODE.fullmatch(error.strip()):
            return True
        if error not in (None, "", False) and not isinstance(error, str):
            return True
        return any(failed(v) for v in value.values())
    if isinstance(value, list):
        return any(failed(v) for v in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return failed(parsed)
        if API_ERROR_CODE.fullmatch(value.strip()):
            return True  # a bare error code IS the whole payload, not content
        return bool(TOOL_ERROR.search(value) or ENVELOPE_ERROR.search(value))
    return False


# A shell command counts as a READ only if it produced a Slack RESPONSE. Matching the
# command text alone is not enough: the pattern is a substring search, so
# `echo "./mcp-call.sh slack_read_channel {\"channel_id\":\"C0…\"}"` matches, exits 0, and
# would be credited as having read C0… — a FALSE PRESENCE, which lets the watermark advance
# over messages nobody saw. That is the one direction this file must never get wrong (see
# the attribution note below: a false absence merely delays work).
#
# So require the OUTPUT to look like an mcp-call.sh response envelope rather than trusting
# the command string. An echo emits its own argument, not a JSON object with these keys.
# Deliberately strict: over-rejecting costs a delayed watch, under-rejecting buries a message.
SLACK_RESPONSE_KEYS = ("messages", "results", "message", "permalink", "response_ts",
                       "channel", "ok", "reactions", "thread", "replies")


def shell_read_succeeded(command, stdout, is_background=False):
    """True when a shell command demonstrably performed a Slack read and got a response."""
    if is_background:
        return False              # returns 0 immediately; the read has not happened yet
    if not command or not SHELL_SLACK_READ.search(command):
        return False
    out = (stdout or "").strip()
    if not out:
        return False              # exit 0 with no output proves nothing was read
    if failed(out):
        return False
    try:
        parsed = json.loads(out)
    except ValueError:
        return False              # a real response is JSON; an echoed command is not
    return isinstance(parsed, dict) and any(k in parsed for k in SLACK_RESPONSE_KEYS)


# ── Cursor's shell tool call ───────────────────────────────────────────────────
# Cursor emits a shape neither of the other two backends use, so without this it looked
# like a worker that had read nothing at all. Captured from a real `cursor-agent -p
# --output-format stream-json` run on 2026-08-10:
#
#   {"type":"tool_call","subtype":"completed","tool_call":{"shellToolCall":{
#      "args":{"command":"...mcp-call.sh slack_read_channel '{\"channel_id\":\"C0…\"}'"},
#      "result":{"success":{"exitCode":0,"command":"…","stdout":"{\"messages\":…"}}}}}
#
# A failure swaps `success` for `failure` (same inner fields, non-zero exitCode), which is
# why this reads the success branch specifically rather than trusting the subtype.
#
# Returns (command, stdout) for a SUCCESSFUL Slack-reading shell call, else (None, None),
# so evidence() and read_sources() cannot drift apart on the parsing.
def cursor_shell_read(event):
    if event.get("type") != "tool_call" or event.get("subtype") != "completed":
        return None, None
    shell = ((event.get("tool_call") or {}).get("shellToolCall") or {})
    result = shell.get("result") or {}
    ok = result.get("success")
    # A result carrying BOTH branches is contradictory; treat it as a failure rather than
    # taking the optimistic read.
    if result.get("failure") is not None:
        return None, None
    if not isinstance(ok, dict) or ok.get("exitCode") != 0:
        return None, None
    args = shell.get("args") or {}
    cmd = ok.get("command") or args.get("command", "") or ""
    background = bool(result.get("isBackground") or args.get("isBackground"))
    if not shell_read_succeeded(cmd, ok.get("stdout", ""), background):
        return None, None
    return cmd, ok.get("stdout", "") or ""


def evidence(path):
    pending_claude_calls = set()
    for raw in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed":
            if (
                item.get("type") == "mcp_tool_call"
                and is_slack_tool(item.get("tool"))
                and item.get("status") == "completed"
                and item.get("error") is None
                and item.get("result") is not None
                and not failed(item.get("result"))
            ):
                return True
            if (
                item.get("type") == "command_execution"
                and item.get("exit_code") == 0
                and shell_read_succeeded(item.get("command", ""),
                                         item.get("aggregated_output", ""))
            ):
                return True

        if cursor_shell_read(event)[0] is not None:
            return True

        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        for block in blocks(message.get("content")):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and is_slack_tool(block.get("name")):
                pending_claude_calls.add(block.get("id"))
            if block.get("type") != "tool_result" or block.get("tool_use_id") not in pending_claude_calls:
                continue
            if not block.get("is_error", False) and not failed(block.get("content", "")):
                return True
    return False


# ── Per-source attribution ─────────────────────────────────────────────────────
# `evidence()` answers "did ANY Slack read succeed", which is the right question for the
# tooling-outage guard. It is the wrong question for the ack ledger: a quest with five dirty
# watches on five channels would have one successful read count as proof for all five.
#
# So this extracts WHICH channels were successfully read. It is deliberately conservative —
# a channel counts only when the read both names it and succeeded — because the consumer
# HOLDS a watermark when a channel is absent, and a false absence merely delays work while a
# false presence buries a message.

CHANNEL_ID = re.compile(r"\b([CDG][A-Z0-9]{6,})\b")


def _channels_in(value):
    """Channel ids mentioned in a tool call's arguments, whatever shape they arrive in."""
    if isinstance(value, dict):
        out = set()
        for k, v in value.items():
            if k in ("channel_id", "channel") and isinstance(v, str):
                out |= set(CHANNEL_ID.findall(v))
            else:
                out |= _channels_in(v)
        return out
    if isinstance(value, list):
        out = set()
        for v in value:
            out |= _channels_in(v)
        return out
    if isinstance(value, str):
        return set(CHANNEL_ID.findall(value))
    return set()


def read_sources(path):
    """The set of channel ids this worker successfully read from."""
    found = set()
    pending = {}
    for raw in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed":
            if (item.get("type") == "mcp_tool_call" and is_slack_tool(item.get("tool"))
                    and item.get("status") == "completed" and item.get("error") is None
                    and item.get("result") is not None and not failed(item.get("result"))):
                found |= _channels_in(item.get("arguments"))
            if (item.get("type") == "command_execution" and item.get("exit_code") == 0
                    and shell_read_succeeded(item.get("command", ""),
                                             item.get("aggregated_output", ""))):
                found |= _channels_in(item.get("command", ""))

        cmd, _out = cursor_shell_read(event)
        if cmd is not None:
            found |= _channels_in(cmd)

        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        for block in blocks(message.get("content")):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and is_slack_tool(block.get("name")):
                pending[block.get("id")] = _channels_in(block.get("input"))
            if block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                if not block.get("is_error", False) and not failed(block.get("content", "")):
                    found |= pending[block["tool_use_id"]]
    return found


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "sources":
        path = Path(sys.argv[2])
        if not path.exists():
            return 1
        for ch in sorted(read_sources(path)):
            print(ch)
        return 0
    if len(sys.argv) != 3 or sys.argv[1] != "slack":
        print(f"usage: {Path(sys.argv[0]).name} slack|sources <worker.ndjson>", file=sys.stderr)
        return 2
    path = Path(sys.argv[2])
    return 0 if path.is_file() and evidence(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
