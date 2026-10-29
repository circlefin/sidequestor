"""Foreground worker authentication and verification orchestrated by sq setup."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .job_path import resolve_job_path
from .native import RUNTIME_ROOT, _apply_env_aliases, _load_workspace_env
from .worker_readiness import auth_check, invoke, invocation_failure, save_state, slack_helper_ready, SLACK_ACCESS_ERROR
from .workspace import workspace_command


def worker_environment(workspace, executable):
    from .launchd import _production_jobs
    path = resolve_job_path(workspace, executable)
    environment = dict(_production_jobs(workspace, executable, 8877, path)["triage"]["values"]["EnvironmentVariables"])
    return path, _apply_env_aliases(_load_workspace_env(environment, workspace))


def update_command(agent, binary, environment):
    real = str(Path(binary).resolve())
    brew = shutil.which("brew", path=environment["PATH"])
    if "/Cellar/" in real or "/Caskroom/" in real:
        if brew:
            package = "codex" if agent == "codex" else "claude-code@latest" if "claude-code@latest" in real else "claude-code"
            return [brew, "upgrade", *(["--cask"] if "/Caskroom/" in real else []), package]
        return None
    if "node_modules" in real:
        npm = shutil.which("npm", path=environment["PATH"])
        if npm:
            package = "@openai/codex" if agent == "codex" else "@anthropic-ai/claude-code"
            return [npm, "install", "-g", package + "@latest"]
        return None
    if agent == "claude":
        return [binary, "update"]
    try:
        help_result = subprocess.run([binary, "update", "--help"], env=environment,
                                     capture_output=True, text=True, timeout=5)
        if help_result.returncode == 0 and "Update Codex" in help_result.stdout:
            return [binary, "update"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def setup_worker(workspace, executable, *, interactive=True, input_fn=input):
    path, environment = worker_environment(workspace, executable)
    agent, binary = path.agent, path.agent_binary
    if not binary:
        print(f"Selected worker CLI is unavailable. Check SIDEQUESTOR_PATH and rerun {workspace_command(workspace, 'setup')}.")
        return False
    if agent not in {"claude", "codex"}:
        return True
    try:
        version = invoke([binary, "--version"], workspace, environment)
        print(f"Worker CLI: {version.stdout.strip()} ({binary})")
        if interactive and input_fn("Check/install the latest worker CLI release? [y/N]: ").strip().lower() in {"y", "yes"}:
            from .launchd import production_status
            if (production_status(workspace) or {}).get("running"):
                print(f"Stop Sidequestor with {workspace_command(workspace, 'stop')} before updating its worker CLI, then rerun {workspace_command(workspace, 'setup')}. Existing jobs were left running.")
                return False
            command = update_command(agent, binary, environment)
            if command is None:
                print(f"CLI installation is unmanaged. Update it with its original installer, then rerun {workspace_command(workspace, 'setup')}.")
                return False
            if invoke(command, workspace, environment, timeout=180, interactive=True).returncode:
                print(f"CLI update failed. Existing settings were preserved; rerun {workspace_command(workspace, 'setup')} after resolving it.")
                return False
            path, environment = worker_environment(workspace, executable)
            binary = path.agent_binary
            if not binary:
                return False
            version = invoke([binary, "--version"], workspace, environment)
            print(f"Worker CLI after update: {version.stdout.strip()} ({binary})")
        reason = auth_check(workspace, agent, binary, environment)
        if reason:
            save_state(workspace, agent, binary, environment, reason)
            if not interactive:
                print(reason.replace("Run sq setup.", f"Run {workspace_command(workspace, 'setup')}."))
                return False
            print("Connect your AI account in the login flow; setup will continue afterwards.")
            command = [binary, "auth", "login"] if agent == "claude" else [binary, "login"]
            if invoke(command, workspace, environment, timeout=300, interactive=True).returncode:
                return False
            if auth_check(workspace, agent, binary, environment):
                print(f"AI login was not verified. Rerun {workspace_command(workspace, 'setup')} to continue.")
                return False
        model = environment.get(f"YAAS_{agent.upper()}_MODEL", "")
        if agent == "claude":
            command = [binary, "-p", "Reply only READY. Do not use tools.", "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format", "json"]
        else:
            command = [binary, "exec", "--skip-git-repo-check", "-s", "read-only", "-c", "approval_policy=never", "-c", 'plugins."slack@openai-curated".enabled=false', "Reply only READY. Do not use tools."]
        if model:
            command += ["--model", model]
        result = invoke(command, workspace, environment, timeout=90)
        text = result.stdout + result.stderr
        failed = result.returncode != 0
        if agent == "claude":
            try:
                failed = failed or json.loads(result.stdout).get("is_error", False)
            except (ValueError, AttributeError):
                failed = True
        if failed:
            reason = invocation_failure(workspace, agent, binary, environment, text, trusted_cli=True, direct_auth=True)
            if reason:
                print(reason.replace("Run sq setup.", f"Run {workspace_command(workspace, 'setup')}."))
            else:
                print(f"Model verification did not complete. Check connection/quota and rerun {workspace_command(workspace, 'setup')}.")
            return False
        save_state(workspace, agent, binary, environment)
        print("AI login and selected model: verified")
        if agent == "claude":
            return verify_claude_slack(workspace, binary, environment, interactive=interactive)
        if verify_slack_helper(workspace, environment):
            save_state(workspace, agent, binary, environment, scope="slack")
            return True
        return False
    except KeyboardInterrupt:
        print(f"\nSetup interrupted. Rerun {workspace_command(workspace, 'setup')} to continue.")
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Worker verification could not complete ({type(exc).__name__}). Rerun {workspace_command(workspace, 'setup')}.")
        return False


def slack_required(environment):
    return (environment.get("YAAS_SLACK_CHECKERS_ENABLED") == "1" and
            "slack" in environment.get("YAAS_CHECKER_CONNECTORS", "slack,email,github,jira").split(","))


def slack_identity(workspace, environment):
    result = invoke([sys.executable, str(RUNTIME_ROOT / "yaas-triage/surfaces/client.py"), "slack-identity"], workspace, environment)
    try:
        return json.loads(result.stdout).get("user_id")
    except (ValueError, AttributeError):
        return None


def verify_slack_helper(workspace, environment):
    if not slack_required(environment):
        return True
    if not slack_helper_ready(workspace, environment):
        print(f"Sidequestor Slack read could not be verified. Check the connection and credentials, then rerun {workspace_command(workspace, 'setup')}.")
        return False
    print("Sidequestor Slack access: verified")
    return True


def claude_slack_status(text):
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init":
            for server in record.get("mcp_servers", []) or []:
                if server.get("name") == "slack":
                    return server.get("status")
    return None


def verify_claude_slack(workspace, binary, environment, *, interactive):
    """Verify the supported worker helper; native Claude MCP OAuth is optional."""
    if not slack_required(environment):
        save_state(workspace, "claude", binary, environment, scope="slack")
        return True
    if verify_slack_helper(workspace, environment):
        save_state(workspace, "claude", binary, environment, scope="slack")
        print("Claude worker Slack access: verified through Sidequestor helper")
        return True
    save_state(workspace, "claude", binary, environment,
               SLACK_ACCESS_ERROR,
               scope="slack")
    return False


def _report_worker_version(workspace, executable):
    """Advisory startup check. Never updates software or blocks a working service."""
    path, environment = worker_environment(workspace, executable)
    if not path.agent_binary or path.agent not in {"claude", "codex"}:
        return
    try:
        result = invoke([path.agent_binary, "--version"], workspace, environment, timeout=5)
        print(f"Worker CLI: {result.stdout.strip()} ({path.agent_binary})")
        npm = shutil.which("npm", path=environment["PATH"])
        if not npm:
            return
        package = "@openai/codex" if path.agent == "codex" else "@anthropic-ai/claude-code"
        latest = invoke([npm, "view", package, "version", "--fetch-retries=0", "--fetch-timeout=2000"],
                        workspace, environment, timeout=3)
        import re
        installed = re.search(r"\d+\.\d+\.\d+", result.stdout)
        available = re.fullmatch(r"\d+\.\d+\.\d+", latest.stdout.strip())
        if installed and available:
            parse = lambda value: tuple(map(int, value.split(".")))
            if parse(available.group()) > parse(installed.group()):
                print(f"Worker CLI update available ({available.group()}). Run {workspace_command(workspace, 'setup')} to review/update it.")
    except (OSError, subprocess.TimeoutExpired):
        pass


def report_worker_version(workspace, executable):
    try:
        _report_worker_version(workspace, executable)
    except Exception:
        pass  # Startup advice must never prevent starting a healthy installation.
