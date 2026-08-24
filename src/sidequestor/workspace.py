"""Workspace identity and user-owned filesystem layout."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1
REACTION_WATERMARK = "reaction-watermark.json"


def _config_home() -> Path:
    configured = os.environ.get("SIDEQUESTOR_CONFIG_HOME") or os.environ.get("YAAS_CONFIG_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".config"


@dataclass(frozen=True)
class Workspace:
    root: Path
    instance_id: str
    display_name: str

    @property
    def yaas_dir(self) -> Path:
        return self.root / ".yaas"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def skills(self) -> Path:
        return self.root / "skills"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid Sidequestor workspace marker: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"invalid Sidequestor workspace marker: {path}: expected object")
    return value


def ensure_reaction_watermark(workspace: Workspace) -> Path:
    """Create the immutable first-scan boundary for global reactions."""
    path = workspace.state / "triage" / REACTION_WATERMARK
    if path.exists():
        return path
    marker = _read_json(workspace.yaas_dir / "instance.json")
    initialized_at = str(marker.get("created_at", ""))
    if not initialized_at:
        raise SystemExit(f"YAAS workspace has no creation timestamp: {workspace.yaas_dir / 'instance.json'}")
    _write_json(path, {
        "initialized_at": initialized_at,
        "policy": "ignore reactions older than workspace initialization",
    })
    return path


def _register(workspace: Workspace) -> None:
    """Keep a small advisory registry for the Stage 2 instance commands."""
    registry = _config_home() / "yaas" / "instances.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = json.loads(registry.read_text()) if registry.exists() else []
    except ValueError:
        rows = []
    if not isinstance(rows, list):
        rows = []
    rows = [row for row in rows if isinstance(row, dict) and row.get("instance_id") != workspace.instance_id]
    rows.append({
        "instance_id": workspace.instance_id,
        "display_name": workspace.display_name,
        "path": str(workspace.root),
    })
    _write_json(registry, rows)


def init_workspace(path: str | Path, name: str | None = None) -> Workspace:
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".yaas" / "instance.json"
    if marker.exists():
        return load_workspace(root)

    instance_id = secrets.token_hex(16)
    display_name = name or root.name
    ws = Workspace(root, instance_id, display_name)
    ws.yaas_dir.mkdir(parents=True, exist_ok=True)
    for directory in (ws.state, ws.logs, ws.skills, ws.state / "quests" / "active"):
        directory.mkdir(parents=True, exist_ok=True)

    if not (root / ".env.example").exists():
        (root / ".env.example").write_text("# Sidequestor workspace configuration\n")
    if not ws.env_file.exists():
        ws.env_file.write_text("")
        ws.env_file.chmod(0o600)
    if not (root / "settings.json").exists():
        _write_json(root / "settings.json", {})
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_json(marker, {
        "instance_id": instance_id,
        "display_name": display_name,
        "created_at": created_at,
    })
    _write_json(ws.yaas_dir / ".yaas-version", {
        "schema_version": SCHEMA_VERSION,
        "created_by": "0.1.0.dev0",
    })
    ensure_reaction_watermark(ws)
    _register(ws)
    return ws


def load_workspace(path: str | Path) -> Workspace:
    root = Path(path).expanduser().resolve()
    marker = root / ".yaas" / "instance.json"
    if not marker.exists():
        raise SystemExit(f"not a Sidequestor workspace: {root} (run 'sidequestor init {root}')")
    data = _read_json(marker)
    instance_id = str(data.get("instance_id", ""))
    display_name = str(data.get("display_name", root.name))
    if not instance_id:
        raise SystemExit(f"YAAS workspace has no instance_id: {root}")
    return Workspace(root, instance_id, display_name)


def find_workspace_root(path: str | Path | None = None) -> Path | None:
    """Find the initialized workspace containing a path."""
    try:
        current = Path.cwd() if path is None else Path(path).expanduser()
        current = current.resolve()
    except OSError as exc:
        raise SystemExit(
            "the current directory is unavailable; change to a live directory or use --workspace PATH"
        ) from exc
    for candidate in (current, *current.parents):
        if (candidate / ".yaas" / "instance.json").is_file():
            return candidate
    return None


def find_workspace(path: str | Path | None = None) -> Workspace:
    """Load the initialized workspace containing a path."""
    root = find_workspace_root(path)
    if root is not None:
        return load_workspace(root)
    target = Path.cwd() if path is None else Path(path).expanduser()
    return load_workspace(target)


def list_instances() -> list[dict]:
    registry = _config_home() / "yaas" / "instances.json"
    if not registry.exists():
        return []
    try:
        value = json.loads(registry.read_text())
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def register_workspace(workspace: Workspace) -> None:
    _register(workspace)


def rekey_workspace(path: str | Path) -> Workspace:
    workspace = load_workspace(path)
    launchd = workspace.yaas_dir / "launchd"
    if (launchd / "production.json").exists() or (launchd / "installed" / "manifest.json").exists():
        raise SystemExit(
            "uninstall Sidequestor launchd jobs before rekeying this workspace"
        )
    data = _read_json(workspace.yaas_dir / "instance.json")
    data["instance_id"] = secrets.token_hex(16)
    _write_json(workspace.yaas_dir / "instance.json", data)
    updated = Workspace(workspace.root, data["instance_id"], workspace.display_name)
    _register(updated)
    return updated


def validate_workspace(workspace: Workspace) -> list[str]:
    errors: list[str] = []
    for required in (workspace.yaas_dir, workspace.state, workspace.logs, workspace.skills):
        if not required.exists():
            errors.append(f"missing {required.relative_to(workspace.root)}")
    schema = workspace.yaas_dir / ".yaas-version"
    if schema.exists():
        data = _read_json(schema)
        if data.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"unsupported schema_version={data.get('schema_version')}")
    else:
            errors.append("missing .yaas/.yaas-version")
    return errors
