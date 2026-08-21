"""The public Sidequestor command shell."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .native import dry_tick, run_native, run_native_loop, run_native_tick
from .dashboard import serve as serve_dashboard
from .isolated import run_isolated
from .launchd import install as install_jobs
from .launchd import install_production, production_status, uninstall_production
from .launchd import render, status as launchd_status, uninstall as uninstall_jobs
from .migrations import migrate_workspace
from .resources import sync_resources
from .workspace import (
    Workspace,
    init_workspace,
    list_instances,
    load_workspace,
    register_workspace,
    rekey_workspace,
    validate_workspace,
)


COMMANDS = {
    "init": "create a workspace",
    "instances": "list and validate workspace instances",
    "setup": "render or manage workspace launchd jobs",
    "tick": "run one triage tick",
    "loop": "run the paced triage loop",
    "dashboard": "serve or inspect the dashboard",
    "doctor": "validate a workspace and its engine",
    "migrate": "apply workspace schema migrations",
    "sync-resources": "refresh managed engine resources",
    "watch": "manage watches",
    "ack": "acknowledge dispatched work",
    "approval": "manage approval state",
    "log": "write a timeline event",
    "slack-send": "send through the Slack surface",
    "react": "advance reaction lifecycle",
    "mcp-call": "call an MCP surface",
    "jira-call": "call a Jira surface",
}

LEGACY_COMMANDS = {
    "watch": "yaas-triage/ledger/add-watch.py",
    "ack": "yaas-triage/ledger/ack-watch.py",
    "approval": "yaas-triage/ledger/approval-helper.py",
    "log": "yaas-triage/surfaces/log-event.py",
}

ISOLATED_COMMANDS = {"slack-send", "react", "mcp-call", "jira-call"}


def _usage() -> str:
    lines = ["usage: sidequestor [--workspace PATH] COMMAND [ARGS...]", "", "commands:"]
    lines.extend(f"  {name:15} {description}" for name, description in COMMANDS.items())
    lines.extend(["", "global options:", "  --workspace PATH  select an initialized workspace",
                  "  --instance ID     select an instance from the advisory registry",
                  "  --version         print the engine version", "  --help            print this help"])
    return "\n".join(lines)


def _command_help(command: str) -> str:
    examples = {
        "init": "sidequestor init PATH [--name NAME]",
        "instances": "sidequestor instances list|doctor|register PATH|rekey PATH",
        "setup": "sidequestor --workspace PATH setup --render-only|install|status|uninstall",
        "tick": "sidequestor --workspace PATH tick [--dry-run|--isolated [--fake-worker]]",
        "loop": "sidequestor --workspace PATH loop [--max-ticks N]",
        "dashboard": "sidequestor --workspace PATH dashboard serve|url",
    }
    usage = examples.get(command, f"sidequestor --workspace PATH {command} [ARGS...]")
    return f"usage: {usage}\n\n{COMMANDS[command]}"


def _extract_globals(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    workspace = None
    instance = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in ("--workspace", "--instance"):
            if index + 1 >= len(argv):
                raise SystemExit(f"{value} requires a value")
            if value == "--workspace":
                workspace = argv[index + 1]
            else:
                instance = argv[index + 1]
            index += 2
            continue
        if value.startswith("--workspace="):
            workspace = value.split("=", 1)[1]
            index += 1
            continue
        if value.startswith("--instance="):
            instance = value.split("=", 1)[1]
            index += 1
            continue
        remaining.append(value)
        index += 1
    return workspace, instance, remaining


def _workspace(workspace_path: str | None, instance: str | None) -> Workspace:
    if workspace_path and instance:
        raise SystemExit("choose either --workspace or --instance, not both")
    if workspace_path:
        return load_workspace(workspace_path)
    if instance:
        for row in list_instances():
            if row.get("instance_id") == instance or row.get("display_name") == instance:
                return load_workspace(row["path"])
        raise SystemExit(f"instance not found: {instance}")
    inherited = os.environ.get("YAAS_WORKSPACE")
    if inherited:
        return load_workspace(inherited)
    raise SystemExit("a workspace is required; use --workspace PATH")


def _cmd_init(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="yaas init")
    parser.add_argument("path")
    parser.add_argument("--name")
    values = parser.parse_args(args)
    workspace = init_workspace(values.path, values.name)
    sync_resources(workspace)
    print(f"initialized Sidequestor workspace: {workspace.root}")
    print(f"instance_id: {workspace.instance_id}")
    return 0


def _cmd_instances(args: list[str], workspace_path: str | None = None) -> int:
    action = args[0] if args else "list"
    if action == "list":
        for row in list_instances():
            print(json.dumps(row, sort_keys=True))
        return 0
    if action in {"doctor", "register", "rekey"}:
        path = args[1] if len(args) > 1 else workspace_path or os.environ.get("YAAS_WORKSPACE")
        if not path:
            raise SystemExit(f"instances {action} requires PATH or YAAS_WORKSPACE")
        if action == "rekey":
            updated = rekey_workspace(path)
            print(f"rekeyed instance {updated.instance_id}: {updated.root}")
            return 0
        workspace = load_workspace(path)
        if action == "register":
            register_workspace(workspace)
            print(f"registered instance {workspace.instance_id}: {workspace.root}")
            return 0
        errors = validate_workspace(workspace)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"instance {workspace.instance_id}: {workspace.root}")
        return 0
    raise SystemExit(f"unknown instances action: {action}")


def _cmd_doctor(workspace: Workspace) -> int:
    errors = validate_workspace(workspace)
    checks = [("workspace", not errors), ("python", sys.version_info >= (3, 11)), ("package", True)]
    for name, passed in checks:
        print(f"{name}: {'ok' if passed else 'error'}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


def _cmd_setup(workspace: Workspace, args: list[str]) -> int:
    production = "--production" in args
    args = [arg for arg in args if arg != "--production"]
    action = args[0] if args and not args[0].startswith("-") else "--render-only"
    if production:
        if action == "install":
            manifest = install_production(workspace, Path(sys.executable))
            print(f"installed production jobs for {manifest['workspace']}")
            for name, job in manifest["jobs"].items():
                print(f"{name}: {job['label']}")
            return 0
        if action == "status":
            manifest = production_status(workspace)
            if manifest is None:
                print("production jobs: not installed")
                return 0
            print(f"production jobs: installed ({manifest['workspace']})")
            for name, job in manifest["jobs"].items():
                print(f"{name}: {job['label']} ({job['plist']})")
            return 0
        if action == "uninstall":
            print("uninstalled production jobs" if uninstall_production(workspace) else "production jobs: not installed")
            return 0
        raise SystemExit("production setup supports install, status, and uninstall")
    if "--render-only" in args or action == "--render-only":
        destination = render(workspace, Path(sys.executable))
        print(f"rendered launchd jobs: {destination}")
        return 0
    if action in {"install", "status", "uninstall"}:
        if action == "install":
            manifest = install_jobs(workspace, Path(sys.executable))
            print(f"installed shadow jobs for {manifest['instance_id']}")
            for name, job in manifest["jobs"].items():
                print(f"{name}: {job['label']}")
            return 0
        if action == "status":
            manifest = launchd_status(workspace)
            if manifest is None:
                print("shadow jobs: not installed")
                return 0
            print(f"shadow jobs: installed ({manifest['instance_id']})")
            print(f"backend: {manifest['backend']}")
            for name, job in manifest["jobs"].items():
                print(f"{name}: {job['label']} ({job['plist']})")
            return 0
        removed = uninstall_jobs(workspace)
        print("uninstalled shadow jobs" if removed else "shadow jobs: not installed")
        return 0
    raise SystemExit(f"unknown setup action: {action}")


def _cmd_loop(workspace: Workspace, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="yaas loop")
    parser.add_argument("--max-ticks", type=int)
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("YAAS_LOOP_INTERVAL", "60")))
    values, unknown = parser.parse_known_args(args)
    if unknown:
        raise SystemExit(f"unknown loop arguments: {' '.join(unknown)}")
    if values.isolated and values.max_ticks is None:
        return run_native_loop(workspace, max(0.01, values.interval))
    if values.max_ticks is None:
        return run_native(workspace, "yaas-triage/triage-loop.sh", [])
    ticks = values.max_ticks if values.max_ticks is not None else None
    completed = 0
    try:
        while ticks is None or completed < max(0, ticks):
            code = run_native_tick(workspace) if values.isolated else dry_tick(workspace)
            completed += 1
            if code:
                return code
            if ticks is None:
                import time
                time.sleep(max(0, values.interval))
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_tick(workspace: Workspace, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="yaas tick")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--fake-worker", action="store_true")
    values, unknown = parser.parse_known_args(args)
    if unknown:
        raise SystemExit(f"unknown tick arguments: {' '.join(unknown)}")
    if values.fake_worker and not values.isolated:
        raise SystemExit("--fake-worker requires --isolated")
    if values.dry_run and values.isolated:
        raise SystemExit("choose either --dry-run or --isolated")
    if values.dry_run:
        return dry_tick(workspace)
    if values.isolated:
        return run_native_tick(workspace, fake_worker=values.fake_worker)
    return run_native(workspace, "yaas-triage/tick.py", [])


def _cmd_dashboard(workspace: Workspace, args: list[str]) -> int:
    action = args[0] if args else "serve"
    if action == "url":
        url_file = workspace.state / "dashboard-url.txt"
        print(url_file.read_text().strip() if url_file.exists() else "dashboard is not running")
        return 0 if url_file.exists() else 1
    if action == "serve":
        parser = argparse.ArgumentParser(prog="yaas dashboard serve")
        parser.add_argument("port", nargs="?", type=int, default=8877)
        parser.add_argument("--port", dest="named_port", type=int)
        values = parser.parse_args(args[1:])
        return serve_dashboard(workspace, values.named_port if values.named_port is not None else values.port)
    raise SystemExit(f"unknown dashboard action: {action}")


def _cmd_migrate(path: str | None, name: str | None = None) -> int:
    if not path:
        raise SystemExit("migrate requires --workspace PATH")
    workspace, archive, changed = migrate_workspace(path, name)
    if changed:
        print(f"migrated Sidequestor workspace: {workspace.root}")
        if archive:
            print(f"rollback archive: {archive}")
    else:
        print(f"workspace schema is current: {workspace.yaas_dir / '.yaas-version'}")
    return 0


def _dispatch(command: str, args: list[str], workspace_path: str | None, instance: str | None) -> int:
    if command == "init":
        return _cmd_init(args)
    if command == "instances":
        return _cmd_instances(args, workspace_path)
    if command == "migrate":
        return _cmd_migrate(workspace_path, args[0] if args else None)
    workspace = _workspace(workspace_path, instance)
    if command == "doctor":
        return _cmd_doctor(workspace)
    if command == "sync-resources":
        print(f"synced engine resources: {sync_resources(workspace)}")
        return 0
    if command == "setup":
        return _cmd_setup(workspace, args)
    if command == "tick":
        return _cmd_tick(workspace, args)
    if command == "loop":
        return _cmd_loop(workspace, args)
    if command == "dashboard":
        return _cmd_dashboard(workspace, args)
    if command in ISOLATED_COMMANDS:
        return run_isolated(workspace, command, args)
    if command in LEGACY_COMMANDS:
        return run_native(workspace, LEGACY_COMMANDS[command], args)
    raise SystemExit(f"command not implemented: {command}")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--version" in raw:
        print(__version__)
        return 0
    if not raw or raw == ["help"]:
        print(_usage())
        return 0
    workspace_path, instance, remaining = _extract_globals(raw)
    if not remaining or remaining == ["--help"]:
        print(_usage())
        return 0
    command, args = remaining[0], remaining[1:]
    if command not in COMMANDS:
        print(f"unknown command: {command}\n\n{_usage()}", file=sys.stderr)
        return 2
    if "--help" in args or "--help" in remaining:
        print(_command_help(command))
        return 0
    return _dispatch(command, args, workspace_path, instance)
