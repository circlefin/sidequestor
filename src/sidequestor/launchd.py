"""UUID-scoped launchd adapters for side-by-side package validation."""

from __future__ import annotations

import os
import json
import re
import signal
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .workspace import Workspace
from .build_info import build_info


JOB_NAMES = ("triage", "dashboard")
PRODUCTION_JOB_NAMES = ("triage", "heartbeat", "dashboard")
_BOOTOUT_ATTEMPTS = 5
_BOOTOUT_DELAY_SECONDS = 0.2
_PROCESS_DRAIN_GRACE_SECONDS = 2.0
_PROCESS_DRAIN_TERM_SECONDS = 3.0
_PROCESS_DRAIN_KILL_SECONDS = 1.0
_PROCESS_DRAIN_POLL_SECONDS = 0.05


class LaunchdLifecycleError(RuntimeError):
    """A package-owned launchd service did not reach the requested state."""


def _production_prefix(workspace: Workspace) -> str:
    return f"com.sidequestor.{workspace.instance_id}"


def _install_root(workspace: Workspace) -> Path:
    return workspace.yaas_dir / "launchd"


def _manifest_path(workspace: Workspace) -> Path:
    return _install_root(workspace) / "installed" / "manifest.json"


def _preserve_executable_path(executable: Path) -> Path:
    """Make the interpreter absolute without collapsing a venv symlink."""
    return Path(os.path.abspath(executable))


def _write_jobs(workspace: Workspace, executable: Path, destination: Path) -> dict:
    python = _preserve_executable_path(executable)
    jobs = {
        "triage": ["loop", "--isolated"],
        "dashboard": ["dashboard", "serve", "0"],
    }
    rendered = {}
    for name, arguments in jobs.items():
        label = f"com.sidequestor.{workspace.instance_id}.{name}"
        args = "\n".join(
            f"        <string>{escape(str(value))}</string>" for value in
            [str(python), "-m", "sidequestor", "--workspace", str(workspace.root), *arguments]
        )
        plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key><array>
{args}
    </array>
    <key>WorkingDirectory</key><string>{escape(str(workspace.root))}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict></plist>
