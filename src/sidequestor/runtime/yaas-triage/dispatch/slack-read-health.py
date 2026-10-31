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

"""Return success when a worker event stream contains a successful Slack read."""

import json
import re
import shlex
import sys
from pathlib import Path


SLACK_READ = re.compile(r"slack(?:\.|__)[^\s\"']*(?:read|search)", re.IGNORECASE)
def is_shell_slack_read(command):
    if not isinstance(command, str) or any(c in command for c in ("\n", "\r", "$", "`", "#")):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    redirects = {">", ">>", "<", "<<", "<<<", "<>", ">&", "<&", "&>", "&>>", ">|"}
    # shlex groups adjacent operators (e.g. ");", "|&"). Only simple
    # foreground invocations and redirections provide an unambiguous exit status.
    for token in tokens:
        if token and all(c in ";&|()<>" for c in token) and token not in redirects:
            return False
    for index, token in enumerate(tokens[:-1]):
        if Path(token).name != "mcp-call.sh" or not re.match(r"slack_(?:read|search)", tokens[index + 1], re.I):
            continue
        prefix = tokens[:index]
        if prefix in ([], ["bash"], ["sh"], ["exec"], ["exec", "bash"], ["exec", "sh"]):
            return True
    return False


TOOL_ERROR = re.compile(
    r"no such tool|tool[_ -]?not[_ -]?found|needs authentication|failed to connect|"
    r"mcp[^\n]*(?:unavailable|not exposed)",
    re.IGNORECASE,
)
API_ERROR_CODE = re.compile(
    r"invalid_auth|not_authed|token_revoked|account_inactive|ratelimited|"
    r"missing_scope|invalid_arguments",
    re.IGNORECASE,
)
ENVELOPE_ERROR = re.compile(
    r'"ok"\s*:\s*false|"error"\s*:\s*"(?:' + API_ERROR_CODE.pattern + r')"',
    re.IGNORECASE,
)


def blocks(value):
    return value if isinstance(value, list) else []


def is_slack_tool(name):
    return isinstance(name, str) and bool(SLACK_READ.search(name))


def failed(value):
    """Return true for a transport or Slack API failure envelope."""
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
            return True
        return bool(TOOL_ERROR.search(value) or ENVELOPE_ERROR.search(value))
    return False


def shell_read_succeeded(command, stdout, is_background=False):
    """Require a completed wrapper read with non-error output.

    The wrapper emits Slack's text payload, not the surrounding JSON-RPC envelope. Its exit
    code already carries the mechanical auth/transport/API verdict from surfaces/client.py.
    """
    if is_background or not command or not is_shell_slack_read(command):
        return False
    if isinstance(stdout, list):
        stdout = "\n".join(v.get("text", "") for v in stdout
                           if isinstance(v, dict) and isinstance(v.get("text"), str))
    if not isinstance(stdout, str):
        return False
    out = stdout.strip()
    if not out or failed(out):
        return False
    return True


def cursor_shell_read_succeeded(event):
    if event.get("type") != "tool_call" or event.get("subtype") != "completed":
        return False
    shell = ((event.get("tool_call") or {}).get("shellToolCall") or {})
    result = shell.get("result") or {}
    success = result.get("success")
    if result.get("failure") is not None:
        return False
    if not isinstance(success, dict) or success.get("exitCode") != 0:
        return False
    args = shell.get("args") or {}
    command = success.get("command") or args.get("command", "") or ""
    background = bool(result.get("isBackground") or args.get("isBackground"))
    return shell_read_succeeded(command, success.get("stdout", ""), background)


def has_successful_slack_read(path):
    pending_claude_calls = {}
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
                and shell_read_succeeded(item.get("command", ""), item.get("aggregated_output", ""))
            ):
                return True

        if cursor_shell_read_succeeded(event):
            return True

        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        for block in blocks(message.get("content")):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                if is_slack_tool(block.get("name")):
                    pending_claude_calls[block.get("id")] = None
                elif block.get("name") == "Bash":
                    args = block.get("input") or {}
                    command = args.get("command", "")
                    if is_shell_slack_read(command) and not args.get("run_in_background"):
                        pending_claude_calls[block.get("id")] = command
            if block.get("type") != "tool_result" or block.get("tool_use_id") not in pending_claude_calls:
                continue
            if block.get("is_error", False) or failed(block.get("content", "")):
                continue
            command = pending_claude_calls[block.get("tool_use_id")]
            if command is None:
                return True
            result = event.get("tool_use_result") or {}
            if not isinstance(result, dict) or result.get("interrupted") or result.get("backgroundTaskId"):
                continue
            if result.get("exitCode", 0) != 0:
                continue
            if shell_read_succeeded(command, result.get("stdout", block.get("content", ""))):
                return True
    return False


def main():
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <worker.ndjson>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    return 0 if path.is_file() and has_successful_slack_read(path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
