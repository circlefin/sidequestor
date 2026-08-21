"""Stage 2 adapters for commands that must not contact external systems."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .workspace import Workspace


def _adapter_dir(workspace: Workspace) -> Path:
    path = workspace.yaas_dir / "stage2-adapters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record(workspace: Workspace, surface: str, payload: dict) -> None:
    entry = {"ts": time.time(), "surface": surface, "payload": payload}
    with (_adapter_dir(workspace) / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")


def _slack_send(workspace: Workspace, args: list[str]) -> int:
    if len(args) == 1 and not args[0].startswith("-"):
        try:
            payload = json.loads(args[0])
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON argument: {exc}", flush=True)
            return 2
    else:
        parser = argparse.ArgumentParser(prog="sidequestor slack-send")
        parser.add_argument("--channel-id", "--channel", dest="channel_id", required=True)
        parser.add_argument("--message", "--text", required=True)
        parser.add_argument("--thread-ts")
        parser.add_argument("--draft", action="store_true")
        parser.add_argument("--quest-id")
        values = vars(parser.parse_args(args))
        payload = {key: value for key, value in values.items() if value is not None}

    if not isinstance(payload, dict) or not payload.get("channel_id") or payload.get("message") is None:
        print("error: channel_id and message are required")
        return 2
    _record(workspace, "slack-send", {**payload, "delivered": False, "adapter": "isolated"})
    print(json.dumps({
        "adapter": "isolated",
        "delivered": False,
        "draft": bool(payload.get("draft")),
        "channel_id": payload["channel_id"],
        "response_ts": "",
        "permalink": "",
    }))
    return 0


def _react(workspace: Workspace, args: list[str]) -> int:
    if len(args) < 4 or args[0] != "advance" or args[3] not in {"loading", "done"}:
        print("usage: sidequestor react advance <channel_id> <message_ts> <loading|done>")
        return 2
    channel, message_ts, state = args[1:4]
    path = _adapter_dir(workspace) / "reactions.json"
    try:
        values = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        values = {}
    values[f"{channel}/{message_ts}"] = state
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")
    _record(workspace, "react", {"channel_id": channel, "message_ts": message_ts, "state": state})
    print(f"isolated reaction {channel}/{message_ts} -> {state}")
    return 0


def _mcp_call(workspace: Workspace, args: list[str]) -> int:
    if len(args) != 2:
        print("usage: sidequestor mcp-call <tool_name> <arguments_json>")
        return 2
    try:
        arguments = json.loads(args[1])
    except json.JSONDecodeError as exc:
        print(f"error: invalid arguments JSON: {exc}")
        return 2
    _record(workspace, "mcp-call", {"tool": args[0], "arguments": arguments})
    print(json.dumps({"adapter": "isolated", "ok": True, "tool": args[0], "result": {}}))
    return 0


def _jira_call(workspace: Workspace, args: list[str]) -> int:
    if len(args) < 2 or not args[1].startswith("/") or args[1].startswith("//"):
        print("usage: sidequestor jira-call <METHOD> <path> [body_json]")
        return 2
    body = None
    if len(args) > 2:
        try:
            body = json.loads(args[2])
        except json.JSONDecodeError as exc:
            print(f"error: invalid body JSON: {exc}")
            return 2
    _record(workspace, "jira-call", {"method": args[0], "path": args[1], "body": body})
    print(json.dumps({"adapter": "isolated", "ok": True, "method": args[0], "path": args[1], "result": {}}))
    return 0


def run_isolated(workspace: Workspace, surface: str, args: list[str]) -> int:
    """Run a deterministic local substitute for an external side-effect surface."""
    if surface == "slack-send":
        return _slack_send(workspace, args)
    if surface == "react":
        return _react(workspace, args)
    if surface == "mcp-call":
        return _mcp_call(workspace, args)
    if surface == "jira-call":
        return _jira_call(workspace, args)
    raise ValueError(f"unknown isolated surface: {surface}")
