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
                and SHELL_SLACK_READ.search(item.get("command", ""))
                and not failed(item.get("aggregated_output", ""))
            ):
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


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "slack":
        print(f"usage: {Path(sys.argv[0]).name} slack <worker.ndjson>", file=sys.stderr)
        return 2
    path = Path(sys.argv[2])
    return 0 if path.is_file() and evidence(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
