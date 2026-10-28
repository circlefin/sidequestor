"""Guided onboarding: one quest per workspace, driven from Slack or from a terminal agent.

The stages live in the bundled ``yaas-onboarding`` skill so upgrades improve onboardings
already in flight. This module only creates, finds, resumes and re-routes the quest. Its
``onboarding.json`` marker stores progress and CLI-owned ``onboarding-channel.json``
records which channel the owner chose; the channel is a
preference, not a separate flow, so ``sq onboard --slack`` or ``--terminal`` switches it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timezone
from pathlib import Path

from .native import RUNTIME_ROOT, _environment
from .quest_terminal import QuestTerminalError, _exec_agent, load_quest
from .resources import current_engine_version, ENGINE_VERSION, sync_resources
from .workspace import Workspace


MARKER = "onboarding.json"
CHANNEL_MARKER = "onboarding-channel.json"
CHANNELS = ("slack", "terminal")
INTERFACE_ID = re.compile(r"[CG][A-Z0-9]{2,}")
TITLE = "Sidequestor onboarding"
SKILL = ".yaas/engine/current/skills/yaas-onboarding/SKILL.md"
_CREATED = re.compile(r"^✓ Created ([a-z0-9-]+)$", re.MULTILINE)


def find_quest(workspace: Workspace, *, include_closed: bool = False) -> Path | None:
    """Find a marked onboarding, including a scaffold whose initial marker write failed."""
    buckets = ("active", "completed", "archived") if include_closed else ("active",)
    for bucket in buckets:
        directory = workspace.state / "quests" / bucket
        if not directory.is_dir():
            continue
        for quest_dir in sorted(directory.iterdir()):
            if (quest_dir / MARKER).is_file():
                return quest_dir
            try:
                meta = json.loads((quest_dir / "meta.json").read_text())
                context = (quest_dir / "context.md").read_text()
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("title") == TITLE and SKILL in context:
                return quest_dir
    return None


def read_marker(quest_dir: Path) -> dict:
    try:
        value = json.loads((quest_dir / MARKER).read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid progress")
        value.pop("slack_channel_id", None)  # selected destination is CLI-owned, never worker progress
        preference = quest_dir / CHANNEL_MARKER
        if preference.exists():
            selected_preference = json.loads(preference.read_text())
            channel = selected_preference.get("channel")
            if channel not in CHANNELS:
                raise ValueError("invalid channel")
            value["channel"] = channel
            selected = selected_preference.get("slack_channel_id")
            if selected is not None:
                if not isinstance(selected, str) or not INTERFACE_ID.fullmatch(selected):
                    raise ValueError("invalid interface channel")
                value["slack_channel_id"] = selected
    except (OSError, ValueError, AttributeError) as exc:
        raise QuestTerminalError(f"cannot read onboarding state in {quest_dir}; restore its progress file before resuming") from exc
    return value


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def _write_marker(quest_dir: Path, marker: dict) -> None:
    _write_json(quest_dir / MARKER, marker)


def slack_enabled(workspace: Workspace) -> bool:
    environment = _environment(workspace)
    connectors = environment.get("SIDEQUESTOR_CHECKER_CONNECTORS", "slack,email,github,jira")
    return (environment.get("SIDEQUESTOR_SLACK_CHECKERS_ENABLED") == "1"
            and "slack" in {part.strip() for part in connectors.split(",")})


def local_timezone() -> str:
    """Best-effort IANA name for the schedule watch; UTC when it cannot be determined."""
    configured = os.environ.get("TZ", "").lstrip(":")
    candidates = [configured]
    try:
        _, _, name = os.readlink("/etc/localtime").partition("zoneinfo/")
        candidates.append(name)
    except OSError:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            ZoneInfo(name)
            return name
        except (ValueError, ZoneInfoNotFoundError):
            continue
    return "UTC"


def _context(channel: str) -> str:
    return (
        "### Objective\n\n"
        "Walk the owner through Sidequestor onboarding: Stage 0 (what is already here), "
        "Stage A (configuration checks), Stage B (AGENTS.md and skills), Stage C (quests) and "
        "Stage D (check-ins until day 30), then close this quest.\n\n"
        "### Procedure\n\n"
        f"Load `{SKILL}` on every dispatch and follow it. It is the source of truth for the "
        "stages and ground rules; this file only holds the latest state.\n\n"
        "### Channel\n\n"
        f"**Chosen channel:** read `onboarding-channel.json` in this folder (initially `{channel}`). "
        "The owner may switch with `sq onboard --slack` or `sq onboard --terminal`.\n\n"
        "### Send boundary\n\n"
        "`allow_send` is false. Post only in the live-verified owner's Slack DM or the "
        "private Sidequestor interface channel the owner explicitly approved, listed in "
        "`allow_send_channels` in `meta.json`. Read the selected conversation from "
        "`onboarding-channel.json`. Never post elsewhere or through other send surfaces. "
        "Early in onboarding, offer to create a private Slack interface so the owner "
        "can use Sidequestor and its harness in plain language without a terminal."
    )


def create_quest(workspace: Workspace, channel: str, *, run=subprocess.run,
                 now: float | None = None) -> Path:
    """Scaffold the onboarding quest through the same helper as every other quest."""
    if channel not in CHANNELS:
        raise QuestTerminalError(f"channel must be one of {', '.join(CHANNELS)}")
    spec: dict = {
        "title": TITLE,
        "priority": "high",
        "allow_send": False,
        "allow_send_channels": [],
        "context": _context(channel),
        "note": f"Onboarding started via {channel}",
        "retire_slack_threads_after_days": False,
    }
    if channel == "slack":
        # Slack needs the owner's DM resolved and verified before any watch exists, which
        # is exactly what the bootstrap dispatch does.
        spec.update({"sidequestor_bootstrap": True, "watches": []})
    else:
        # The terminal session drives Stages 0-C; the loop only needs a daily heartbeat for
        # Stage D check-ins and nudges, so no bootstrap dispatch competes with the session.
        spec["watches"] = [{
            "type": "schedule", "cron": "0 9 * * *", "tz": local_timezone(),
            "reason": "daily onboarding heartbeat: Stage D check-ins and stalled-stage nudges",
        }]
    helper = RUNTIME_ROOT / "yaas-triage" / "skills" / "yaas-quest-creation" / "new-quest.py"
    result = _run_helper(workspace, helper, [json.dumps(spec)], run)
    match = _CREATED.search(result.stdout or "")
    if result.returncode or not match:
        detail = ((result.stderr or result.stdout) or "").strip()[:500]
        raise QuestTerminalError(f"could not create the onboarding quest: {detail}")
    quest_dir = workspace.state / "quests" / "active" / match.group(1)
    stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    _write_marker(quest_dir, {
        "version": 1,
        "channel": channel,
        "started_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stage": "0",
        "stage_c_completed_at": None,
    })
    _write_json(quest_dir / CHANNEL_MARKER, {"channel": channel})
    return quest_dir


def _trigger_soon(workspace: Workspace, quest_id: str, reason: str, *, run=subprocess.run,
                  now: float | None = None) -> None:
    """Wake the quest on the next tick with a one-shot schedule watch."""
    stamp = time.time() if now is None else now
    for existing in _read_watches(workspace.state / "quests" / "active" / quest_id):
        try:
            if (existing.get("type") == "schedule"
                    and existing.get("reason") == reason
                    and float(existing.get("next_fire_ts", 0)) > max(stamp, float(existing.get("last_checked_ts") or 0))):
                return
        except (TypeError, ValueError):
            continue
    fire = int(stamp + 60)
    watch = {"type": "schedule", "next_fire_ts": str(fire), "reason": reason}
    helper = RUNTIME_ROOT / "yaas-triage" / "ledger" / "add-watch.py"
    result = _run_helper(workspace, helper, [quest_id, json.dumps(watch)], run)
    if result.returncode:
        detail = ((result.stderr or result.stdout) or "").strip()[:500]
        raise QuestTerminalError(f"could not schedule the Slack hand-off: {detail}")


def terminal_prompt(quest_id: str, resumed: bool) -> str:
    opening = ("Resume my Sidequestor onboarding where it left off." if resumed
               else "Start my Sidequestor onboarding.")
    return "\n".join([
        opening,
        f"The onboarding quest is {quest_id} in state/quests/active/{quest_id}/.",
        f"Load {SKILL} and follow it, using this terminal session as the channel: talk to me "
        "here and wait for my OK here.",
        "Read the quest's context.md, onboarding.json, onboarding-channel.json and recent timeline first, and tell me "
        "in one line which stage we are on.",
    ])


def _run_helper(workspace, helper, args, run):
    try:
        return run([sys.executable, str(helper), *args], cwd=workspace.root,
                   env=_environment(workspace), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuestTerminalError(f"onboarding helper could not finish: {type(exc).__name__}") from exc


def _read_watches(quest_dir):
    try:
        document = json.loads((quest_dir / "watch.json").read_text())
        watches = document["watches"]
        if not isinstance(watches, list) or not all(isinstance(w, dict) for w in watches):
            raise ValueError("invalid watches")
        return watches
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise QuestTerminalError(f"cannot read onboarding watches in {quest_dir}") from exc


def _ensure_heartbeat(workspace, quest_dir, run):
    reason = "daily onboarding heartbeat: Stage D check-ins and stalled-stage nudges"
    if any(w.get("type") == "schedule" and w.get("cron") == "0 9 * * *"
           and w.get("reason") == reason for w in _read_watches(quest_dir)):
        return
    watch = {"type": "schedule", "cron": "0 9 * * *", "tz": local_timezone(), "reason": reason}
    helper = RUNTIME_ROOT / "yaas-triage" / "ledger" / "add-watch.py"
    result = _run_helper(workspace, helper, [quest_dir.name, json.dumps(watch)], run)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise QuestTerminalError(f"could not schedule onboarding heartbeat: {detail}")


def run_onboard(workspace: Workspace, channel: str | None, requested_agent: str | None = None,
                *, run=subprocess.run, execve=os.execve, now: float | None = None,
                slack_channel_id: str | None = None, slack_dm: bool = False) -> int:
    """Start or resume a single quest; release the creation lock before entering the agent."""
    if slack_dm:
        if slack_channel_id is not None or channel == "terminal":
            raise QuestTerminalError("choose a Slack DM, interface channel, or terminal")
        channel = "slack"
    if slack_channel_id is not None:
        if not isinstance(slack_channel_id, str) or not INTERFACE_ID.fullmatch(slack_channel_id):
            raise QuestTerminalError("interface channel must be a Slack conversation ID (C…/G…)")
        if channel == "terminal":
            raise QuestTerminalError("choose a Slack interface channel or terminal")
        channel = "slack"
    selected_channel_id = None
    if channel is not None and channel not in CHANNELS:
        raise QuestTerminalError(f"channel must be one of {', '.join(CHANNELS)}")
    try:
        with (workspace.state / "onboarding.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (current_engine_version(workspace) != ENGINE_VERSION
                    or not (workspace.root / SKILL).is_file()):
                sync_resources(workspace)
            quest_dir = find_quest(workspace)
            resumed = quest_dir is not None
            if quest_dir is None:
                closed = find_quest(workspace, include_closed=True)
                if closed is not None:
                    print(f"Onboarding already finished ({closed.name}); no new quest was created.")
                    return 0
                if slack_channel_id is not None:
                    raise QuestTerminalError("start onboarding before selecting its approved interface channel")
                channel = channel or ("slack" if slack_enabled(workspace) else "terminal")
                if channel == "slack" and not slack_enabled(workspace):
                    raise QuestTerminalError("Slack checkers are disabled; use sq onboard --terminal or enable Slack through sq setup")
                quest_dir = create_quest(workspace, channel, run=run, now=now)
                print(f"Created onboarding quest {quest_dir.name}.")
            else:
                marker = read_marker(quest_dir)
                current = marker.get("channel") if marker.get("channel") in CHANNELS else "slack"
                channel = channel or current
                selected_channel_id = None if slack_dm else slack_channel_id or marker.get("slack_channel_id")
                if channel == "slack" and selected_channel_id:
                    meta = load_quest(workspace, quest_dir.name).meta
                    grants = meta.get("allow_send_channels")
                    if not isinstance(grants, list) or selected_channel_id not in grants:
                        raise QuestTerminalError("interface channel needs an owner-approved send grant before selection")
                    if not any(w.get("type") == "slack_channel" and w.get("channel_id") == selected_channel_id
                               for w in _read_watches(quest_dir)):
                        raise QuestTerminalError("interface channel needs a slack_channel watch before selection")
                if channel == "slack" and not slack_enabled(workspace):
                    raise QuestTerminalError("Slack checkers are disabled; use sq onboard --terminal or enable Slack through sq setup")
                if channel == "terminal":
                    _ensure_heartbeat(workspace, quest_dir, run)
                elif load_quest(workspace, quest_dir.name).meta.get("sidequestor_bootstrap") is not True:
                    _trigger_soon(workspace, quest_dir.name,
                                  "continue onboarding in the selected Slack conversation", run=run, now=now)
                if channel != current or slack_channel_id is not None or slack_dm or not (quest_dir / CHANNEL_MARKER).exists():
                    preference = {"channel": channel}
                    if selected_channel_id:
                        preference["slack_channel_id"] = selected_channel_id
                    _write_json(quest_dir / CHANNEL_MARKER, preference)
                if channel != current:
                    print(f"Moved onboarding to {channel}.")
            quest = load_quest(workspace, quest_dir.name)
    except (OSError, ValueError) as exc:
        raise QuestTerminalError(f"could not prepare onboarding: {exc}") from exc
    if channel == "slack":
        destination = f"Slack channel {selected_channel_id}" if selected_channel_id else "Slack DM"
        print(f"{destination} onboarding is queued. The Sidequestor loop and Slack authentication must be working (check sq doctor).")
        print("To continue here instead: sq onboard --terminal")
        return 0
    try:
        return _exec_agent(workspace, terminal_prompt(quest.id, resumed), requested_agent,
                           {"SQ_QUEST_ID": quest.id, "SQ_QUEST_DIR": str(quest.dir)}, execve)
    except OSError as exc:
        raise QuestTerminalError(f"could not start the onboarding agent: {exc}") from exc
