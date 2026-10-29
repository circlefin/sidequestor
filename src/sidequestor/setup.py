"""Interactive, non-destructive workspace onboarding."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .dashboard import wait_for_dashboard_url
from .job_path import find_agent, resolve_job_path
from .launchd import install_production
from .native import RUNTIME_ROOT
from .onboarding import find_quest as find_onboarding_quest
from .onboarding import run_onboard
from .quest_terminal import QuestTerminalError
from .resources import sync_resources
from .workspace import Workspace, set_default_workspace
from .setup_worker import setup_worker, verify_slack_helper, worker_environment


_PLACEHOLDERS = {None, "", "<set>", "CHANGE_ME", "TODO"}
_BACKENDS = {"claude", "codex", "cursor"}
_CLI_PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}
_MODEL_DEFAULTS = {
    "claude": "claude-opus-5-5",
    "codex": "gpt-6.1-sol",
    "cursor": "",
}
_ENV_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
_INTERACTIVE_INSTRUCTIONS = (
    "Before acting on a triage dispatch, read `.yaas/engine/current/OPERATING.md`.\n"
    "Engine-managed skills are installed under `.yaas/engine/current/skills/`."
)


def _value(lines: list[str], key: str) -> str | None:
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and match.group("key") == key:
            value = match.group("value").strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return value
    return None


def _set_missing(lines: list[str], values: dict[str, str]) -> list[str]:
    result = list(lines)
    positions = {}
    for index, line in enumerate(result):
        match = _ENV_LINE.match(line)
        if match:
            positions[match.group("key")] = index
    for key, value in values.items():
        index = positions.get(key)
        if index is not None:
            current = _value(result, key)
            if current not in _PLACEHOLDERS:
                continue
            result[index] = f"{key}={shlex.quote(value)}\n"
        else:
            if result and result[-1].strip():
                result.append("\n")
            result.append(f"{key}={shlex.quote(value)}\n")
    return result


def _prompt(label: str, default: str, *, input_fn=input) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input_fn(f"{label}{suffix}: ").strip()
    return answer or default


def _yes_no(label: str, default: bool, *, input_fn=input) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input_fn(f"{label} [{suffix}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def configured_agent(workspace: Workspace) -> str:
    """Return the selected backend without changing workspace state."""
    return _value(workspace.env_file.read_text().splitlines(), "SIDEQUESTOR_AGENT") or "codex"


def print_worker_instructions(agent: str) -> None:
    """Print optional interactive guidance without touching user-owned files."""
    filename = "CLAUDE.md" if agent == "claude" else "AGENTS.md"
    print()
    print(f"Optional: give your interactive sessions the same context, via {filename}.")
    print(f"Paste this in yourself when you want it — {filename} is yours and Sidequestor never touches it:")
    print("---")
    print(_INTERACTIVE_INSTRUCTIONS)
    print("---")


def provision_env(workspace: Workspace, *, input_fn=input, interactive: bool = True) -> tuple[dict[str, str], bool]:
    """Copy the template once and fill only empty/placeholder values."""
    example = workspace.root / ".env.example"
    env_file = workspace.env_file
    if not example.exists():
        sync_resources(workspace)
    if not env_file.exists():
        env_file.write_text(example.read_text())
        env_file.chmod(0o600)
    lines = env_file.read_text().splitlines(keepends=True)

    existing = {key: _value(lines, key) for key in (
        "SIDEQUESTOR_CHECKER_CONNECTORS", "SIDEQUESTOR_SLACK_CHECKERS_ENABLED", "SIDEQUESTOR_AGENT",
        "SIDEQUESTOR_CLAUDE_MODEL", "SIDEQUESTOR_CODEX_MODEL", "SIDEQUESTOR_CURSOR_MODEL",
        "SIDEQUESTOR_CLAUDE_EFFORT", "SIDEQUESTOR_CODEX_EFFORT",
        "SIDEQUESTOR_CLAUDE_PERMISSION_MODE", "SIDEQUESTOR_CODEX_PERMISSION_MODE",
        "SIDEQUESTOR_CURSOR_PERMISSION_MODE",
        "SLACK_APP_ID", "SLACK_CLIENT_ID", "SLACK_WORKSPACE_NAME", "SLACK_WORKSPACE_DOMAIN",
    )}
    values: dict[str, str] = {}

    connectors = existing["SIDEQUESTOR_CHECKER_CONNECTORS"] or "slack,email,github,jira"
    slack_selected = "slack" in {value.strip() for value in connectors.split(",")}
    slack_enabled = existing["SIDEQUESTOR_SLACK_CHECKERS_ENABLED"]
    if slack_enabled in _PLACEHOLDERS:
        enabled = False
        if slack_selected:
            enabled = _yes_no(
                "Do you want costless local Slack checking", True, input_fn=input_fn
            ) if interactive else False
            values["SIDEQUESTOR_SLACK_CHECKERS_ENABLED"] = "1" if enabled else "0"
    else:
        enabled = slack_selected and slack_enabled == "1"
        if interactive:
            print(f"Preserving existing Slack checker setting: {slack_enabled}")

    agent = existing["SIDEQUESTOR_AGENT"]
    if agent in _PLACEHOLDERS:
        agent = _prompt("Worker backend (claude, codex, or cursor)", "codex", input_fn=input_fn) if interactive else "codex"
        if agent not in _BACKENDS:
            raise SystemExit("worker backend must be claude, codex, or cursor")
        values["SIDEQUESTOR_AGENT"] = agent
    else:
        if interactive:
            print(f"Preserving existing worker backend: {agent}")
    if agent not in _BACKENDS:
        raise SystemExit("worker backend must be claude, codex, or cursor")

    model_key = f"SIDEQUESTOR_{agent.upper()}_MODEL"
    model_default = _MODEL_DEFAULTS[agent]
    model = existing.get(model_key)
    if model in _PLACEHOLDERS:
        prompt = "Worker model (blank uses Cursor's default)" if agent == "cursor" else "Worker model"
        model = _prompt(prompt, model_default, input_fn=input_fn) if interactive else model_default
        if model:
            values[model_key] = model

    # Cursor has no reasoning-effort flag exposed by Sidequestor.
    if agent != "cursor":
        effort_key = f"SIDEQUESTOR_{agent.upper()}_EFFORT"
        effort = existing.get(effort_key)
        if effort in _PLACEHOLDERS:
            effort = _prompt("Reasoning effort", "high", input_fn=input_fn) if interactive else "high"
            values[effort_key] = effort

    permission_key = f"SIDEQUESTOR_{agent.upper()}_PERMISSION_MODE"
    permission = existing.get(permission_key)
    if permission in _PLACEHOLDERS:
        # Kept as the default because restricted modes block helper scripts headless
        # workers must run. Cursor's bypass mode also disables its sandbox.
        print("Worker permission default is bypassPermissions: workers may run shell, file and "
              "network actions without approval while reading untrusted Slack, email "
              "and GitHub text. Cursor bypass mode also disables its sandbox. Use it only in a "
              "workspace where that behavior is acceptable.")
        permission = _prompt("Worker permission mode", "bypassPermissions", input_fn=input_fn) if interactive else "bypassPermissions"
        values[permission_key] = permission

    if enabled:
        for key in ("SLACK_APP_ID", "SLACK_CLIENT_ID", "SLACK_WORKSPACE_NAME", "SLACK_WORKSPACE_DOMAIN"):
            if existing[key] in _PLACEHOLDERS:
                if not interactive:
                    raise SystemExit(f"{key} is required when Slack checking is enabled")
                value = _prompt(key, "", input_fn=input_fn)
                if not value:
                    raise SystemExit(f"{key} is required when Slack checking is enabled")
                values[key] = value

    updated = _set_missing(lines, values)
    changed = updated != lines
    if changed:
        env_file.write_text("".join(updated))
        env_file.chmod(0o600)
    return values, changed


def check_worker_clis(workspace: Workspace, executable: Path, *, agent: str, input_fn=input,
                      interactive: bool = True) -> bool:
    """Report Codex and Claude CLIs, plus Cursor when selected."""
    selected_available = True
    job_path = resolve_job_path(workspace, executable)
    names = (*_CLI_PACKAGES, *(("cursor",) if agent == "cursor" else ()))
    for name in names:
        package = _CLI_PACKAGES.get(name)
        label = "Cursor" if name == "cursor" else name
        binary = find_agent(name, job_path.value)
        if binary:
            print(f"{label} CLI: installed ({binary})")
            continue
        print(f"{label} CLI: missing from the background-job PATH")
        install_succeeded = False
        if package and agent != "cursor" and interactive and _yes_no(
            f"Install {name} CLI with npm ({package})", False, input_fn=input_fn
        ):
            npm = shutil.which("npm", path=job_path.value) or shutil.which("npm")
            if not npm:
                print(f"npm is unavailable. Install Node.js and run: npm install -g {package}")
            else:
                try:
                    result = subprocess.run([npm, "install", "-g", package], cwd=workspace.root,
                                            check=False)
                except OSError as exc:
                    print(f"{name} CLI installation failed: {exc}")
                else:
                    if result.returncode:
                        print(f"{name} CLI installation failed (exit {result.returncode}).")
                    else:
                        install_succeeded = True
                        job_path = resolve_job_path(workspace, executable)
                        binary = find_agent(name, job_path.value)
                        if binary:
                            print(f"{name} CLI: installed ({binary})")
                        else:
                            try:
                                prefix = subprocess.run([npm, "prefix", "-g"], cwd=workspace.root,
                                                        capture_output=True, text=True, check=False)
                            except OSError:
                                pass
                            else:
                                if prefix.returncode == 0 and prefix.stdout.strip():
                                    print(f"npm installed {name}, but {prefix.stdout.strip()}/bin is not on"
                                          " the background-job PATH.")
        if not binary:
            if package and not install_succeeded:
                print(f"To install {name} later: npm install -g {package}")
            if name == agent:
                selected_available = False
                print(f"Selected worker CLI ({name}) is required before setup can start jobs."
                      " Set SIDEQUESTOR_PATH in .env if it is installed outside the job PATH.")
    return selected_available


def offer_onboarding(workspace: Workspace, *, slack_ready: bool, input_fn=input,
                     start=run_onboard) -> int:
    """Offer guided onboarding once setup is live; never start it without an answer."""
    existing = find_onboarding_quest(workspace, include_closed=True)
    if existing is not None:
        print(f"Onboarding already exists ({existing.name}). Use `sq onboard` to check or resume it.")
        return 0
    default = "slack" if slack_ready else "terminal"
    print()
    print("Guided onboarding reviews your setup, drafts your AGENTS.md and skills, and proposes "
          "quests, asking before each change. It can set up a private Slack channel so you "
          "can talk to Sidequestor without using the terminal.")
    while True:
        answer = _prompt("Start guided onboarding in [slack], [terminal], or [later]",
                         default, input_fn=input_fn).lower()
        if answer == "slack" and not slack_ready:
            print("Slack checkers are disabled. Choose terminal or later; enable Slack with sq setup first.")
            continue
        if answer in {"slack", "terminal", "later"}:
            break
        print("Please answer slack, terminal, or later.")
    if answer == "later":
        print("Start it any time with `sq onboard --slack` or `sq onboard --terminal`.")
        return 0
    try:
        return start(workspace, answer)
    except QuestTerminalError as exc:
        print(f"Onboarding could not start: {exc}")
        return 1


def run_setup(workspace: Workspace, executable: Path, *, input_fn=input, interactive: bool = True,
              start_onboarding=run_onboard) -> int:
    sync_resources(workspace)
    provision_env(workspace, input_fn=input_fn, interactive=interactive)
    env_lines = workspace.env_file.read_text().splitlines()
    agent = _value(env_lines, "SIDEQUESTOR_AGENT") or "codex"
    connectors = _value(env_lines, "SIDEQUESTOR_CHECKER_CONNECTORS") or "slack,email,github,jira"
    enabled = (
        "slack" in {value.strip() for value in connectors.split(",")}
        and _value(env_lines, "SIDEQUESTOR_SLACK_CHECKERS_ENABLED") == "1"
    )
    if interactive:
        print(f"Worker backend: {agent}. Good choice.")
        print("Setup will verify your worker login, selected model, and required Slack access.")
        if enabled:
            app_id = _value(env_lines, "SLACK_APP_ID")
            if app_id:
                print("Confirm Agents → Slack Model Context Protocol (MCP) Server is enabled before OAuth; enable it manually only if Slack did not apply the manifest setting:")
                print(f"https://api.slack.com/apps/{app_id}/app-assistant")
    slack_ready = False
    if enabled and interactive:
        try:
            _, environment = worker_environment(workspace, executable)
            slack_ready = verify_slack_helper(workspace, environment)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if enabled and interactive and not slack_ready and _yes_no("Connect Sidequestor to Slack now", True, input_fn=input_fn):
        script = RUNTIME_ROOT / "yaas-triage" / "setup" / "setup.sh"
        result = subprocess.run(["bash", str(script), "--workspace", str(workspace.root), "--oauth-only"], cwd=workspace.root)
        if result.returncode:
            return result.returncode
    if not check_worker_clis(workspace, executable, agent=agent, input_fn=input_fn,
                             interactive=interactive):
        return 1
    if not setup_worker(workspace, executable, interactive=interactive, input_fn=input_fn):
        print("Setup is incomplete. Rerun sq setup to continue; existing jobs and settings were preserved.")
        return 1
    set_default_workspace(workspace)
    manifest = install_production(workspace, executable, replace_runtime=True)
    print(f"You are live. Sidequestor instance {workspace.instance_id} is running these jobs:")
    for name, job in manifest["jobs"].items():
        print(f"{name}: {job['label']}")
    url = wait_for_dashboard_url(workspace)
    print(f"dashboard: {url}" if url else "dashboard: still starting (run `sq dashboard url` to check)")
    if interactive:
        print_worker_instructions(agent)
        # Last, because the terminal choice replaces this process with the agent.
        return offer_onboarding(workspace, slack_ready=enabled, input_fn=input_fn,
                                start=start_onboarding)
    return 0
