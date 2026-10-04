"""Safely replace the installed package and refresh one workspace."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .dashboard import read_dashboard_port, wait_for_dashboard_port
from .launchd import LaunchdLifecycleError, production_status, stop_production
from .workspace import Workspace, list_instances, load_workspace


_GITHUB_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SPEC_EXTRAS = re.compile(r"\[([A-Za-z0-9_.,\s-]+)\]\s*$")
INSTALLERS = ("pip", "pipx", "uv")


@dataclass(frozen=True)
class Installer:
    """The tool that owns the environment running this interpreter."""

    name: str
    tool: str = "sidequestor"
    extras: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    suffix: str = ""


def _pipx_installer(prefix: Path) -> Installer | None:
    try:
        metadata = json.loads((prefix / "pipx_metadata.json").read_text(encoding="utf-8"))
        package = metadata["main_package"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(package, dict) or package.get("package") != "sidequestor":
        return None
    spec = str(package.get("package_or_url") or "").split(" @ ", 1)[0]
    match = _SPEC_EXTRAS.search(spec)
    extras = tuple(sorted({item.strip() for item in match.group(1).split(",") if item.strip()})) if match else ()
    env = {"PIPX_HOME": str(prefix.parent.parent)} if prefix.parent.name == "venvs" else {}
    return Installer("pipx", prefix.name, extras, env,
                     suffix=str(package.get("suffix") or ""))


def _uv_installer(prefix: Path) -> Installer | None:
    try:
        receipt = tomllib.loads((prefix / "uv-receipt.toml").read_text(encoding="utf-8"))
        requirements = receipt["tool"]["requirements"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for requirement in requirements if isinstance(requirements, list) else []:
        if isinstance(requirement, dict) and requirement.get("name") == "sidequestor":
            extras = tuple(sorted(str(item) for item in requirement.get("extras") or ()))
            return Installer("uv", prefix.name, extras, {"UV_TOOL_DIR": str(prefix.parent)})
    return None


def detect_installer(prefix: Path | None = None) -> Installer | None:
    """Identify pipx, uv tool, or a plain pip venv from the environment's own metadata.

    Directory names and tools on PATH are not proof of ownership; the receipt each
    manager writes into the environment root is. A plain venv qualifies only when pip
    is importable, since uv-created environments usually have no pip at all.
    """
    prefix = Path(sys.prefix) if prefix is None else prefix
    for detect in (_pipx_installer, _uv_installer):
        installer = detect(prefix)
        if installer is not None:
            return installer
    # A damaged receipt must not turn a managed tool into an apparent pip venv.
    # The directory shape is only a veto here, never evidence for selecting a manager.
    if ((prefix / "pipx_metadata.json").exists()
            or (prefix / "uv-receipt.toml").exists()
            or prefix.parent.name in {"venvs", "tools"}):
        return None
    if prefix == Path(sys.prefix) and any(
        prefix.resolve().parent == _manager_root(name) for name in ("uv", "pipx")
    ):
        return None
    if (prefix == Path(sys.prefix) and sys.prefix != sys.base_prefix
            and importlib.util.find_spec("pip") is not None):
        return Installer("pip")
    return None


def installer_command(
    installer: Installer, *, git_requirement: str | None = None, pre: bool = False,
) -> list[str]:
    """Return the upgrade command for the tool that owns this installation."""
    if installer.name == "pip":
        command = [sys.executable, "-m", "pip", "install", "--upgrade"]
        if pre:
            command.append("--pre")
        if git_requirement:
            # A moving branch can retain the same project version, so --upgrade alone is a no-op.
            command.append("--force-reinstall")
        command.append(git_requirement or "sidequestor")
        return command
    executable = shutil.which(installer.name)
    if executable is None:
        raise ValueError(
            f"this installation is managed by {installer.name}, but `{installer.name}` "
            "is not on PATH"
        )
    if git_requirement:
        # Reinstalling from a direct reference replaces the recorded spec, so carry the
        # extras forward explicitly or they would silently disappear.
        extras = f"[{','.join(installer.extras)}]" if installer.extras else ""
        requirement = git_requirement.replace("sidequestor @ ", f"sidequestor{extras} @ ", 1)
        if installer.name == "pipx":
            command = [executable, "install", "--force"]
            if installer.suffix:
                # Otherwise pipx targets the unsuffixed environment, not this tool.
                command.append(f"--suffix={installer.suffix}")
            return [*command, requirement]
        return [executable, "tool", "install", "--force",
                "--reinstall-package", "sidequestor", requirement]
    if installer.name == "pipx":
        command = [executable, "upgrade"]
        if pre:
            command.append("--pip-args=--pre")
        return [*command, installer.tool]
    command = [executable, "tool", "upgrade"]
    if pre:
        command.extend(["--prerelease", "allow"])
    return [*command, installer.tool]


def _manager_root(name: str) -> Path | None:
    """Ask a manager where it will write tools, without changing an installation."""
    executable = shutil.which(name)
    if executable is None:
        return None
    command = ([executable, "tool", "dir"] if name == "uv" else
               [executable, "environment", "--value", "PIPX_HOME"])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    root = Path(result.stdout.strip()).expanduser().resolve()
    return root if name == "uv" else root / "venvs"


def _select_installer(choice: str, *, needs_extras: bool = False) -> Installer:
    detected = detect_installer()
    if choice == "auto":
        if detected is None:
            raise ValueError(
                "could not tell whether pip, pipx, or uv manages this installation "
                f"({sys.prefix}); rerun with --installer pip, pipx, or uv"
            )
        return detected
    if detected is not None:
        if detected.name != choice:
            raise ValueError(
                f"this installation is managed by {detected.name}, not {choice}"
            )
        return detected
    prefix = Path(sys.prefix)
    if choice == "pip":
        # Detection only trusts a non-base venv outside manager-shaped directories, but
        # pip also owns ~/venvs/* venvs and pyenv, conda, or --user installs. An explicit
        # choice is accepted unless a manager's receipt or tool directory claims the prefix.
        if ((prefix / "pipx_metadata.json").exists()
                or (prefix / "uv-receipt.toml").exists()
                or any(prefix.resolve().parent == _manager_root(name) for name in ("uv", "pipx"))
                or importlib.util.find_spec("pip") is None):
            raise ValueError(f"cannot verify that pip owns this installation ({prefix})")
        return Installer("pip")
    if prefix.name != "sidequestor":
        raise ValueError(
            f"the running environment is named {prefix.name!r}, not the Sidequestor tool"
        )
    if (prefix / "pipx_metadata.json").exists() or (prefix / "uv-receipt.toml").exists():
        raise ValueError(
            f"the manager receipt does not identify this environment as Sidequestor ({prefix})"
        )
    if needs_extras:
        raise ValueError(
            f"cannot read installed extras without a valid {choice} receipt; "
            "repair the receipt before installing from Git"
        )
    root = _manager_root(choice)
    if root is None or prefix.resolve().parent != root:
        raise ValueError(
            f"cannot verify that {choice} manages this installation ({prefix})"
        )
    env = {"UV_TOOL_DIR" if choice == "uv" else "PIPX_HOME":
           str(root if choice == "uv" else root.parent)}
    return Installer(choice, prefix.name, env=env)


def _other_running_workspaces(workspace: Workspace) -> list[Workspace]:
    """Running instances whose jobs execute this same shared installation."""
    python = os.path.abspath(sys.executable)
    others = []
    for row in list_instances():
        try:
            other = load_workspace(row["path"])
            manifest = production_status(other)
        except (KeyError, TypeError, OSError, SystemExit, ValueError):
            continue
        if (other.root != workspace.root and manifest and manifest.get("running")
                and manifest.get("python") == python):
            others.append(other)
    return others


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


def _fresh_cli(workspace: Workspace, command: str, *args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "sidequestor",
        "--workspace",
        str(workspace.root),
        command,
        *args,
    ]


def _run(
    command: list[str], workspace: Workspace, *, fresh_import: bool = False,
    extra_env: dict[str, str] | None = None,
) -> int:
    environment = None
    if fresh_import or extra_env:
        environment = dict(os.environ)
        environment.update(extra_env or {})
    if fresh_import:
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


def _restart_command(workspace: Workspace, dashboard_port: int | None) -> list[str]:
    command = _fresh_cli(workspace, "start")
    if dashboard_port is not None:
        command.extend(["--dashboard-port", str(dashboard_port)])
    return command


def _wait_for_previous_dashboard_port(dashboard_port: int | None) -> bool:
    if dashboard_port is None:
        return True
    print(f"Waiting for dashboard port {dashboard_port} to become available.")
    return wait_for_dashboard_port(dashboard_port)


def _restore_after_install_failure(
    workspace: Workspace, should_restart: bool, dashboard_port: int | None,
) -> None:
    if not should_restart:
        return
    print("Package installation failed; attempting to restore the previously running jobs.")
    if not _wait_for_previous_dashboard_port(dashboard_port):
        print(
            f"warning: dashboard port {dashboard_port} is still in use; "
            "Sidequestor was not restarted.",
            file=sys.stderr,
        )
        return
    if _run(_restart_command(workspace, dashboard_port), workspace, fresh_import=True):
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
    parser.add_argument(
        "--installer", choices=("auto", *INSTALLERS), default="auto",
        help="tool that manages this installation (default: detect pip, pipx, or uv)",
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
        requirement = None

    # Resolve the installer before stopping anything: an unusable upgrade path must not
    # leave a running instance stopped.
    try:
        installer = _select_installer(values.installer, needs_extras=bool(requirement))
        install_command = installer_command(
            installer, git_requirement=requirement, pre=values.pre,
        )
    except ValueError as exc:
        print(f"cannot upgrade: {exc}", file=sys.stderr)
        return 1
    others = _other_running_workspaces(workspace)

    manifest = production_status(workspace)
    was_running = bool(manifest and manifest.get("running"))
    dashboard_port = read_dashboard_port(workspace) if was_running else None
    if was_running:
        try:
            if not stop_production(workspace):
                print("could not stop the recorded Sidequestor production jobs", file=sys.stderr)
                return 1
        except LaunchdLifecycleError as exc:
            print(f"could not stop Sidequestor before upgrading: {exc}", file=sys.stderr)
            return 1
        print(f"Stopped Sidequestor instance {workspace.instance_id} for upgrade.")

    print(f"Upgrading Sidequestor {__version__} with {installer.name} ({sys.prefix})")
    try:
        install_code = _run(install_command, workspace, extra_env=installer.env)
    except KeyboardInterrupt:
        print("Package installation interrupted.", file=sys.stderr)
        _restore_after_install_failure(
            workspace, was_running and not values.no_restart, dashboard_port,
        )
        return 130
    if install_code:
        _restore_after_install_failure(
            workspace, was_running and not values.no_restart, dashboard_port,
        )
        return install_code

    if not os.path.exists(sys.executable):
        print(
            f"Upgrade installed, but the interpreter {sys.executable} no longer exists; "
            "Sidequestor remains stopped. Run `sq start` from the upgraded installation.",
            file=sys.stderr,
        )
        return 1

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
        if not _wait_for_previous_dashboard_port(dashboard_port):
            print(
                f"Upgrade validated, but dashboard port {dashboard_port} is still in use; "
                "Sidequestor remains stopped.",
                file=sys.stderr,
            )
            return 1
        print("Restarting the previously running Sidequestor jobs.")
        start_code = _run(
            _restart_command(workspace, dashboard_port), workspace, fresh_import=True,
        )
        if start_code:
            print(
                "Upgrade validated, but Sidequestor could not be restarted. "
                "If Keychain authorization was interrupted, run "
                "`sq credentials repair-keychain` in a terminal.",
                file=sys.stderr,
            )
            return start_code
    elif was_running:
        print("Upgrade complete; jobs remain stopped because --no-restart was supplied.")

    if others:
        print(
            "These workspaces share this installation and are still running the previous "
            "version; restart each one to pick up the upgrade:"
        )
        for other in others:
            print(f"  sq --workspace {other.root} start")
    print("Sidequestor upgrade complete.")
    return 0
