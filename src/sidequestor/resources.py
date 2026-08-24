"""Materialize engine-owned resources into a workspace."""

from __future__ import annotations

import os
import shutil
from importlib.resources import as_file, files
from pathlib import Path

from .workspace import Workspace, ensure_reaction_watermark
from . import __version__


ENGINE_VERSION = __version__


def sync_resources(workspace: Workspace) -> Path:
    ensure_reaction_watermark(workspace)
    env_example = workspace.root / ".env.example"
    if not env_example.exists() or env_example.read_text() in {
        "# YAAS workspace configuration\n",
        "# Sidequestor workspace configuration\n",
    }:
        env_resource = files("sidequestor").joinpath("package_data", "env.example")
        env_example.write_text(env_resource.read_text())
    settings_example = workspace.root / "settings.json.example"
    if not settings_example.exists():
        settings_resource = files("sidequestor").joinpath("package_data", "settings.json.example")
        settings_example.write_text(settings_resource.read_text())
    destination = workspace.yaas_dir / "engine" / ENGINE_VERSION
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    packaged_skills = files("sidequestor").joinpath("package_data", "skills")
    with as_file(packaged_skills) as source:
        shutil.copytree(source, destination / "skills")
    operating = destination / "OPERATING.md"
    operating_resource = files("sidequestor").joinpath("package_data", "OPERATING.md")
    operating.write_text(operating_resource.read_text())
    current = workspace.yaas_dir / "engine" / "current"
    temporary = current.with_name(".current.tmp")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    temporary.symlink_to(ENGINE_VERSION, target_is_directory=True)
    os.replace(temporary, current)
    for sibling in (destination.parent).iterdir():
        if sibling.name in {ENGINE_VERSION, "current"}:
            continue
        if sibling.is_dir() and not sibling.is_symlink():
            shutil.rmtree(sibling)
    return destination
