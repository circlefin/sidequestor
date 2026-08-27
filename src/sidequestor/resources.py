"""Materialize engine-owned resources into a workspace."""

from __future__ import annotations

import os
import shutil
from importlib.resources import as_file, files
from pathlib import Path

from .workspace import Workspace, ensure_reaction_watermark
from . import __version__


ENGINE_VERSION = __version__


MANAGED_MARKER = "<!-- managed by sidequestor; edits are overwritten on sync -->"
NEW_QUEST_PROMPT = f"""{MANAGED_MARKER}
Read `.yaas/engine/current/OPERATING.md` first, then `.yaas/engine/current/skills/yaas-quest-creation/SKILL.md`.

When you are ready to scaffold the quest, run:

sq new-quest '<spec_json>'
"""


def current_engine_version(workspace: Workspace) -> str | None:
    """Read the active engine version from the `current` symlink only.

    The runtime flips `.yaas/engine/current` atomically on upgrade, so a readlink is
    enough to detect drift without walking the copied tree.
    """
    current = workspace.yaas_dir / "engine" / "current"
    try:
        target = os.readlink(current)
    except OSError:
        return None
    # A symlink naming the CURRENT version but pointing at a deleted directory would
    # otherwise read as "no drift", so the auto-sync would skip the one repair it
    # exists to perform and hand workers a broken engine root.
    if not current.resolve().is_dir():
        return None
    return Path(target).name or None


def _safe_managed_directory(path: Path) -> Path | None:
    """Create a managed directory only when no user file blocks the parent path."""
    if path.exists():
        return path if path.is_dir() else None
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sync_skill_links(directory: Path | None, relative_prefix: str, names: set[str]) -> None:
    """Publish engine skills as symlinks into a directory Sidequestor does not own.

    `skills/` and `.claude/skills/` belong to the user; this writes into them so an
    interactive agent can discover the engine skills without being told a path. That
    makes the blast radius the important part of this function.

    THE CLAIM: Sidequestor owns the `yaas-` prefix inside these directories, and nothing
    else. Within that namespace it will DELETE a symlink it did not create — one whose
    name no longer ships, or whose target has moved. That is deliberate: a workspace that
    survives several upgrades would otherwise accumulate links to skills that no longer
    exist, and a stale link is worse than a missing one because an agent will follow it.
    The cost is real, so state it plainly: a hand-made `yaas-*` symlink here WILL be
    replaced. Name yours anything else and it is safe.

    Regular files and directories are never touched, at any name. If one occupies a name
    we want, we skip it and publish nothing there — the user's copy shadows ours, which
    is the right way round for a file they created on purpose.

    Links point through `.yaas/engine/current`, which sync_resources() repoints on
    upgrade, so an engine bump needs no re-linking: the existing symlinks follow it.
    """
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    for entry in directory.iterdir():
        # Anything outside the `yaas-` namespace, and any real file, is not ours to judge.
        if not entry.name.startswith("yaas-") or not entry.is_symlink():
            continue
        if entry.name not in names:
            entry.unlink()          # skill no longer ships — drop the dangling link
            continue
        target = os.readlink(entry)
        expected = f"{relative_prefix}{entry.name}"
        if target != expected:
            entry.unlink()          # retargeted or hand-edited — rewritten below
    for name in sorted(names):
        entry = directory / name
        if entry.exists() and not entry.is_symlink():
            continue
        if entry.is_symlink():
            continue
        entry.symlink_to(f"{relative_prefix}{name}", target_is_directory=True)


def _sync_new_quest_prompt(workspace: Workspace) -> None:
    codex_root = _safe_managed_directory(workspace.root / ".codex")
    prompts = _safe_managed_directory(codex_root / "prompts") if codex_root else None
    if prompts is None:
        return
    prompt = prompts / "new-quest.md"
    if prompt.exists() and not prompt.is_file():
        return
    if prompt.is_file():
        # Refresh only what we wrote. Without the marker check, a user who edited this
        # prompt would silently lose their edits on every sync — and sync now runs
        # unattended on version drift, so that would happen without them asking for it.
        try:
            if MANAGED_MARKER not in prompt.read_text():
                return
        except OSError:
            return
    prompt.write_text(NEW_QUEST_PROMPT)


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
    # The skills tree lives once, under runtime/. It used to be duplicated into
    # package_data/ by the Sidequestor rebrand (815f26f) and the two forked: the
    # copy that shipped had lost the pip-workspace path handling, so new-quest.py
    # died on "cannot locate repo root" in every installed workspace.
    packaged_skills = files("sidequestor").joinpath("runtime", "yaas-triage", "skills")
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
    skill_names = {path.name for path in (destination / "skills").iterdir()
                   if path.is_dir() and path.name.startswith("yaas-")}
    # workspace.skills is passed straight in, while .claude/.codex go through
    # _safe_managed_directory(). Not an oversight: init_workspace() creates skills/ and
    # validate_workspace() requires it, so it is ours by construction. .claude/ and
    # .codex/ belong to other tools and may not exist, or may be something unexpected.
    _sync_skill_links(workspace.skills, "../.yaas/engine/current/skills/", skill_names)
    claude_root = _safe_managed_directory(workspace.root / ".claude")
    claude_skills = _safe_managed_directory(claude_root / "skills") if claude_root else None
    _sync_skill_links(claude_skills, "../../.yaas/engine/current/skills/", skill_names)
    _sync_new_quest_prompt(workspace)
    for sibling in (destination.parent).iterdir():
        if sibling.name in {ENGINE_VERSION, "current"}:
            continue
        if sibling.is_dir() and not sibling.is_symlink():
            shutil.rmtree(sibling)
    return destination
