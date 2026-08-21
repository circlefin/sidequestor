"""UUID-scoped launchd adapters for side-by-side package validation."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .workspace import Workspace


JOB_NAMES = ("triage", "dashboard")
PRODUCTION_JOB_NAMES = ("triage", "heartbeat", "dashboard")
PRODUCTION_PREFIX = "com.sidequestor.yaas.production"


def _install_root(workspace: Workspace) -> Path:
    return workspace.yaas_dir / "launchd"


def _manifest_path(workspace: Workspace) -> Path:
    return _install_root(workspace) / "installed" / "manifest.json"


def _write_jobs(workspace: Workspace, executable: Path, destination: Path) -> dict:
    python = executable.resolve()
    jobs = {
        "triage": ["loop", "--isolated"],
        "dashboard": ["dashboard", "serve", "0"],
    }
    rendered = {}
    for name, arguments in jobs.items():
        label = f"com.sidequestor.yaas.{workspace.instance_id}.{name}"
        args = "\n".join(
            f"        <string>{escape(str(value))}</string>" for value in
            [str(python), "-m", "yaas_triage", "--workspace", str(workspace.root), *arguments]
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
            str(python), "-m", "yaas_triage", "--workspace", str(workspace.root), *arguments,
        ]}
    return rendered


def render(workspace: Workspace, executable: Path, destination: Path | None = None) -> Path:
    destination = destination or workspace.yaas_dir / "rendered-launchd"
    destination.mkdir(parents=True, exist_ok=True)
    _write_jobs(workspace, executable, destination)
    return destination


def install(workspace: Workspace, executable: Path) -> dict:
    """Install only this workspace's jobs into its shadow-managed bundle."""
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
            "python": str(executable.resolve()),
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


def _production_jobs(workspace: Workspace, executable: Path) -> dict:
    python = executable.resolve()
    runtime = Path(__file__).resolve().parent / "runtime"
    common = {
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "YAAS_WORKSPACE": str(workspace.root),
            "YAAS_RUNTIME_ROOT": str(runtime),
        },
        "WorkingDirectory": str(workspace.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
    }
    commands = {
        "triage": [str(python), "-m", "yaas_triage", "--workspace", str(workspace.root), "loop"],
        "heartbeat": ["/bin/bash", str(runtime / "yaas-triage" / "ops" / "heartbeat-loop.sh")],
        "dashboard": [str(python), "-m", "yaas_triage", "--workspace", str(workspace.root), "dashboard", "serve", "8877"],
    }
    jobs = {}
    for name, arguments in commands.items():
        label = f"{PRODUCTION_PREFIX}.{name}"
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
    """Install and load the package jobs without touching legacy labels."""
    launch_agents = _production_root()
    launch_agents.mkdir(parents=True, exist_ok=True)
    jobs = _production_jobs(workspace, executable)
    manifest_path = _production_manifest_path(workspace)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = {}
    uid = str(os.getuid())
    written: list[tuple[str, Path]] = []
    loaded: list[str] = []
    try:
        for name, job in jobs.items():
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
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
        for _, destination in written:
            destination.unlink(missing_ok=True)
        raise
    manifest = {"schema": 1, "backend": "production", "workspace": str(workspace.root), "python": str(executable.resolve()), "jobs": rendered}
    temporary_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
    temporary_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary_manifest, manifest_path)
    return manifest


def production_status(workspace: Workspace) -> dict | None:
    try:
        value = json.loads(_production_manifest_path(workspace).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("backend") == "production" else None


def uninstall_production(workspace: Workspace) -> bool:
    manifest = production_status(workspace)
    if not manifest:
        return False
    uid = str(os.getuid())
    for job in manifest.get("jobs", {}).values():
        label = job.get("label")
        plist = Path(job.get("plist", ""))
        if label:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
        if plist.is_file() and plist.parent == _production_root():
            plist.unlink()
    _production_manifest_path(workspace).unlink(missing_ok=True)
    return True
