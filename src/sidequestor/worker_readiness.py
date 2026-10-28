"""Bounded worker checks; only proven credential/configuration failures block work."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .workspace import Workspace, _write_json, workspace_lock

AUTH_ERROR = re.compile(r"not logged in|please (?:log|sign) in|authentication required|invalid api key|oauth token (?:has )?expired|token_revoked|invalid_auth|not_authed|authentication_error|unauthorized", re.I)
COMPAT_ERROR = re.compile(r"unknown (?:option|argument)|unrecognized (?:option|argument|model)|unexpected argument|model .{0,100}(?:not found|does not exist|not supported|not recognized)", re.I)


def invoke(arguments, workspace, environment, timeout=30, *, interactive=False):
    """Kill the subprocess group on timeout, including any pending MCP children."""
    if interactive:
        # Preserve the controlling terminal and let Ctrl-C reach login/package-manager UI.
        return subprocess.run(arguments, cwd=workspace.root, env=environment, check=False)
    proc = subprocess.Popen(arguments, cwd=workspace.root, env=environment,
                            stdin=None if interactive else subprocess.DEVNULL,
                            stdout=None if interactive else subprocess.PIPE,
                            stderr=None if interactive else subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(arguments, proc.returncode, out or "", err or "")


def fingerprint(agent, binary, environment):
    keys = ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "YAAS_CLAUDE_MODEL", "YAAS_CODEX_MODEL",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
    identity = [agent, str(Path(binary).resolve()), *[environment.get(k, "") for k in keys]]
    identity.extend(environment.get(k, "") for k in
                    ("CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                     "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"))
    try:
        identity.append(str(Path(binary).stat().st_mtime_ns))
    except OSError:
        pass
    config = Path(__file__).parent / "runtime/yaas-triage/dispatch/worker.mcp.json"
    if agent == "claude":
        try:
            identity.append(hashlib.sha256(config.read_bytes()).hexdigest())
        except OSError:
            pass
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def state_path(workspace):
    return workspace.state / "triage" / "worker-readiness.json"


def read_state(workspace):
    try:
        value = json.loads(state_path(workspace).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(workspace, agent, binary, environment, reason=None, *, scope="blocked"):
    with workspace_lock(workspace, "worker-readiness"):
        previous = read_state(workspace)
        record = dict(previous) if previous.get("fingerprint") == fingerprint(agent, binary, environment) else {}
        record.update(agent=agent, fingerprint=fingerprint(agent, binary, environment), checked_at=time.time())
        if reason:
            record[scope] = reason
        else:
            record.pop(scope, None)
        _write_json(state_path(workspace), record)

def auth_check(workspace, agent, binary, environment):
    if agent not in {"claude", "codex"}:
        return None
    external = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN") if agent == "claude" else ("OPENAI_API_KEY", "CODEX_API_KEY")
    if any(environment.get(k) for k in external):
        return None  # Account-status commands do not reliably report environment auth.
    if agent == "claude" and any(environment.get(k, "").lower() in {"1", "true"} for k in
                                  ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        return None
    args = [binary, "auth", "status"] if agent == "claude" else [binary, "login", "status"]
    try:
        cp = invoke(args, workspace, environment, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None  # An unavailable check is not proof of invalid credentials.
    text = cp.stdout + cp.stderr
    if agent == "claude":
        try:
            status = json.loads(cp.stdout)
            if status.get("authMethod") in {"third_party", "api_key", "api_key_helper", "oauth_token"}:
                return None
            if status.get("loggedIn") is False:
                return "AI account is not connected. Run sq setup."
        except (ValueError, AttributeError):
            pass
    if cp.returncode and AUTH_ERROR.search(text):
        return "AI account is not connected. Run sq setup."
    return None


def blocked_reason(workspace, agent, binary, environment, *, needs_slack=False):
    record = read_state(workspace)
    if record.get("fingerprint") != fingerprint(agent, binary, environment):
        return None
    return record.get("blocked") or (record.get("slack") if needs_slack else None)


def invocation_failure(workspace, agent, binary, environment, text, *, trusted_cli=False, slack=False, direct_auth=False):
    """Diagnostics are hints; confirm AI credentials before persisting an auth block."""
    if not slack and AUTH_ERROR.search(text):
        reason = auth_check(workspace, agent, binary, environment)
        if not reason and direct_auth:
            reason = "AI account could not authenticate. Run sq setup to reconnect."
        if reason:
            save_state(workspace, agent, binary, environment, reason)
            return reason
    if trusted_cli and COMPAT_ERROR.search(text):
        reason = "Worker CLI/model configuration needs attention. Run sq setup to check CLI updates and model access."
        save_state(workspace, agent, binary, environment, reason)
        return reason
    return None


def preflight(workspace, agent, binary, environment, *, needs_slack=False):
    reason = blocked_reason(workspace, agent, binary, environment, needs_slack=needs_slack)
    if reason:
        return reason
    record = read_state(workspace)
    if (record.get("fingerprint") == fingerprint(agent, binary, environment)
            and 0 <= time.time() - record.get("checked_at", 0) < 60):
        return None
    reason = auth_check(workspace, agent, binary, environment)
    save_state(workspace, agent, binary, environment, reason)
    return reason


def current_state(workspace):
    """Read current daemon readiness for UI/doctor without network or shell probes."""
    import plistlib
    from .job_path import find_agent
    from .launchd import production_status
    from .native import _apply_env_aliases, _load_workspace_env

    record = read_state(workspace)
    if not record:
        return {}
    environment = {"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "")}
    manifest = production_status(workspace) or {}
    try:
        plist = Path(manifest["jobs"]["triage"]["plist"])
        stored = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]
        if stored.get("SIDEQUESTOR_WORKSPACE") == str(workspace.root):
            environment = dict(stored)
    except (KeyError, OSError, ValueError, TypeError):
        pass
    environment = _apply_env_aliases(_load_workspace_env(environment, workspace))
    agent = environment.get("YAAS_AGENT", "codex")
    binary = find_agent(agent, environment.get("PATH", ""))
    return record if binary and record.get("fingerprint") == fingerprint(agent, binary, environment) else {}
