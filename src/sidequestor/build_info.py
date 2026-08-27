"""Build identity exposed by the package and dashboard."""

from __future__ import annotations

import json
import subprocess
from importlib import metadata
from pathlib import Path

from . import __version__
from .resources import ENGINE_VERSION

_BUILD_INFO: dict[str, str] | None = None


def build_info() -> dict[str, str]:
    """Return the immutable identity of this running package build."""
    global _BUILD_INFO
    if _BUILD_INFO is not None:
        return dict(_BUILD_INFO)
    commit_full = ""
    ref = ""
    source = "unknown"
    try:
        direct_url = metadata.distribution("sidequestor").read_text("direct_url.json")
        if direct_url:
            data = json.loads(direct_url)
            vcs_info = data.get("vcs_info", {})
            commit_full = str(vcs_info.get("commit_id", "") or "")
            ref = str(vcs_info.get("requested_revision", "") or "")
            if commit_full:
                source = "install"
    except Exception:
        pass
    if not commit_full:
        checkout = Path(__file__).resolve().parents[2]
        if not (checkout / "src" / "sidequestor" / "__init__.py").exists():
            checkout = None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=checkout, check=False,
                capture_output=True, text=True, timeout=2,
            ) if checkout else None
            commit_full = result.stdout.strip() if result and result.returncode == 0 else ""
            if commit_full:
                source = "checkout"
        except Exception:
            pass
    _BUILD_INFO = {
        "version": __version__, "commit": commit_full[:7],
        "commit_full": commit_full, "ref": ref, "source": source,
        "engine": ENGINE_VERSION,
    }
    return dict(_BUILD_INFO)
