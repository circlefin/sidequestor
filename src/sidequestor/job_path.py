"""Choose the PATH that launchd jobs and runtime scripts run with.

Launchd starts jobs with whatever PATH the plist records, and the runtime scripts call
bare ``python3``, ``sq``, and the agent CLI. The documented install used to be an
activated virtualenv, which put the environment's ``bin`` first; uv and pipx installs
are never activated, so that ordering is reproduced explicitly here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .workspace import Workspace


SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
AGENT_BINARIES = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}
LOGIN_SHELL_TIMEOUT_SECONDS = 5.0


def _dispatch_dirs() -> list[str]:
    """Directories dispatch-agent.sh prepends before launching a worker."""
    return ["/opt/homebrew/bin", str(Path.home() / ".local" / "bin")]


def _components(value: str | None) -> list[str]:
    # Empty and relative entries make lookups depend on the job's working directory.
    return [part for part in (value or "").split(os.pathsep) if part and os.path.isabs(part)]


def merge_path(*groups: list[str]) -> str:
    seen: list[str] = []
    for group in groups:
        for part in group:
            if part not in seen:
                seen.append(part)
    return os.pathsep.join(seen)


def environment_bin_dir(executable: Path | str) -> str | None:
    """Return the virtualenv ``bin`` holding this interpreter, without resolving symlinks."""
    directory = Path(os.path.abspath(executable)).parent
    return str(directory) if (directory.parent / "pyvenv.cfg").is_file() else None


def _workspace_env(workspace: Workspace) -> dict[str, str]:
    from .native import _load_workspace_env

    return _load_workspace_env({}, workspace)


def selected_agent(workspace: Workspace) -> str:
    """The backend launchd jobs will use; they read ``.env``, not the caller's exports."""
    dotenv = _workspace_env(workspace)
    return dotenv.get("SIDEQUESTOR_AGENT") or dotenv.get("YAAS_AGENT") or "codex"


def agent_binary_name(agent: str) -> str:
    return AGENT_BINARIES.get(agent, agent)


def find_agent(agent: str, path: str) -> str | None:
    """Resolve the agent the way the dispatcher will, including its prepended dirs."""
    return shutil.which(agent_binary_name(agent), path=merge_path(_dispatch_dirs(), _components(path)))


def login_shell_path(timeout: float = LOGIN_SHELL_TIMEOUT_SECONDS) -> list[str]:
    """Ask the user's login shell for PATH; return nothing on any failure.

    Interactive rc files may print, prompt, or hang, so the shell gets no stdin, its
    output is discarded, it is killed after ``timeout``, and PATH comes back through a
    private file instead of being parsed out of startup noise.
    """
    shell = os.environ.get("SHELL") or "/bin/zsh"
    if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
        return []
    with tempfile.TemporaryDirectory(prefix="sidequestor-path-") as directory:
        target = Path(directory) / "path"
        environment = dict(os.environ, SIDEQUESTOR_PATH_PROBE=str(target))
        try:
            subprocess.run(
                [shell, "-l", "-i", "-c", 'printf "%s" "$PATH" > "$SIDEQUESTOR_PATH_PROBE"'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, timeout=timeout, check=False,
            )
            return _components(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            return []


@dataclass(frozen=True)
class JobPath:
    value: str
    agent: str
    agent_binary: str | None
    login_shell_used: bool


def resolve_job_path(workspace: Workspace, executable: Path | str) -> JobPath:
    """Build the PATH recorded in launchd plists.

    ``SIDEQUESTOR_PATH`` (exported, or in ``.env``) replaces the caller's PATH outright.
    Otherwise the caller's PATH is kept in order, so existing jobs see nothing removed,
    with the installation's ``bin`` first and the system directories last. The login
    shell is consulted only when the selected agent CLI still cannot be found.
    """
    override = os.environ.get("SIDEQUESTOR_PATH") or _workspace_env(workspace).get("SIDEQUESTOR_PATH")
    base = _components(override if override else os.environ.get("PATH"))
    own_bin = environment_bin_dir(executable)
    groups = [[own_bin] if own_bin else [], base, list(SYSTEM_DIRS)]
    value = merge_path(*groups)
    agent = selected_agent(workspace)
    binary = find_agent(agent, value)
    used_login_shell = False
    if binary is None and not override:
        probed = login_shell_path()
        if probed:
            value = merge_path(*groups[:2], probed, groups[2])
            binary = find_agent(agent, value)
            used_login_shell = True
    return JobPath(value, agent, binary, used_login_shell)
