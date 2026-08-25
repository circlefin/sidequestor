"""The public Sidequestor command shell."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .build_info import build_info
from .native import dry_tick, run_native, run_native_loop, run_native_tick
from .dashboard import (
    read_dashboard_url,
    serve as serve_dashboard,
    stop_dashboard_process,
    wait_for_dashboard_url,
)
from .isolated import run_isolated
from .launchd import install as install_jobs
from .launchd import install_production, production_is_running, production_status, uninstall_production
from .launchd import render, status as launchd_status, uninstall as uninstall_jobs
from .migrations import migrate_workspace
from .resources import ENGINE_VERSION, current_engine_version, sync_resources
from .setup import configured_agent, print_worker_instructions, run_setup
from .workspace import (
    Workspace,
    find_workspace,
    find_workspace_root,
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
    "setup": "run the interactive workspace onboarding wizard",
    "start": "start all jobs for a workspace",
    "stop": "stop all jobs for an instance",
    "tick": "run one triage tick",
    "loop": "run the paced triage loop",
    "dashboard": "serve or inspect the dashboard",
    "doctor": "validate a workspace and its engine",
    "migrate": "apply workspace schema migrations",
    "sync-resources": "refresh managed engine resources",
    "new-quest": "scaffold a quest folder from a JSON spec",
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
    # new-quest.py cannot self-locate from the workspace copy under
    # .yaas/engine/current/skills/: its RUNTIME_ROOT default walks to .yaas/engine,
    # which has no yaas-triage/. Routing it through here runs the in-package copy
    # with SIDEQUESTOR_WORKSPACE and SIDEQUESTOR_RUNTIME_ROOT already exported.
    "new-quest": "yaas-triage/skills/yaas-quest-creation/new-quest.py",
    "watch": "yaas-triage/ledger/add-watch.py",
    "ack": "yaas-triage/ledger/ack-watch.py",
    "approval": "yaas-triage/ledger/approval-helper.py",
    "log": "yaas-triage/surfaces/log-event.py",
}

# These four route to isolated.py, which RECORDS the call instead of performing it.
# That matters when editing the shipped skill docs: those docs deliberately invoke
# `python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/..."` rather than the `sq`
# alias, because rewriting them to `sq slack-send` would look like a tidy-up and would
# silently stop the worker from actually sending. Only LEGACY_COMMANDS above are safe
# to reference from docs as `sq <name>`.
ISOLATED_COMMANDS = {"slack-send", "react", "mcp-call", "jira-call"}


def _workspace_from_environment() -> str | None:
    return os.environ.get("SIDEQUESTOR_WORKSPACE") or os.environ.get("YAAS_WORKSPACE")


def _usage() -> str:
    lines = ["usage: sidequestor [--workspace PATH] COMMAND [ARGS...]", "", "commands:"]
    lines.extend(f"  {name:15} {description}" for name, description in COMMANDS.items())
    lines.extend(["", "global options:", "  --workspace PATH  select an initialized workspace (default: current directory)",
                  "  --instance ID     select an instance from the advisory registry",
                  "  --version         print the engine version", "  --help            print this help"])
    return "\n".join(lines)


def _command_help(command: str) -> str:
    examples = {
        "init": "sidequestor init PATH [--name NAME]",
        "instances": "sidequestor instances list [--all]|doctor|register [PATH]|rekey [PATH]",
        "setup": "sidequestor [--workspace PATH] setup [--instructions|--manifest|--production] [--non-interactive|--render-only|install|status|uninstall]",
        "start": "sidequestor [--workspace PATH] start",
        "stop": "sidequestor [--workspace PATH] stop [INSTANCE_ID]",
        "tick": "sidequestor [--workspace PATH] tick [--dry-run|--isolated [--fake-worker]]",
        "loop": "sidequestor [--workspace PATH] loop [--max-ticks N]",
        "dashboard": "sidequestor [--workspace PATH] dashboard serve|url",
        "migrate": "sidequestor [--workspace PATH] migrate [NAME|--name NAME]",
    }
    usage = examples.get(command, f"sidequestor [--workspace PATH] {command} [ARGS...]")
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
        return find_workspace(workspace_path)
    if instance:
        matches = [row for row in list_instances()
                   if row.get("instance_id") == instance or row.get("display_name") == instance]
        if len(matches) != 1:
            raise SystemExit(f"instance not found or ambiguous: {instance}")
        return load_workspace(matches[0]["path"])
    inherited = _workspace_from_environment()
    if inherited:
        return find_workspace(inherited)
    return find_workspace()


def _cmd_init(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="yaas init")
    parser.add_argument("path")
    parser.add_argument("--name")
    values = parser.parse_args(args)
    workspace = init_workspace(values.path, values.name)
    sync_resources(workspace)
    print(f"initialized Sidequestor workspace: {workspace.root}")
    print(f"instance_id: {workspace.instance_id}")
    print("Slack app manifest: run `sq setup --manifest > slack-app-manifest.yaml` to generate the ready-to-paste YAML.")
    return 0


def _cmd_setup_manifest() -> int:
    script = Path(__file__).resolve().parent / "runtime" / "yaas-triage" / "setup" / "setup.sh"
    if not script.is_file():
        raise SystemExit(f"Slack app manifest generator not found: {script}")
    result = subprocess.run(["bash", str(script), "--manifest"], check=False)
    return result.returncode


def _cmd_instances(
    args: list[str], workspace_path: str | None = None, instance: str | None = None,
) -> int:
    action = args[0] if args else "list"
    if action == "list":
        parser = argparse.ArgumentParser(prog="sidequestor instances list")
        parser.add_argument("--all", action="store_true", help="include registered workspaces that are not running")
        values = parser.parse_args(args[1:])
        rows = []
        for registered in list_instances():
            row = dict(registered)
            path = str(registered.get("path", ""))
            try:
                workspace = load_workspace(path)
            except (OSError, SystemExit):
                row["status"] = "missing"
                row["path"] = path
            else:
                row["path"] = str(workspace.root)
                row["status"] = "running" if production_is_running(workspace) else "stopped"
            if values.all or row["status"] == "running":
                rows.append(row)
        if not rows and not values.all:
            print("no running Sidequestor instances")
        for row in rows:
            print(
                f"{row['status'].upper():7} "
                f"{row.get('instance_id', 'unknown')} "
                f"{row.get('display_name', '')} "
                f"workspace={row.get('path', '')}"
            )
        return 0
    if action in {"doctor", "register", "rekey"}:
        if len(args) > 2:
            raise SystemExit(f"instances {action} accepts at most one PATH")
        if workspace_path and instance:
            raise SystemExit("choose either --workspace or --instance, not both")
        if len(args) > 1 and instance:
            raise SystemExit("choose one of PATH or --instance")
        if len(args) > 1 and workspace_path:
            positional_root = find_workspace(args[1]).root
            selected_root = find_workspace(workspace_path).root
            if positional_root != selected_root:
                raise SystemExit("PATH and --workspace select different workspaces")
        path = args[1] if len(args) > 1 else workspace_path
        if path:
            workspace = find_workspace(path)
        elif instance:
            workspace = _workspace(None, instance)
        else:
            workspace = find_workspace(_workspace_from_environment())
        if action == "rekey":
            updated = rekey_workspace(workspace.root)
            print(f"rekeyed instance {updated.instance_id}: {updated.root}")
            return 0
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
    info = build_info()
    suffix = f" ({info['commit']}, engine {info['engine']})" if info["commit"] else f" (engine {info['engine']})"
    checks = [("workspace", not errors), ("python", sys.version_info >= (3, 11))]
    for name, passed in checks:
        print(f"{name}: {'ok' if passed else 'error'}")
    print(f"sidequestor {info['version']}{suffix}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


def _cmd_setup(workspace: Workspace, args: list[str]) -> int:
    if args == ["--instructions"]:
        print_worker_instructions(configured_agent(workspace))
        return 0
    if not args or args == ["--non-interactive"]:
        return run_setup(workspace, Path(sys.executable), interactive="--non-interactive" not in args)
    production = "--production" in args
    args = [arg for arg in args if arg != "--production"]
    action = args[0] if args and not args[0].startswith("-") else "--render-only"
    if production:
        if action == "install":
            manifest = install_production(workspace, Path(sys.executable))
            print(f"installed production jobs for {manifest['workspace']}")
            for name, job in manifest["jobs"].items():
                print(f"{name}: {job['label']}")
            url = wait_for_dashboard_url(workspace)
            print(f"dashboard: {url}" if url else "dashboard: still starting (run `sq dashboard url` to check)")
            return 0
        if action == "status":
            manifest = production_status(workspace)
            if manifest is None:
                print("production jobs: not installed")
                return 0
            state = "running" if manifest.get("running", True) else "stopped"
            print(f"production jobs: installed, {state} ({manifest['workspace']})")
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


def _cmd_start(workspace: Workspace) -> int:
    manifest = install_production(workspace, Path(sys.executable))
    print(f"started Sidequestor instance {workspace.instance_id}")
    for name, job in manifest["jobs"].items():
        print(f"{name}: {job['label']}")
    url = wait_for_dashboard_url(workspace)
    print(f"dashboard: {url}" if url else "dashboard: still starting (run `sq dashboard url` to check)")
    return 0


def _sync_resources_if_version_drifted(workspace: Workspace, command: str) -> None:
    """Refresh managed runtime assets before long-lived commands if the package changed.

    `start`, `tick`, and `loop` all rely on `.yaas/engine/current`; if pip upgraded the
    package without an explicit `sync-resources`, those commands would keep executing the
    stale tree forever. Drift detection stays cheap by reading only the symlink target.
    """
    if current_engine_version(workspace) == ENGINE_VERSION:
        return
    try:
        sync_resources(workspace)
    except Exception as exc:  # pragma: no cover - defensive logging seam
        # Warn and continue rather than abort. The trade is deliberate but not free: the
        # command proceeds against a stale engine, and tick.py points workers at
        # `.yaas/engine/current/...`, so they may read old instructions. Failing hard
        # instead would take the whole triage loop down for what is usually a permissions
        # problem in one directory, and a loop that stops is a loop nobody notices. If
        # this warning is ever seen in the wild, the sync failure is the bug to chase.
        print(f"warning: could not refresh engine resources before {command}: {exc}", file=sys.stderr)


def _cmd_stop(workspace: Workspace) -> int:
    from .launchd import LaunchdLifecycleError, stop_production

    try:
        stopped_production = stop_production(workspace)
        stopped_foreground = stop_dashboard_process(workspace)
        if not stopped_production and not stopped_foreground:
            print(f"no production jobs installed for instance {workspace.instance_id}")
            return 1
    except LaunchdLifecycleError as exc:
        print(f"could not stop Sidequestor instance {workspace.instance_id}: {exc}", file=sys.stderr)
        return 1
    print(f"stopped Sidequestor instance {workspace.instance_id}")
    return 0


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
    try:
        for _ in range(max(0, values.max_ticks)):
            code = run_native_tick(workspace) if values.isolated else dry_tick(workspace)
            if code:
                return code
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
    # Inspection is safe by default; foreground serving is an explicit escape hatch.
    action = args[0] if args else "url"
    if action == "url":
        url = read_dashboard_url(workspace)
        print(url or "dashboard is not running")
        return 0 if url else 1
    if action == "serve":
        parser = argparse.ArgumentParser(prog="yaas dashboard serve")
        parser.add_argument("port", nargs="?", type=int, default=8877)
        parser.add_argument("--port", dest="named_port", type=int)
        values = parser.parse_args(args[1:])
        return serve_dashboard(workspace, values.named_port if values.named_port is not None else values.port)
    raise SystemExit(f"unknown dashboard action: {action}")


def _cmd_migrate(path: str | None, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sidequestor migrate")
    parser.add_argument("legacy_name", nargs="?")
    parser.add_argument("--name")
    values = parser.parse_args(args)
    if values.legacy_name and values.name:
        parser.error("choose either positional NAME or --name NAME")
    selected = path or _workspace_from_environment()
    target = Path(selected).expanduser() if selected else Path.cwd()
    workspace_root = find_workspace_root(target) or target
    workspace, archive, changed = migrate_workspace(workspace_root, values.name or values.legacy_name)
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
        return _cmd_instances(args, workspace_path, instance)
    if command == "migrate":
        if instance:
            raise SystemExit("migrate accepts --workspace, not --instance")
        return _cmd_migrate(workspace_path, args)
    if command == "setup" and args == ["--manifest"]:
        return _cmd_setup_manifest()
    if command == "stop" and len(args) > 1:
        raise SystemExit("stop accepts at most one INSTANCE_ID")
    if command == "stop" and args and (workspace_path or instance):
        raise SystemExit("choose one of INSTANCE_ID, --workspace, or --instance")
    if command == "stop" and (args or instance):
        target = args[0] if args else instance
        matches = [row for row in list_instances()
                   if row.get("instance_id") == target or row.get("display_name") == target]
        if len(matches) != 1:
            raise SystemExit(f"instance not found or ambiguous: {target}")
        return _cmd_stop(load_workspace(matches[0]["path"]))
    workspace = _workspace(workspace_path, instance)
    if command == "doctor":
        return _cmd_doctor(workspace)
    if command == "sync-resources":
        print(f"synced engine resources: {sync_resources(workspace)}")
        return 0
    if command == "setup":
        return _cmd_setup(workspace, args)
    if command in {"start", "tick", "loop"}:
        _sync_resources_if_version_drifted(workspace, command)
    if command == "start":
        return _cmd_start(workspace)
    if command == "stop":
        return _cmd_stop(workspace)
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
        info = build_info()
        commit = f"{info['commit']}, " if info["commit"] else ""
        print(f"sidequestor {info['version']} ({commit}engine {info['engine']})")
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
