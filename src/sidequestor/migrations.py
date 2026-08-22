"""Safe migration of a legacy checkout into a YAAS workspace."""

from __future__ import annotations

import json
import os
import secrets
import tarfile
import time
from pathlib import Path

from .workspace import Workspace, init_workspace, load_workspace


MIGRATION_ID = "legacy-checkout-to-workspace-v1"
LOCK_NAME = "migration.lock"
BACKUP_ITEMS = (
    ".env",
    ".env.example",
    "CLAUDE.md",
    "settings.json",
    "state",
    "logs",
    "skills",
)


def _timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _backup_root(workspace_root: Path) -> Path:
    configured = os.environ.get("YAAS_ROLLBACK_DIR")
    return Path(configured).expanduser() if configured else workspace_root.parent / ".yaas-rollback"


def _archive_legacy_files(root: Path) -> Path:
    destination = _backup_root(root)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{root.name}-{_timestamp()}-{secrets.token_hex(4)}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for relative in BACKUP_ITEMS:
            path = root / relative
            if path.exists() or path.is_symlink():
                bundle.add(path, arcname=relative, recursive=True)
    archive.chmod(0o600)
    return archive


def _acquire_lock(root: Path) -> Path:
    lock = root / ".yaas" / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SystemExit(f"migration already in progress: {lock}") from exc
    with os.fdopen(descriptor, "w") as stream:
        stream.write(f"pid={os.getpid()}\n")
    return lock


def migrate_workspace(path: str | Path, name: str | None = None) -> tuple[Workspace, Path | None, bool]:
    """Migrate once, preserving legacy files and returning its rollback archive."""
    root = Path(path).expanduser().resolve()
    marker = root / ".yaas" / "instance.json"
    journal = root / ".yaas" / "migrations" / "journal.json"
    if marker.exists() and journal.exists():
        return load_workspace(root), None, False

    lock = _acquire_lock(root)
    archive: Path | None = None
    try:
        if marker.exists():
            workspace = load_workspace(root)
        else:
            archive = _archive_legacy_files(root)
            workspace = init_workspace(root, name)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({
            "completed": [MIGRATION_ID],
            "engine": "0.1.0.dev0",
            "backup": str(archive) if archive else None,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2) + "\n")
        journal.chmod(0o600)
        return workspace, archive, True
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
