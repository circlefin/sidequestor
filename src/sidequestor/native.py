"""Execute the migrated runtime bundled inside the Sidequestor distribution."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .workspace import Workspace
from .build_info import build_info


RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"


def _apply_env_aliases(environment: dict[str, str]) -> dict[str, str]:
    """Expose canonical names while keeping the unchanged runtime contract."""
    for key, value in list(environment.items()):
        if key.startswith("SIDEQUESTOR_"):
            environment["YAAS_" + key[len("SIDEQUESTOR_"):]] = value
    for key, value in list(environment.items()):
        if key.startswith("YAAS_"):
            environment.setdefault("SIDEQUESTOR_" + key[len("YAAS_"):], value)
    return environment


def _environment(workspace: Workspace, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "YAAS_WORKSPACE": str(workspace.root),
        "YAAS_ENGINE_ROOT": str(workspace.yaas_dir / "engine" / "current"),
        "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT),
        "REPO_ROOT": str(workspace.root),
    })
    info = build_info()
    environment.update({
        "SIDEQUESTOR_VERSION": info["version"],
        "SIDEQUESTOR_COMMIT": info["commit_full"],
        "SIDEQUESTOR_REF": info["ref"],
    })
    runtime_python = str(RUNTIME_ROOT / "yaas-triage")
    current_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        runtime_python if not current_pythonpath
        else runtime_python + os.pathsep + current_pythonpath
    )
    if extra_env:
        environment.update(extra_env)
    return _apply_env_aliases(environment)


def run_native(
    workspace: Workspace,
    relative: str,
    args: list[str],
    *,
    timeout: float | None = None,
    extra_env: dict[str, str] | None = None,
) -> int:
    executable = RUNTIME_ROOT / relative
    if not executable.exists():
        raise SystemExit(f"packaged runtime command is not present: {relative}")
    command = (
        [sys.executable, str(executable), *args]
        if executable.suffix == ".py"
        else ["bash", str(executable), *args]
    )
    result = subprocess.run(
        command,
        cwd=workspace.root,
        env=_environment(workspace, extra_env),
        timeout=timeout,
    )
    return result.returncode


def run_native_tick(workspace: Workspace, *, fake_worker: bool = False) -> int:
    extra = {
        "YAAS_STAGE3_FAKE_WORKER": "1" if fake_worker else "0",
    }
    if fake_worker:
        extra["YAAS_RUN_AGENT"] = str(RUNTIME_ROOT / "yaas-triage" / "dispatch" / "fake-worker.py")
    return run_native(
        workspace,
        "yaas-triage/tick.py",
        [],
        extra_env={
            "YAAS_STAGE3_ISOLATED": "1",
            "YAAS_AGENT": "stub",
            "YAAS_SLACK_CHECKERS_ENABLED": "0",
            "YAAS_SKIP_NETWORK_PROBE": "1",
            "DRY_RUN": "0" if fake_worker else "1",
            **extra,
        },
    )


def run_native_loop(workspace: Workspace, interval: float) -> int:
    return run_native(
        workspace,
        "yaas-triage/triage-loop.sh",
        [],
        extra_env={
            "YAAS_STAGE3_ISOLATED": "1",
            "YAAS_AGENT": "stub",
            "YAAS_SLACK_CHECKERS_ENABLED": "0",
            "YAAS_SKIP_NETWORK_PROBE": "1",
            "DRY_RUN": "1",
            "YAAS_TRIAGE_INTERVAL": str(interval),
        },
    )


def dry_tick(workspace: Workspace) -> int:
    return run_native(
        workspace,
        "yaas-triage/tick_state.py",
        [],
        extra_env={"YAAS_AGENT": "stub"},
    )
