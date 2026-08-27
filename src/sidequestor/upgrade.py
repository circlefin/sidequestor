"""Safely replace the installed package and refresh one workspace."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

from . import __version__
from .launchd import LaunchdLifecycleError, production_status, stop_production
from .workspace import Workspace


_GITHUB_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def github_requirement(source: str, ref: str) -> str:
    """Return a validated pip direct reference for one GitHub repository revision."""
    parsed = urlsplit(source)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("--source must be an https://github.com URL")
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("--source must not contain credentials, a port, query, or fragment")
    components = [component for component in parsed.path.split("/") if component]
    if len(components) != 2:
        raise ValueError("--source must identify exactly one GitHub owner/repository")
    owner, repository = components
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository or not all(
        _GITHUB_COMPONENT.fullmatch(component) for component in (owner, repository)
    ):
        raise ValueError("--source contains an invalid GitHub owner or repository name")
    if (
        not _GIT_REF.fullmatch(ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".", ".lock"))
        or "@{" in ref
    ):
        raise ValueError("--ref is not a safe Git branch, tag, or commit name")
    return f"sidequestor @ git+https://github.com/{owner}/{repository}.git@{ref}"


def _fresh_cli(workspace: Workspace, command: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "sidequestor",
        "--workspace",
        str(workspace.root),
        command,
    ]


def _run(command: list[str], workspace: Workspace, *, fresh_import: bool = False) -> int:
    environment = None
    if fresh_import:
        environment = dict(os.environ)
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
    try:
        result = subprocess.run(
            command, cwd=workspace.root, check=False, env=environment,
        )
    except OSError as exc:
        print(f"could not execute {command[0]}: {exc}", file=sys.stderr)
        return 1
    return result.returncode if result.returncode >= 0 else 1


def _restore_after_install_failure(workspace: Workspace, should_restart: bool) -> None:
    if not should_restart:
        return
    print("Package installation failed; attempting to restore the previously running jobs.")
    if _run(_fresh_cli(workspace, "start"), workspace, fresh_import=True):
        print(
            "warning: Sidequestor could not be restarted; "
            "run `sq start` after repairing the installation.",
            file=sys.stderr,
        )


def _confirm_git_upgrade(requirement: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit("Git source upgrades require --yes when input is not interactive")
    print("A Git source upgrade installs code that will run with your Sidequestor worker permissions:")
    print(f"  {requirement}")
    return input("Continue? [y/N]: ").strip().lower() in {"y", "yes"}


def run_upgrade(workspace: Workspace, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sidequestor upgrade")
    parser.add_argument("--source", help="GitHub repository URL instead of PyPI")
    parser.add_argument("--ref", "--branch", dest="ref", help="Git branch, tag, or commit")
    parser.add_argument("--pre", action="store_true", help="allow pre-releases from PyPI")
    parser.add_argument("--yes", action="store_true", help="confirm a Git source non-interactively")
    parser.add_argument(
        "--no-restart", action="store_true", help="leave previously running jobs stopped"
    )
    values = parser.parse_args(args)

    if values.source and not values.ref:
        parser.error("--source requires --ref")
    if values.ref and not values.source:
        parser.error("--ref requires --source")
    if values.source and values.pre:
        parser.error("--pre applies only to PyPI upgrades")

    if values.source:
        try:
            requirement = github_requirement(values.source, values.ref)
        except ValueError as exc:
            parser.error(str(exc))
        if not _confirm_git_upgrade(requirement, values.yes):
            print("Upgrade cancelled.")
            return 1
    else:
        requirement = "sidequestor"

    manifest = production_status(workspace)
    was_running = bool(manifest and manifest.get("running"))
    if was_running:
        try:
            if not stop_production(workspace):
                print("could not stop the recorded Sidequestor production jobs", file=sys.stderr)
                return 1
        except LaunchdLifecycleError as exc:
            print(f"could not stop Sidequestor before upgrading: {exc}", file=sys.stderr)
            return 1
        print(f"Stopped Sidequestor instance {workspace.instance_id} for upgrade.")

    print(f"Upgrading Sidequestor {__version__} using {sys.executable}")
    pip_command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if values.pre:
        pip_command.append("--pre")
    if values.source:
        # A moving branch can retain the same project version, so --upgrade alone is a no-op.
        pip_command.append("--force-reinstall")
    pip_command.append(requirement)
    try:
        install_code = _run(pip_command, workspace)
    except KeyboardInterrupt:
        print("Package installation interrupted.", file=sys.stderr)
        _restore_after_install_failure(workspace, was_running and not values.no_restart)
        return 130
    if install_code:
        _restore_after_install_failure(workspace, was_running and not values.no_restart)
        return install_code

    # This process still has the old package imported. Every post-install operation must
    # run in a child interpreter so resources and launchd plists come from the new build.
    print("Refreshing managed engine resources with the installed build.")
    sync_code = _run(_fresh_cli(workspace, "sync-resources"), workspace, fresh_import=True)
    if sync_code:
        print(
            "Upgrade installed, but resource sync failed; Sidequestor remains stopped.",
            file=sys.stderr,
        )
        return sync_code

    print("Validating the upgraded workspace.")
    doctor_code = _run(_fresh_cli(workspace, "doctor"), workspace, fresh_import=True)
    if doctor_code:
        print(
            "Upgrade installed, but validation failed; Sidequestor remains stopped.",
            file=sys.stderr,
        )
        return doctor_code

    if was_running and not values.no_restart:
        print("Restarting the previously running Sidequestor jobs.")
        start_code = _run(_fresh_cli(workspace, "start"), workspace, fresh_import=True)
        if start_code:
            print("Upgrade validated, but Sidequestor could not be restarted.", file=sys.stderr)
            return start_code
    elif was_running:
        print("Upgrade complete; jobs remain stopped because --no-restart was supplied.")

    print("Sidequestor upgrade complete.")
    return 0
