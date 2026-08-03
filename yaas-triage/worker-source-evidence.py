#!/usr/bin/env python3
"""Detect successful source reads in a headless worker's JSONL event stream."""

import json
import re
import sys
from pathlib import Path


SLACK_READ = re.compile(r"slack(?:\.|__)[^\s\"']*(?:read|search)", re.IGNORECASE)
SHELL_SLACK_READ = re.compile(r"mcp-call\.sh\s+slack_(?:read|search)", re.IGNORECASE)
ERROR_TEXT = re.compile(
    r"no such tool|tool[_ -]?not[_ -]?found|needs authentication|failed to connect|"
    r"mcp[^\n]*(?:unavailable|not exposed)|\"ok\"\s*:\s*false|"
    r"invalid_auth|not_authed|token_revoked|account_inactive|ratelimited",
    re.IGNORECASE,
)


def blocks(value):
    return value if isinstance(value, list) else []


def is_slack_tool(name):
    return isinstance(name, str) and bool(SLACK_READ.search(name))


def evidence(path):
    pending_claude_calls = set()
    for raw in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") == "item.completed":
            result_text = json.dumps(item.get("result", ""), ensure_ascii=True)
            if (
                item.get("type") == "mcp_tool_call"
                and is_slack_tool(item.get("tool"))
                and item.get("status") == "completed"
                and item.get("error") is None
                and item.get("result") is not None
                and not ERROR_TEXT.search(result_text)
            ):
                return True
            if (
                item.get("type") == "command_execution"
                and item.get("exit_code") == 0
                and SHELL_SLACK_READ.search(item.get("command", ""))
                and not ERROR_TEXT.search(item.get("aggregated_output", ""))
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
            rendered = json.dumps(block.get("content", ""), ensure_ascii=True)
            if not block.get("is_error", False) and not ERROR_TEXT.search(rendered):
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
