"""Interactive, non-destructive workspace onboarding."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from .launchd import install_production
from .native import RUNTIME_ROOT
from .resources import sync_resources
from .workspace import Workspace


_PLACEHOLDERS = {None, "", "<set>", "CHANGE_ME", "TODO"}
_ENV_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


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
        "SIDEQUESTOR_SLACK_CHECKERS_ENABLED", "SIDEQUESTOR_AGENT",
        "SIDEQUESTOR_CLAUDE_MODEL", "SIDEQUESTOR_CODEX_MODEL",
        "SIDEQUESTOR_CLAUDE_EFFORT", "SIDEQUESTOR_CODEX_EFFORT",
        "SIDEQUESTOR_CLAUDE_PERMISSION_MODE", "SIDEQUESTOR_CODEX_PERMISSION_MODE",
        "SLACK_APP_ID", "SLACK_CLIENT_ID", "SLACK_WORKSPACE_NAME", "SLACK_WORKSPACE_DOMAIN",
    )}
    values: dict[str, str] = {}

    slack_enabled = existing["SIDEQUESTOR_SLACK_CHECKERS_ENABLED"]
    if slack_enabled in _PLACEHOLDERS:
        enabled = _yes_no("Do you want costless local Slack checking", True, input_fn=input_fn) if interactive else False
        values["SIDEQUESTOR_SLACK_CHECKERS_ENABLED"] = "1" if enabled else "0"
    else:
        enabled = slack_enabled == "1"
        if interactive:
            print(f"Preserving existing Slack checker setting: {slack_enabled}")

    agent = existing["SIDEQUESTOR_AGENT"]
    if agent in _PLACEHOLDERS:
        agent = _prompt("Worker backend (claude or codex)", "codex", input_fn=input_fn) if interactive else "codex"
        if agent not in {"claude", "codex"}:
            raise SystemExit("worker backend must be claude or codex")
        values["SIDEQUESTOR_AGENT"] = agent
    else:
        if interactive:
            print(f"Preserving existing worker backend: {agent}")

    model_key = f"SIDEQUESTOR_{agent.upper()}_MODEL"
    effort_key = f"SIDEQUESTOR_{agent.upper()}_EFFORT"
    permission_key = f"SIDEQUESTOR_{agent.upper()}_PERMISSION_MODE"
    model = existing.get(model_key)
    if model in _PLACEHOLDERS:
        model = _prompt("Worker model", "claude-opus-4-6" if agent == "claude" else "gpt-5", input_fn=input_fn) if interactive else ("claude-opus-4-6" if agent == "claude" else "gpt-5")
        values[model_key] = model
    effort = existing.get(effort_key)
    if effort in _PLACEHOLDERS:
        effort = _prompt("Reasoning effort", "medium", input_fn=input_fn) if interactive else "medium"
        values[effort_key] = effort
    permission = existing.get(permission_key)
    if permission in _PLACEHOLDERS:
        permission = _prompt("Worker permission mode", "workspace-write", input_fn=input_fn) if interactive else "workspace-write"
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


def run_setup(workspace: Workspace, executable: Path, *, input_fn=input, interactive: bool = True) -> int:
    sync_resources(workspace)
    provision_env(workspace, input_fn=input_fn, interactive=interactive)
    enabled = _value(workspace.env_file.read_text().splitlines(), "SIDEQUESTOR_SLACK_CHECKERS_ENABLED") == "1"
    if enabled and interactive and _yes_no("Run Slack OAuth now", True, input_fn=input_fn):
        script = RUNTIME_ROOT / "yaas-triage" / "setup" / "setup.sh"
        result = subprocess.run(["bash", str(script), "--workspace", str(workspace.root), "--oauth-only"], cwd=workspace.root)
        if result.returncode:
            return result.returncode
    manifest = install_production(workspace, executable)
    print(f"started Sidequestor instance {workspace.instance_id}")
    for name, job in manifest["jobs"].items():
        print(f"{name}: {job['label']}")
    return 0