'''
        path = destination / f"{label}.plist"
        path.write_text(plist)
        rendered[name] = {"label": label, "plist": path.name, "arguments": [
            str(python), "-m", "sidequestor", "--workspace", str(workspace.root), *arguments,
        ]}
    return rendered


def render(workspace: Workspace, executable: Path, destination: Path | None = None) -> Path:
    destination = destination or workspace.yaas_dir / "rendered-launchd"
    destination.mkdir(parents=True, exist_ok=True)
    _write_jobs(workspace, executable, destination)
    return destination


def install(workspace: Workspace, executable: Path) -> dict:
    """Install workspace-local validation artifacts without loading them.

    The shadow bundle lets package and migration tests inspect stable, instance-scoped
    plists without changing the operator's launchd state. Only the production
    lifecycle below bootstraps runnable jobs.
    """
    root = _install_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".install.", dir=root))
    try:
        jobs = _write_jobs(workspace, executable, temporary)
        manifest = {
            "schema": 1,
            "backend": "workspace-shadow",
            "instance_id": workspace.instance_id,
            "workspace": str(workspace.root),
            "python": str(_preserve_executable_path(executable)),
            "jobs": jobs,
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        installed = root / "installed"
        if installed.exists():
            shutil.rmtree(installed)
        os.replace(temporary, installed)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def status(workspace: Workspace) -> dict | None:
    try:
        value = json.loads(_manifest_path(workspace).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("instance_id") != workspace.instance_id:
        return None
    return value


def uninstall(workspace: Workspace) -> bool:
    root = _install_root(workspace)
    installed = root / "installed"
    manifest = root / "manifest.json"
    existed = installed.exists() or manifest.exists()
    if installed.exists():
        shutil.rmtree(installed)
    if manifest.exists():
        manifest.unlink()
    try:
        root.rmdir()
    except OSError:
        pass
    return existed


def _production_root() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _production_manifest_path(workspace: Workspace) -> Path:
    return workspace.yaas_dir / "launchd" / "production.json"


def _clear_dashboard_readiness(workspace: Workspace) -> None:
    (workspace.state / "dashboard-url.txt").unlink(missing_ok=True)


def _service_loaded(target: str) -> bool:
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _service_root_pid(target: str) -> int | None:
    """Return launchd's current root PID for a loaded service."""
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout or "", re.MULTILINE)
    return int(match.group(1)) if match else None


def _process_tree(root_pid: int) -> set[int]:
    """Snapshot a process and all descendants before launchd severs the tree."""
    found = {root_pid}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(parent)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        for value in (result.stdout or "").split():
            try:
                child = int(value)
            except ValueError:
                continue
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _alive_processes(pids: set[int]) -> set[int]:
    alive = set()
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            alive.add(pid)
            continue
        alive.add(pid)
    return alive


def _wait_for_processes(pids: set[int], timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    remaining = _alive_processes(pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(_PROCESS_DRAIN_POLL_SECONDS)
        remaining = _alive_processes(remaining)
    return remaining


def _signal_processes(pids: set[int], signum: signal.Signals) -> None:
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signum)
        except (ProcessLookupError, PermissionError):
            pass


def _drain_processes(pids: set[int], label: str) -> None:
    """Wait for launchd teardown, then stop any captured descendants it left."""
    remaining = _wait_for_processes(pids, _PROCESS_DRAIN_GRACE_SECONDS)
    if remaining:
        _signal_processes(remaining, signal.SIGTERM)
        remaining = _wait_for_processes(remaining, _PROCESS_DRAIN_TERM_SECONDS)
    if remaining:
        _signal_processes(remaining, signal.SIGKILL)
        remaining = _wait_for_processes(remaining, _PROCESS_DRAIN_KILL_SECONDS)
    if remaining:
        values = ", ".join(str(pid) for pid in sorted(remaining))
        raise LaunchdLifecycleError(
            f"launchd service processes remain after unload: {label} ({values})"
        )


def _bootout_job(uid: str, label: str) -> None:
    """Unload one exact package service and drain its original process tree."""
    target = f"gui/{uid}/{label}"
    root_pid = _service_root_pid(target)
    processes = _process_tree(root_pid) if root_pid is not None else set()
    diagnostics = []
    for attempt in range(_BOOTOUT_ATTEMPTS):
        result = subprocess.run(
            ["launchctl", "bootout", target],
            check=False,
            capture_output=True,
            text=True,
        )
        if not _service_loaded(target):
            _drain_processes(processes, label)
            return
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            diagnostics.append(detail)
        if attempt + 1 < _BOOTOUT_ATTEMPTS:
            time.sleep(_BOOTOUT_DELAY_SECONDS)
    suffix = f": {diagnostics[-1]}" if diagnostics else ""
    raise LaunchdLifecycleError(f"launchd service remains loaded: {label}{suffix}")


def _production_jobs(workspace: Workspace, executable: Path) -> dict:
    python = _preserve_executable_path(executable)
    runtime = Path(__file__).resolve().parent / "runtime"
    common = {
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "SIDEQUESTOR_WORKSPACE": str(workspace.root),
            "SIDEQUESTOR_RUNTIME_ROOT": str(runtime),
            "YAAS_WORKSPACE": str(workspace.root),
            "YAAS_RUNTIME_ROOT": str(runtime),
        },
        "WorkingDirectory": str(workspace.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
    }
    info = build_info()
    common["EnvironmentVariables"].update({
        "SIDEQUESTOR_VERSION": info["version"],
        "SIDEQUESTOR_COMMIT": info["commit_full"],
        "SIDEQUESTOR_REF": info["ref"],
    })
    commands = {
        "triage": [str(python), "-m", "sidequestor", "--workspace", str(workspace.root), "loop"],
        "heartbeat": ["/bin/bash", str(runtime / "yaas-triage" / "ops" / "heartbeat-loop.sh")],
        "dashboard": [str(python), "-m", "sidequestor", "--workspace", str(workspace.root), "dashboard", "serve", "0"],
    }
    jobs = {}
    for name, arguments in commands.items():
        label = f"{_production_prefix(workspace)}.{name}"
        values = dict(common)
        values.update({
            "Label": label,
            "ProgramArguments": arguments,
            "StandardOutPath": str(workspace.logs / f"package-{name}.out.log"),
            "StandardErrorPath": str(workspace.logs / f"package-{name}.err.log"),
        })
        jobs[name] = {"label": label, "arguments": arguments, "plist": f"{label}.plist", "values": values}
    return jobs


def _plist(values: dict) -> str:
    def scalar(value: object) -> str:
        if isinstance(value, bool):
            return "<true/>" if value else "<false/>"
        return f"<string>{escape(str(value))}</string>"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0"><dict>',
    ]
    for key, value in values.items():
        lines.append(f"    <key>{key}</key>")
        if isinstance(value, dict):
            lines.append("    <dict>")
            for child_key, child_value in value.items():
                lines.append(f"        <key>{child_key}</key><string>{escape(str(child_value))}</string>")
            lines.append("    </dict>")
        elif isinstance(value, list):
            lines.append("    <array>")
            lines.extend(f"        {scalar(item)}" for item in value)
            lines.append("    </array>")
        else:
            lines.append(f"    {scalar(value)}")
    lines.extend(["</dict></plist>", ""])
    return "\n".join(lines)


def install_production(workspace: Workspace, executable: Path) -> dict:
    """Install package jobs, replacing this workspace's previous package labels."""
    launch_agents = _production_root()
    launch_agents.mkdir(parents=True, exist_ok=True)
    previous = production_status(workspace)
    jobs = _production_jobs(workspace, executable)
    manifest_path = _production_manifest_path(workspace)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = {}
    uid = str(os.getuid())
    written: list[tuple[str, Path]] = []
    loaded: list[str] = []
    try:
        if previous:
            for old_job in previous.get("jobs", {}).values():
                old_label = old_job.get("label")
                if old_label:
                    _bootout_job(uid, old_label)
        for name, job in jobs.items():
            if name == "dashboard":
                _clear_dashboard_readiness(workspace)
            destination = launch_agents / job["plist"]
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_text(_plist(job["values"]))
            os.replace(temporary, destination)
            written.append((job["label"], destination))
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{job['label']}"], check=False, capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(destination)], check=True)
            loaded.append(job["label"])
            rendered[name] = {"label": job["label"], "plist": str(destination), "arguments": job["arguments"]}
    except Exception:
        for label in loaded:
            try:
                _bootout_job(uid, label)
            except LaunchdLifecycleError:
                pass
        for _, destination in written:
            destination.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": 2,
        "backend": "production",
        "instance_id": workspace.instance_id,
        "workspace": str(workspace.root),
        "python": str(_preserve_executable_path(executable)),
        "running": True,
        "jobs": rendered,
    }
    temporary_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
    temporary_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary_manifest, manifest_path)
    if previous:
        active_plists = {Path(job["plist"]) for job in rendered.values()}
        for old_job in previous.get("jobs", {}).values():
            old_plist = Path(old_job.get("plist", ""))
            if old_plist not in active_plists and old_plist.is_file() and old_plist.parent == launch_agents:
                old_plist.unlink()
    return manifest


def production_status(workspace: Workspace) -> dict | None:
    try:
        value = json.loads(_production_manifest_path(workspace).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("backend") != "production":
        return None
    try:
        recorded_workspace = Path(value["workspace"]).expanduser().resolve()
    except (KeyError, OSError, TypeError):
        return None
    if recorded_workspace != workspace.root:
        return None
    recorded_instance = value.get("instance_id")
    if recorded_instance is not None and recorded_instance != workspace.instance_id:
        return None
    return value


def production_is_running(workspace: Workspace) -> bool:
    """Return whether all recorded production jobs are loaded in launchd."""
    manifest = production_status(workspace)
    if not manifest or not manifest.get("running"):
        return False
    labels = [
        job.get("label")
        for job in manifest.get("jobs", {}).values()
        if isinstance(job, dict) and job.get("label")
    ]
    return bool(labels) and all(
        _service_loaded(f"gui/{os.getuid()}/{label}") for label in labels
    )


def uninstall_production(workspace: Workspace) -> bool:
    manifest = production_status(workspace)
    if not manifest:
        return False
    uid = str(os.getuid())
    for job in manifest.get("jobs", {}).values():
        label = job.get("label")
        plist = Path(job.get("plist", ""))
        if label:
            _bootout_job(uid, label)
        if plist.is_file() and plist.parent == _production_root():
            plist.unlink()
    _production_manifest_path(workspace).unlink(missing_ok=True)
    _clear_dashboard_readiness(workspace)
    return True


def stop_production(workspace: Workspace) -> bool:
    """Stop this workspace's production jobs while retaining their manifest."""
    manifest = production_status(workspace)
    if not manifest:
        return False
    uid = str(os.getuid())
    labels = [job.get("label") for job in manifest.get("jobs", {}).values() if job.get("label")]
    if not labels:
        return False
    for label in labels:
        _bootout_job(uid, label)
    _clear_dashboard_readiness(workspace)
    manifest["running"] = False
    path = _production_manifest_path(workspace)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary, path)
    return True
