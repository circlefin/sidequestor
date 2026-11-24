"""Bounded worker checks; only proven credential/configuration failures block work."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from .workspace import Workspace, _write_json, workspace_lock

AUTH_ERROR = re.compile(r"not logged in|please (?:log|sign) in|authentication required|invalid api key|oauth token (?:has )?expired|token_revoked|invalid_auth|not_authed|authentication_error|unauthorized", re.I)
COMPAT_ERROR = re.compile(r"unknown (?:option|argument)|unrecognized (?:option|argument|model)|unexpected argument|model .{0,100}(?:not found|does not exist|not supported|not recognized)", re.I)

RECHECK_INTERVAL_SEC = 30
SLACK_ACCESS_ERROR = "Slack access could not be verified. Check the connection or credentials. Run sq setup."
MODEL_ACCESS_ERROR = re.compile(r"unauthori[sz]ed.{0,80}model|model.{0,80}(?:access denied|permission denied|not entitled)|no access to.{0,40}model", re.I)


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
    identity = ["readiness-v2", agent, str(Path(binary).resolve()), *[environment.get(k, "") for k in keys]]
    identity.extend(environment.get(k, "") for k in
                    ("CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                     "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                     "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_MODEL",
                     "YAAS_CLAUDE_PERMISSION_MODE", "YAAS_CODEX_PERMISSION_MODE",
                     "YAAS_CURSOR_PERMISSION_MODE",
                     "SIDEQUESTOR_CLAUDE_PERMISSION_MODE", "SIDEQUESTOR_CODEX_PERMISSION_MODE",
                     "SIDEQUESTOR_CURSOR_PERMISSION_MODE",
                     "YAAS_WORKER_PERMISSION_MODE", "SIDEQUESTOR_WORKER_PERMISSION_MODE",
                     "YAAS_CURSOR_MODEL", "SIDEQUESTOR_CURSOR_MODEL"))
    home = Path(environment.get("HOME") or Path.home())
    configuration = []
    if agent == "codex":
        directory = Path(environment.get("CODEX_HOME") or home / ".codex")
        configuration = [directory / "config.toml", directory / "auth.json"]
    elif agent == "claude":
        directory = Path(environment.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        configuration = [directory / "settings.json", directory / ".credentials.json"]
        root = environment.get("SIDEQUESTOR_WORKSPACE") or environment.get("YAAS_WORKSPACE")
        if root:
            configuration.extend(Path(root) / ".claude" / name for name in ("settings.json", "settings.local.json"))
    for path in configuration:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        identity.append((str(path), digest))
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


def save_state(workspace, agent, binary, environment, reason=None, *, scope="blocked", kind="auth", preserve_stronger_block=False):
    with workspace_lock(workspace, "worker-readiness"):
        previous = read_state(workspace)
        record = dict(previous) if previous.get("fingerprint") == fingerprint(agent, binary, environment) else {}
        if preserve_stronger_block and record.get("blocked_kind") in {"compat", "auth_direct"}:
            return record
        record.update(agent=agent, fingerprint=fingerprint(agent, binary, environment), checked_at=time.time())
        record[f"{scope}_checked_at"] = record["checked_at"]
        if reason:
            record[scope] = reason
            record[f"{scope}_kind"] = kind
        else:
            record.pop(scope, None)
            record.pop(f"{scope}_kind", None)
        _write_json(state_path(workspace), record)
        return record


def auth_status(workspace, agent, binary, environment):
    """Return (ok|unknown|error, reason); unknown never proves recovery."""
    if agent not in {"claude", "codex"}:
        return "unknown", None
    external = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN") if agent == "claude" else ("OPENAI_API_KEY", "CODEX_API_KEY")
    if any(environment.get(k) for k in external):
        return "unknown", None  # Account-status commands do not reliably report environment auth.
    if agent == "claude" and any(environment.get(k, "").lower() in {"1", "true"} for k in
                                  ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        return "unknown", None
    args = [binary, "auth", "status"] if agent == "claude" else [binary, "login", "status"]
    try:
        cp = invoke(args, workspace, environment, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", None  # An unavailable check is not proof of invalid credentials.
    text = cp.stdout + cp.stderr
    if agent == "claude":
        try:
            status = json.loads(cp.stdout)
            if status.get("authMethod") in {"third_party", "api_key", "api_key_helper", "oauth_token"}:
                return "unknown", None
            if status.get("loggedIn") is True:
                return "ok", None
            if status.get("loggedIn") is False:
                return "error", "AI account is not connected. Run sq setup."
        except (ValueError, AttributeError):
            pass
    if cp.returncode and AUTH_ERROR.search(text):
        return "error", "AI account is not connected. Run sq setup."
    return ("ok", None) if agent == "codex" and cp.returncode == 0 else ("unknown", None)


def auth_check(workspace, agent, binary, environment):
    """Compatibility wrapper: only a proven authentication error is blocking."""
    return auth_status(workspace, agent, binary, environment)[1]


def blocked_reason(workspace, agent, binary, environment, *, needs_slack=False):
    record = read_state(workspace)
    if record.get("fingerprint") != fingerprint(agent, binary, environment):
        return None
    return record.get("blocked") or (record.get("slack") if needs_slack else None)


def invocation_failure(workspace, agent, binary, environment, text, *, trusted_cli=False, slack=False, direct_auth=False):
    """Keep model entitlement separate from account authentication."""
    if trusted_cli and (COMPAT_ERROR.search(text) or MODEL_ACCESS_ERROR.search(text)):
        reason = "Worker CLI/model configuration needs attention. Run sq setup to check CLI updates and model access."
        save_state(workspace, agent, binary, environment, reason, kind="compat")
        return reason
    if not slack and AUTH_ERROR.search(text):
        status, reason = auth_status(workspace, agent, binary, environment)
        if direct_auth:
            reason = "AI account could not authenticate. Run sq setup to reconnect."
        if reason:
            save_state(workspace, agent, binary, environment, reason,
                       kind="auth_direct" if direct_auth and status == "ok" else "auth")
            return reason
    return None


def slack_helper_ready(workspace, environment):
    """One bounded, read-only probe through the same helper workers can use."""
    helper = Path(__file__).parent / "runtime/yaas-triage/surfaces/client.py"
    try:
        result = invoke([sys.executable, str(helper), "mcp", "slack_search_public_and_private",
                         '{"query":"yaas-health-ping","limit":1}'],
                        workspace, environment, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def verify_slack_access(workspace, agent, binary, environment, *, read_succeeded=False):
    """A native MCP failure is blocking only if the supported helper also fails."""
    record = read_state(workspace)
    if record.get("fingerprint") != fingerprint(agent, binary, environment):
        record = {}
    if not read_succeeded and _recent(record, "slack"):
        return record.get("slack")
    ready = read_succeeded or slack_helper_ready(workspace, environment)
    reason = None if ready else SLACK_ACCESS_ERROR
    save_state(workspace, agent, binary, environment, reason, scope="slack")
    return reason


def _recent(record, scope):
    try:
        return 0 <= time.time() - float(record.get(f"{scope}_checked_at", 0)) < RECHECK_INTERVAL_SEC
    except (ValueError, TypeError):
        return False


def preflight(workspace, agent, binary, environment, *, needs_slack=False):
    record = read_state(workspace)
    if record.get("fingerprint") != fingerprint(agent, binary, environment):
        record = {}
    reason = record.get("blocked")
    # A local status cannot disprove a direct provider rejection or a model error.
    if reason and record.get("blocked_kind") in {"compat", "auth_direct"}:
        return reason
    if not _recent(record, "blocked"):
        status, checked_reason = auth_status(workspace, agent, binary, environment)
        # An inconclusive status must neither create a block nor clear a known failure.
        reason = checked_reason if status == "error" else None if status == "ok" else reason
        record = save_state(workspace, agent, binary, environment, reason, kind="auth", preserve_stronger_block=True)
        reason = record.get("blocked")
    if reason:
        return reason
    slack_reason = record.get("slack") if needs_slack else None
    if slack_reason and not _recent(record, "slack"):
        if slack_helper_ready(workspace, environment):
            slack_reason = None
        save_state(workspace, agent, binary, environment, slack_reason, scope="slack")
    return slack_reason


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
