"""The public Sidequestor command shell."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .build_info import build_info
from .native import _environment, _load_workspace_env, dry_tick, run_native, run_native_loop, run_native_tick
from .dashboard import (
    read_dashboard_port,
    read_dashboard_url,
    serve as serve_dashboard,
    stop_dashboard_process,
    wait_for_dashboard_port,
    wait_for_dashboard_url,
)
from .isolated import run_isolated
from .launchd import install as install_jobs
from .launchd import (
    LaunchdLifecycleError,
    install_production,
    production_agent_status,
    production_is_running,
    production_status,
    stop_production,
    uninstall_production,
)
from .launchd import render, status as launchd_status, uninstall as uninstall_jobs
from .migrations import migrate_workspace
from . import credential_setup, onboarding, quest_terminal
from .resources import ENGINE_VERSION, current_engine_version, sync_resources
from .setup import configured_agent, print_worker_instructions, run_setup
from .upgrade import run_upgrade
from .update_check import available_update, check_enabled as update_check_enabled
from .update_check import read_status as read_update_status
from .workspace import (
    Workspace,
    find_workspace,
    find_workspace_root,
    init_workspace,
    list_instances,
    load_workspace,
    register_workspace,
    default_workspace,
    set_default_workspace,
    rekey_workspace,
    validate_workspace,
)


COMMANDS = {
    "init": "create a workspace",
    "instances": "list and validate workspace instances",
    "setup": "run the interactive workspace onboarding wizard",
    "onboard": "start or resume guided onboarding in Slack or this terminal",
    "start": "start all jobs for a workspace",
    "stop": "stop all jobs for an instance",
    "pause": "pause a workspace now (alias for stop)",
    "resume": "resume a workspace (alias for start)",
    "tick": "run one triage tick",
    "loop": "run the paced triage loop",
    "dashboard": "serve or inspect the dashboard",
    "doctor": "validate a workspace and its engine",
    "status": "show the default workspace, jobs, dashboard and worker readiness",
    "migrate": "apply workspace schema migrations",
    "sync-resources": "refresh managed engine resources",
    "upgrade": "upgrade the package and refresh resources when in a workspace",
    "credentials": "inspect or repair the Keychain helper, or store a Jira/Notion token",
    "new-quest": "scaffold a quest folder from a JSON spec",
    "watch": "manage watches",
    "ack": "acknowledge dispatched work",
    "approval": "manage approval state",
    "log": "write a timeline event",
    "slack-send": "send through the Slack surface",
    "slack-channel": "create private Slack channels, invite members, or set channel details",
    "slack-file": "upload a file to Slack, or fetch a message's attached files",
    "telegram-send": "save a Telegram draft or explicitly send a message",
    "react": "advance reaction lifecycle",
    "mcp-call": "call an MCP surface",
    "jira-call": "call a Jira surface",
    "telegram-auth": "authorize or inspect a Telegram user session",
    "x-auth": "authorize or inspect an X user account",
    "x-send": "perform an X action as the authorized user",
    "gdoc-comment": "add verified text-anchored Google Doc comments",
    **quest_terminal.COMMANDS,
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
    "slack-file": "yaas-triage/surfaces/slack-file.py",
    "slack-channel": "yaas-triage/surfaces/slack-channel.py",
    "telegram-send": "yaas-triage/surfaces/telegram-send.py",
    "telegram-auth": "yaas-triage/surfaces/telegram_credentials.py",
    "x-auth": "yaas-triage/surfaces/x_credentials.py",
    "x-send": "yaas-triage/surfaces/x-send.py",
    "gdoc-comment": "yaas-triage/skills/yaas-gdoc-anchored-comments/gdoc-comment.py",
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
        "setup": "sidequestor [--workspace PATH] setup [--select-workspace|--instructions|--manifest|--production] [--non-interactive|--render-only|install|status|uninstall]",
        "onboard": "sidequestor [--workspace PATH] onboard [--slack|--slack-dm|--slack-channel CHANNEL_ID|--terminal] [--codex|--claude|--cursor]",
        "start": "sidequestor [--workspace PATH] start [--dashboard-port PORT]",
        "stop": "sidequestor [--workspace PATH] stop [INSTANCE_ID]",
        "pause": "sidequestor [--workspace PATH] pause [INSTANCE_ID]",
        "resume": "sidequestor [--workspace PATH] resume [--dashboard-port PORT]",
        "tick": "sidequestor [--workspace PATH] tick [--dry-run|--isolated [--fake-worker]]",
        "loop": "sidequestor [--workspace PATH] loop [--max-ticks N]",
        "dashboard": "sidequestor [--workspace PATH] dashboard [open|serve|url]",
        "migrate": "sidequestor [--workspace PATH] migrate [NAME|--name NAME]",
        "upgrade": "sidequestor [--workspace PATH] upgrade [--source GITHUB_URL --ref REF] [--pre] [--yes] [--no-restart]",
        "credentials": "sidequestor [--workspace PATH] credentials status|repair-keychain\n       sidequestor [--workspace PATH] credentials set-token jira|notion",
        "watch": "sidequestor [--workspace PATH] watch [--allow-dormant] QUEST_ID WATCH_JSON\n       sidequestor [--workspace PATH] watch retire QUEST_ID WATCH_ID REASON",
        "new-quest": "sidequestor [--workspace PATH] new-quest [--allow-dormant] SPEC_JSON",
        "slack-channel": "sidequestor [--workspace PATH] slack-channel create|invite|topic|purpose|approval-spec '<json>'",
        "slack-file": "sidequestor [--workspace PATH] slack-file send|approval-spec|fetch '<json>'",
        "telegram-send": "sidequestor [--workspace PATH] telegram-send --peer @name --message \"hello\" [--send] [--quest-id QUEST_ID] [--reply-to-message-id N] [--credential-id ID] [--idempotency-key KEY]",
        "telegram-auth": "sidequestor [--workspace PATH] telegram-auth authorize [API_ID] [CREDENTIAL_ID]\n       API_ID defaults to TELEGRAM_API_ID from the workspace .env\n       sidequestor [--workspace PATH] telegram-auth status [CREDENTIAL_ID]",
        "x-auth": "sidequestor [--workspace PATH] x-auth authorize CLIENT_ID [CREDENTIAL_ID]\n       sidequestor [--workspace PATH] x-auth status [CREDENTIAL_ID]\n       sidequestor [--workspace PATH] x-auth revoke [CREDENTIAL_ID]",
        "x-send": "sidequestor [--workspace PATH] x-send ACTION [OPTIONS]\n       sidequestor [--workspace PATH] x-send '{\"action\":\"post\",\"text\":\"hello\"}'",
        "gdoc-comment": "sidequestor [--workspace PATH] gdoc-comment [approval-spec] '<payload_json>'",
        **quest_terminal.USAGE,
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
    default = default_workspace()
    if default is not None:
        return default
    workspace = find_workspace()
    register_workspace(workspace)
    return workspace


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
    # Advisory only: `sq upgrade` gates restarts on doctor, and a missing agent CLI
    # is fixed by the user, not by keeping jobs stopped.
    agent = production_agent_status(workspace)
    if agent is not None:
        name, binary = agent
        print(f"agent: ok ({binary})" if binary else
              f"agent: warning ({name} CLI is not on the PATH recorded for background jobs)")
    from .worker_readiness import current_state
    worker = current_state(workspace)
    for scope in ("blocked", "slack"):
        if worker.get(scope):
            print(f"worker: paused — {worker[scope]}")
    print(f"sidequestor {info['version']}{suffix}")
    # Advisory, from the heartbeat's cached check; doctor itself never touches the network.
    update = (available_update(read_update_status(workspace.root), info["version"])
              if update_check_enabled(_load_workspace_env(dict(os.environ), workspace)) else None)
    if update:
        print(f"update: {update['latest']} available (run 'sq upgrade')")
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
            helper_code = _ensure_keychain_helper(workspace)
            if helper_code:
                return helper_code
            manifest = install_production(workspace, Path(sys.executable), replace_runtime=True)
            print(f"installed production jobs for {manifest['workspace']}")
            for name, job in manifest["jobs"].items():
                print(f"{name}: {job['label']}")
            _report_agent(manifest)
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


def _cmd_onboard(workspace: Workspace, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sidequestor onboard")
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument("--slack", dest="channel", action="store_const", const="slack")
    channel.add_argument("--terminal", dest="channel", action="store_const", const="terminal")
    channel.add_argument("--slack-channel", metavar="CHANNEL_ID")
    channel.add_argument("--slack-dm", action="store_true")
    agent = parser.add_mutually_exclusive_group()
    for name in quest_terminal.AGENT_ORDER:
        agent.add_argument(f"--{name}", dest="agent", action="store_const", const=name)
    parsed = parser.parse_args(args)
    try:
        options = {"slack_channel_id": parsed.slack_channel}
        if parsed.slack_dm:
            options["slack_dm"] = True
        return onboarding.run_onboard(workspace, parsed.channel, parsed.agent, **options)
    except quest_terminal.QuestTerminalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _slack_credentials_enabled(workspace: Workspace) -> bool:
    environment = _environment(workspace)
    return environment.get("SIDEQUESTOR_SLACK_CHECKERS_ENABLED") == "1"


def _ensure_keychain_helper(workspace: Workspace) -> int:
    if not _slack_credentials_enabled(workspace):
        return 0
    code = run_native(
        workspace,
        "yaas-triage/surfaces/slack_credentials.py",
        ["repair-keychain", "--quiet"],
    )
    if code:
        print(
            "Sidequestor was not started because its Keychain helper migration "
            "did not complete. Run `sq credentials repair-keychain` in a terminal.",
            file=sys.stderr,
        )
    return code


def _cmd_credentials(workspace: Workspace, args: list[str]) -> int:
    if args and args[0] == "set-token":
        # Parsed by hand: argparse echoes unrecognized arguments, which would print
        # a token someone pasted on the command line.
        if len(args) != 2 or args[1] not in credential_setup.INTEGRATIONS:
            print("usage: sidequestor credentials set-token jira|notion\n"
                  "The token is read from a hidden prompt, never from arguments.",
                  file=sys.stderr)
            return 2
        return credential_setup.set_token(args[1], _environment(workspace))
    parser = argparse.ArgumentParser(prog="sidequestor credentials")
    parser.add_argument("action", choices=("status", "repair-keychain"))
    values = parser.parse_args(args)
    surface = "yaas-triage/surfaces/slack_credentials.py"
    if values.action == "status":
        return run_native(workspace, surface, ["helper-status"])

    manifest = production_status(workspace)
    was_running = bool(manifest and manifest.get("running"))
    dashboard_port = read_dashboard_port(workspace) if was_running else None
    if was_running:
        try:
            if not stop_production(workspace):
                print("could not stop the Sidequestor jobs before repair", file=sys.stderr)
                return 1
        except LaunchdLifecycleError as exc:
            print(f"could not stop Sidequestor before repair: {exc}", file=sys.stderr)
            return 1
        print(f"Stopped Sidequestor instance {workspace.instance_id} for Keychain repair.")

    code = run_native(workspace, surface, ["repair-keychain"])
    if code:
        if was_running:
            print(
                "Keychain repair did not complete; Sidequestor remains stopped.",
                file=sys.stderr,
            )
        return code

    if was_running:
        if dashboard_port is not None and not wait_for_dashboard_port(dashboard_port):
            print(
                f"Keychain repair completed, but dashboard port {dashboard_port} is "
                "still in use; Sidequestor remains stopped.",
                file=sys.stderr,
            )
            return 1
        start_args = (["--dashboard-port", str(dashboard_port)]
                      if dashboard_port is not None else [])
        return _cmd_start(workspace, start_args)
    return 0


def _cmd_start(workspace: Workspace, args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sidequestor start")
    parser.add_argument(
        "--dashboard-port", type=int,
        help="bind the dashboard to a specific loopback port",
    )
    values = parser.parse_args(args or [])
    if values.dashboard_port is not None and not 1 <= values.dashboard_port <= 65535:
        parser.error("--dashboard-port must be between 1 and 65535")
    from .setup_worker import report_worker_version
    report_worker_version(workspace, Path(sys.executable))
    helper_code = _ensure_keychain_helper(workspace)
    if helper_code:
        return helper_code
    try:
        manifest = install_production(
            workspace, Path(sys.executable), values.dashboard_port or 0,
        )
    except LaunchdLifecycleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"started Sidequestor instance {workspace.instance_id}")
    for name, job in manifest["jobs"].items():
        print(f"{name}: {job['label']}")
    _report_agent(manifest)
    url = wait_for_dashboard_url(workspace)
    print(f"dashboard: {url}" if url else "dashboard: still starting (run `sq dashboard url` to check)")
    return 0


def _report_agent(manifest: dict) -> None:
    agent = manifest.get("agent")
    if not isinstance(agent, dict) or not agent.get("name"):
        return
    if agent.get("binary"):
        print(f"agent: {agent['name']} ({agent['binary']})")
        return
    print(
        f"warning: the {agent['name']} CLI was not found on the PATH recorded for background "
        "jobs. Install it, or set SIDEQUESTOR_PATH in .env to a PATH that contains it, then "
        "run `sq start` again.",
        file=sys.stderr,
    )


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
            return 0
    except LaunchdLifecycleError as exc:
        print(f"could not stop Sidequestor instance {workspace.instance_id}: {exc}", file=sys.stderr)
        return 1
    print(f"stopped Sidequestor instance {workspace.instance_id}")
    return 0


def _cmd_loop(workspace: Workspace, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="yaas loop")
    parser.add_argument("--max-ticks", type=int)
    parser.add_argument("--isolated", action="store_true")
    configured_env = _environment(workspace)
    default_interval = configured_env.get(
        "YAAS_TRIAGE_INTERVAL",
        configured_env.get("YAAS_LOOP_INTERVAL", "60"),
    )
    try:
        parsed_default = float(default_interval)
        if not math.isfinite(parsed_default) or parsed_default <= 0:
            raise ValueError
    except (TypeError, ValueError):
        parsed_default = 60.0

    def positive_interval(value: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError("interval must be a positive number") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise argparse.ArgumentTypeError("interval must be a positive number")
        return parsed

    parser.add_argument("--interval", type=positive_interval, default=parsed_default)
    values, unknown = parser.parse_known_args(args)
    if unknown:
        raise SystemExit(f"unknown loop arguments: {' '.join(unknown)}")
    if values.isolated and values.max_ticks is None:
        return run_native_loop(workspace, max(0.01, values.interval))
    if values.max_ticks is None:
        return run_native(
            workspace,
            "yaas-triage/triage-loop.sh",
            [],
            extra_env={"YAAS_TRIAGE_INTERVAL": str(values.interval)},
        )
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
    action = args[0] if args else "open"
    if action in {"url", "open"}:
        url = read_dashboard_url(workspace)
        print(url or "dashboard is not running (run sq start)")
        if action == "open" and url:
            if read_dashboard_port(workspace) is None:
                raise SystemExit("Dashboard URL is not a valid loopback address. Run sq setup.")
            try:
                if sys.platform == "darwin":
                    return subprocess.run(["open", url], check=False).returncode
                import webbrowser
                return 0 if webbrowser.open(url) else 1
            except OSError as exc:
                print(f"Could not open the dashboard: {exc}", file=sys.stderr)
                return 1
        return 0 if url else 1
    if action == "serve":
        parser = argparse.ArgumentParser(prog="yaas dashboard serve")
        parser.add_argument("port", nargs="?", type=int, default=0)
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
    # Vacation-friendly names for the existing lifecycle, not a second scheduler
    # or a new state machine. Preserve all targeting, safety checks and failures.
    command = {"pause": "stop", "resume": "start"}.get(command, command)
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
    if (command == "upgrade" and not (workspace_path or instance or _workspace_from_environment())
            and find_workspace_root() is None):
        return run_upgrade(None, args)
    if command == "setup" and args == ["--select-workspace"]:
        rows = list_instances()
        if not rows:
            raise SystemExit("No registered workspaces. Run sq --workspace PATH setup.")
        for index, row in enumerate(rows, 1):
            print(f"{index}. {row.get('display_name')} ({row.get('path')})")
        try:
            choice = int(input("Default workspace number: "))
            if not 1 <= choice <= len(rows):
                raise ValueError()
            selected = load_workspace(rows[choice - 1]["path"])
        except (ValueError, EOFError):
            raise SystemExit("No default selected.")
        set_default_workspace(selected)
        print(f"Default workspace: {selected.root}")
        return 0
    workspace = _workspace(workspace_path, instance)
    register_workspace(workspace)
    if command in {"start", "stop", "setup", "doctor", "status", "dashboard"}:
        print(f"Workspace: {workspace.display_name} ({workspace.root})",
              file=sys.stderr if command == "dashboard" else sys.stdout)
    if command in {"start", "upgrade"} or (command == "setup" and args in ([], ["--non-interactive"])):
        from .launchd import start_executable, runtime_environment
        try:
            registered = start_executable(workspace, Path(sys.executable))
        except LaunchdLifecycleError as exc:
            if command != "setup":
                print(str(exc), file=sys.stderr)
                return 1
            print("Setup will repair this workspace's unavailable runtime using the current installation.")
            registered = Path(os.path.abspath(sys.executable))
        forwarded_key = workspace.instance_id + ":" + str(registered)
        # Interpreter launchers may report a different sys.executable spelling.
        # Do not collapse symlinks across distinct venvs; forward at most once.
        if (str(registered) != os.path.abspath(sys.executable) and
                os.environ.get("SIDEQUESTOR_RUNTIME_FORWARDED") != forwarded_key):
            from .dashboard import saved_dashboard_port
            forwarded = list(args)
            if command == "start" and not any(arg == "--dashboard-port" or arg.startswith("--dashboard-port=") for arg in forwarded):
                forwarded.extend(["--dashboard-port", str(saved_dashboard_port(workspace))])
            environment = runtime_environment()
            environment["SIDEQUESTOR_RUNTIME_FORWARDED"] = forwarded_key
            return subprocess.run([str(registered), "-P", "-m", "sidequestor", "--workspace", str(workspace.root),
                                   command, *forwarded], env=environment, check=False).returncode
    if command == "status":
        print("Jobs: running" if production_is_running(workspace) else "Jobs: stopped")
        from .dashboard import saved_dashboard_port
        print(f"Dashboard: http://localhost:{saved_dashboard_port(workspace)}")
        return _cmd_doctor(workspace)
    if command == "doctor":
        return _cmd_doctor(workspace)
    if command == "sync-resources":
        print(f"synced engine resources: {sync_resources(workspace)}")
        return 0
    if command == "upgrade":
        return run_upgrade(workspace, args)
    if command == "credentials":
        return _cmd_credentials(workspace, args)
    if command == "setup":
        return _cmd_setup(workspace, args)
    if command == "onboard":
        return _cmd_onboard(workspace, args)
    if command in {"start", "tick", "loop"}:
        _sync_resources_if_version_drifted(workspace, command)
    if command == "start":
        return _cmd_start(workspace, args)
    if command == "stop":
        return _cmd_stop(workspace)
    if command == "tick":
        return _cmd_tick(workspace, args)
    if command == "loop":
        return _cmd_loop(workspace, args)
    if command == "dashboard":
        return _cmd_dashboard(workspace, args)
    if command in quest_terminal.COMMANDS:
        return quest_terminal.run_command(workspace, command, args)
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
