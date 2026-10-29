"""Terminal sessions: ``sq ctx``, ``trail``, ``ask``, ``queue``, ``create`` and ``terminal``.

The dashboard's "Open in terminal" button opens a native terminal window attached to one
long-lived, named session per quest, so commands and agent conversations about a quest
collect in one place and a second click reattaches instead of starting over. "Create
interactively in Terminal" does the same for one workspace-level session whose ``create``
helper starts an agent on the quest-creation skill.

The session is scoped by convention only. It starts in the quest folder, exports the quest
ID, labels the prompt and greets with the quest's state, but it is an ordinary shell running
as the user: nothing stops it from reading or changing other quests. The agent started by
``ask`` is steered to the quest by its first message, which is advisory in the same way.

Untrusted text (titles, context, timeline entries that may quote Slack) is rendered by
Python at run time with control characters stripped. The generated shell files contain
only fixed code and ``shlex``-quoted values that Sidequestor itself controls.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .job_path import agent_binary_name, find_agent, selected_agent
from .native import RUNTIME_ROOT, _environment
from .workspace import Workspace


# Kept in step with the guard in yaas-triage/ops/dashboard-server.py.
QUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
MAX_INSTRUCTION_CHARS = 4000  # same limit as the dashboard composer
TRAIL_TAIL_BYTES = 256 * 1024
LAUNCH_THROTTLE_SECONDS = 3.0
AGENT_LABELS = {"codex": "Codex", "claude": "Claude Code", "cursor": "Cursor"}
AGENT_ORDER = ("codex", "claude", "cursor")
# Quest IDs start with a letter or digit, so this key can never name a quest's session.
NEW_QUEST_KEY = "_new-quest"
MULTIPLEXER_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
ZSH = "/bin/zsh"

_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class QuestTerminalError(Exception):
    """A user-facing failure; the CLI prints it instead of a traceback."""


@dataclass(frozen=True)
class Quest:
    id: str
    dir: Path
    meta: dict

    @property
    def title(self) -> str:
        return clean(self.meta.get("title") or self.id, single_line=True) or self.id

    @property
    def label(self) -> str:
        """Short prompt label; quest IDs are already shell- and prompt-safe."""
        return self.id.removeprefix("quest-")


def clean(value: object, *, single_line: bool = False) -> str:
    """Strip terminal escapes and control characters from untrusted text."""
    text = _CONTROLS.sub("", _ESCAPES.sub("", str(value)))
    return " ".join(text.split()) if single_line else text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def load_quest(workspace: Workspace, quest_id: str) -> Quest:
    if not QUEST_ID.fullmatch(quest_id or ""):
        raise QuestTerminalError(f"invalid quest id: {quest_id!r}")
    active = workspace.state / "quests" / "active"
    quest_dir = active / quest_id
    if not quest_dir.is_dir() or quest_dir.resolve().parent != active.resolve():
        raise QuestTerminalError(f"no active quest named {quest_id}")
    try:
        meta = json.loads((quest_dir / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    return Quest(quest_id, quest_dir, meta if isinstance(meta, dict) else {})


# ── Rendering ────────────────────────────────────────────────────────────────

def _timestamp(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value or "").strip()
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return number / 1000 if number > 1e12 else number


def _ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "?"
    seconds = max(0, now - ts)
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return ""


def _status_line(quest: Quest) -> str:
    meta = quest.meta
    channels = meta.get("allow_send_channels")
    scoped = [str(c) for c in channels if isinstance(c, str)] if isinstance(channels, list) else []
    permission = "send enabled" if meta.get("allow_send") is True else (
        "Slack text allowed: " + ", ".join(clean(c, single_line=True) for c in scoped)
        if scoped else "draft only")
    parts = [clean(meta.get("status") or "active", single_line=True),
             clean(meta.get("priority") or "normal", single_line=True) + " priority", permission]
    return " · ".join(parts)


_WATCH_TARGET_KEYS = ("channel_name", "channel_id", "user_name", "user_id", "partner",
                      "issue_key", "key", "repo", "number", "query", "peer", "chat",
                      "handle", "username", "url", "thread_ts", "name")
_LEGACY_WATCH_GROUPS = (("channels", "slack_channel"), ("threads", "slack_thread"),
                        ("reactions", "reaction"), ("dm_partners", "slack_dm"))


def _watches(quest: Quest) -> list[tuple[str, dict]]:
    try:
        data = json.loads((quest.dir / "watch.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    rows: list[tuple[str, dict]] = []
    for watch in data.get("watches") or []:
        if isinstance(watch, dict):
            rows.append((str(watch.get("type") or "watch"), watch))
    # The older watch.json shape groups Slack watches by kind instead of typing each one.
    for group, kind in _LEGACY_WATCH_GROUPS:
        for watch in data.get(group) or []:
            if isinstance(watch, dict):
                rows.append((kind, watch))
            elif isinstance(watch, str):
                rows.append((kind, {"partner": watch}))
    return rows


def render_context(quest: Quest) -> str:
    lines = [f"{quest.title} · {_status_line(quest)}", quest.id, "", "Mission"]
    mission = clean(_read_text(quest.dir / "context.md")).strip()
    lines.extend(f"  {line}" for line in (mission.splitlines() or ["No context.md yet."]))
    lines.extend(["", "Watching"])
    watches = _watches(quest)
    for kind, watch in watches:
        target = next((watch[key] for key in _WATCH_TARGET_KEYS if watch.get(key)), "")
        row = f"  • {clean(kind, single_line=True)}"
        if target:
            row += f"  {clean(target, single_line=True)}"
        reason = clean(watch.get("reason") or "", single_line=True)
        if reason:
            row += f"  — {reason}"
        lines.append(_clip(row, 160))
        error = clean(watch.get("last_error") or "", single_line=True)
        if error:
            lines.append(_clip(f"    last error: {error}", 160))
    if not watches:
        lines.append("  Nothing yet.")
    return "\n".join(lines)


def _timeline(quest: Quest) -> list[dict]:
    """Read only the newest part of the timeline; it grows for the life of the quest."""
    path = quest.dir / "timeline.ndjson"
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRAIL_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > TRAIL_TAIL_BYTES and lines:
        lines = lines[1:]  # the first line is probably cut mid-record
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    # Stable sort: equal or missing timestamps keep file order.
    return sorted(entries, key=lambda e: _timestamp(e.get("ts")) or 0.0, reverse=True)


def _trail_lines(quest: Quest, limit: int | None, now: float) -> list[str]:
    entries = _timeline(quest)
    rows = []
    for entry in entries if limit is None else entries[:limit]:
        event = clean(entry.get("event") or "note", single_line=True)
        detail = clean(entry.get("note") or entry.get("message_text") or entry.get("reason") or "",
                       single_line=True)
        rows.append(_clip(f"{_ago(_timestamp(entry.get('ts')), now):>9}  {event:<16} {detail}".rstrip(), 160))
    return rows


def render_trail(quest: Quest, limit: int | None = 15, now: float | None = None) -> str:
    rows = _trail_lines(quest, limit, time.time() if now is None else now)
    return "\n".join([f"{quest.title} · recent activity", *(rows or ["  Nothing logged yet."])])


# ── Agents ───────────────────────────────────────────────────────────────────

def agent_label(agent: str) -> str:
    return AGENT_LABELS.get(agent, agent)


def resolve_agent(workspace: Workspace, path: str, requested: str | None = None
                  ) -> tuple[str | None, str | None, str | None]:
    """Return (agent, binary, notice). An explicit request never falls back silently."""
    if requested:
        binary = find_agent(requested, path)
        if not binary:
            raise QuestTerminalError(
                f"{agent_label(requested)} is not installed ({agent_binary_name(requested)} not found on PATH)")
        return requested, binary, None
    configured = selected_agent(workspace)
    binary = find_agent(configured, path)
    if binary:
        return configured, binary, None
    for other in AGENT_ORDER:
        if other != configured and (binary := find_agent(other, path)):
            return other, binary, (f"{agent_label(configured)} is not installed; "
                                   f"using {agent_label(other)} instead.")
    return None, None, ("No agent CLI found. Install Codex, Claude Code, or Cursor's "
                        "cursor-agent, then run `help` again.")


def seed_prompt(quest: Quest, request: str = "") -> str:
    lines = [
        f'You are helping with the Sidequestor quest "{quest.title}" ({quest.id}).',
        f"Its files are in state/quests/active/{quest.id}/: context.md (the mission), "
        "meta.json, watch.json and timeline.ndjson.",
        "Read context.md and the recent timeline before acting. Work only on this quest "
        "unless I say otherwise.",
        "Do not send messages, post, or queue instructions on my behalf without asking me first.",
        "",
        f"My request: {request}" if request.strip()
        else "Summarise where this quest stands in a few lines, then ask me what to do next.",
    ]
    return "\n".join(lines)


def creation_prompt(mission: str = "") -> str:
    lines = [
        "I want to create a new Sidequestor quest in this workspace.",
        "Use the Sidequestor quest-creation skill (yaas-quest-creation, scaffolded with "
        "`sq new-quest`). Ask me for the title, who and what to watch, and what done looks "
        "like, and show me the quest before you create it.",
        "",
        f"The mission: {mission}" if mission.strip() else "Start by asking me what the quest is for.",
    ]
    return "\n".join(lines)


def _exec_agent(workspace: Workspace, prompt: str, requested: str | None,
                extra_env: dict[str, str], execve) -> int:
    environment = _environment(workspace)
    agent, binary, notice = resolve_agent(workspace, environment.get("PATH", ""), requested)
    if notice:
        print(notice, file=sys.stderr)
    if not binary:
        return 1
    environment.update(extra_env)
    # The agent works from the workspace root, where its AGENTS.md/CLAUDE.md and `sq` live.
    os.chdir(workspace.root)
    execve(binary, [binary, prompt], environment)
    return 0  # only reached when execve is replaced in tests


def run_ask(workspace: Workspace, quest: Quest, request: str, requested: str | None = None,
            *, execve=os.execve) -> int:
    return _exec_agent(workspace, seed_prompt(quest, request), requested,
                       {"SQ_QUEST_ID": quest.id, "SQ_QUEST_DIR": str(quest.dir)}, execve)


def run_create(workspace: Workspace, mission: str, requested: str | None = None,
               *, execve=os.execve) -> int:
    return _exec_agent(workspace, creation_prompt(mission), requested, {}, execve)


# ── Queue ────────────────────────────────────────────────────────────────────

def queue_instruction(workspace: Workspace, quest: Quest, text: str, *,
                      stdin=None, ask=input, run=subprocess.run) -> dict:
    """Queue an instruction exactly as the dashboard composer does, after a human confirms.

    The queued item is born reviewed, so the next tick acts on it. Requiring a typed
    confirmation on a real terminal keeps an agent running in the same session (whose
    shell commands have no TTY) from queueing work on its own.
    """
    instruction = (text or "").strip()
    if not instruction:
        raise QuestTerminalError("instruction is required")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise QuestTerminalError(f"instruction too long (max {MAX_INSTRUCTION_CHARS} chars)")
    stream = sys.stdin if stdin is None else stdin
    if not stream.isatty():
        raise QuestTerminalError("sq queue must be confirmed at an interactive terminal; "
                                 "use the dashboard composer otherwise")
    print(f"Queue for {quest.title}:\n  {clean(instruction)}")
    if ask("Queue this instruction for the next triage tick? [y/N] ").strip().lower() not in {"y", "yes"}:
        raise QuestTerminalError("not queued")
    request = {
        "quest_id": quest.id,
        "quest_title": quest.title,
        "instruction": instruction,
        "context": "Submitted from the quest terminal.",
    }
    helper = RUNTIME_ROOT / "yaas-triage" / "ledger" / "approval-helper.py"
    completed = run(
        [sys.executable, str(helper), "enqueue-instruction", json.dumps(request)],
        capture_output=True, text=True, timeout=10, cwd=str(workspace.root),
        env=_environment(workspace),
    )
    try:
        result = json.loads((completed.stdout or "").strip())
    except ValueError:
        result = {}
    if completed.returncode != 0 or not isinstance(result, dict) or not result.get("queued"):
        detail = (completed.stderr or "").strip()[:300]
        raise QuestTerminalError("instruction could not be queued" + (f": {detail}" if detail else ""))
    return result


# ── Sessions ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionTarget:
    """What one named terminal session is about: a quest, or creating a new one."""
    key: str                  # session and state/terminals/ directory key
    title: str                # window title, after "Sidequestor · "
    cwd: Path
    label: str                # prompt label; only [A-Za-z0-9._-]
    exports: tuple[tuple[str, str], ...]
    helpers: tuple[tuple[str, tuple[str, ...]], ...]  # shell function -> sq arguments
    welcome: tuple[str, ...]  # sq arguments that print the welcome screen
    prefill: str              # pre-typed command when an agent CLI is installed


def quest_target(quest: Quest) -> SessionTarget:
    return SessionTarget(
        key=quest.id, title=quest.title, cwd=quest.dir, label=quest.label,
        exports=(("SQ_QUEST_ID", quest.id), ("SQ_QUEST_DIR", str(quest.dir))),
        helpers=tuple((name, (name, quest.id)) for name in ("ask", "ctx", "trail", "queue")),
        welcome=("terminal", quest.id, "--welcome"), prefill='ask "',
    )


def new_quest_target(workspace: Workspace) -> SessionTarget:
    return SessionTarget(
        key=NEW_QUEST_KEY, title="New quest", cwd=workspace.root, label="new-quest",
        exports=(), helpers=(("create", ("create",)),),
        welcome=("terminal", "--new-quest", "--welcome"), prefill='create "',
    )


def session_name(workspace: Workspace, target: SessionTarget) -> str:
    """Workspace-qualified, because tmux and screen names are global to the user."""
    instance = re.sub(r"[^A-Za-z0-9]", "", workspace.instance_id) or "local"
    # Escape underscores first so dotted and underscored quest IDs stay distinct.
    key = target.key.replace("_", "_u").replace(".", "_d")
    if target.key == NEW_QUEST_KEY:
        key = NEW_QUEST_KEY
    return f"sq-{instance}-{key}"


def find_multiplexer(path: str) -> tuple[str, str | None]:
    search = os.pathsep.join([*(part for part in path.split(os.pathsep) if part), *MULTIPLEXER_DIRS])
    for kind in ("tmux", "screen"):
        binary = shutil.which(kind, path=search)
        if binary:
            return kind, binary
    return "none", None


def _screen_sessions(binary: str, name: str, run=subprocess.run) -> list[str]:
    listing = run([binary, "-ls"], capture_output=True, text=True, timeout=5).stdout or ""
    return re.findall(rf"^\s*(\d+\.{re.escape(name)})\s", listing, flags=re.MULTILINE)


def session_running(kind: str, binary: str | None, name: str, run=subprocess.run) -> bool:
    if kind == "tmux" and binary:
        return run([binary, "has-session", "-t", "=" + name], capture_output=True, timeout=5).returncode == 0
    if kind == "screen" and binary:
        return bool(_screen_sessions(binary, name, run))
    return False


def _session_dir(workspace: Workspace, target: SessionTarget) -> Path:
    root = workspace.state / "terminals"
    directory = root / target.key
    for path in (root, directory):
        if path.is_symlink():
            raise QuestTerminalError(f"refusing to use symlinked session directory: {path}")
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    return directory


def _write_atomic(path: Path, content: str, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
    os.replace(temporary, path)  # rename replaces a symlink itself, never its target


def zshenv(workspace: Workspace, target: SessionTarget, prefill: bool) -> str:
    """Startup file for the session's zsh, found through ZDOTDIR.

    It restores the user's own ZDOTDIR and sources their .zshenv, so zsh then reads their
    .zprofile, .zshrc and .zlogin normally, exactly once. The helpers are installed by a
    one-shot precmd hook, i.e. after all of the user's startup files, so a same-named
    alias or prompt framework there cannot undo them before the first prompt.
    """
    q = shlex.quote
    sq = f"{q(sys.executable)} -m sidequestor --workspace {q(str(workspace.root))}"
    names = [name for name, _ in target.helpers] + ["help"]
    exports = " ".join(f"{key}={q(value)}" for key, value in
                       (*target.exports, ("SIDEQUESTOR_WORKSPACE", str(workspace.root))))
    return "\n".join([
        f"# Generated by Sidequestor for {target.key}; rewritten each time its terminal opens.",
        f"export {exports}",
        'ZDOTDIR="${SQ_USER_ZDOTDIR:-$HOME}"',
        'unset SQ_USER_ZDOTDIR',
        '[[ -r "$ZDOTDIR/.zshenv" ]] && source "$ZDOTDIR/.zshenv"',
        # Aliases expand while a function is parsed, before its unalias can run.
        f"unalias {' '.join(names)} 2>/dev/null",
        "",
        "_sq_session_setup() {",
        "  add-zsh-hook -d precmd _sq_session_setup",
        f"  unalias {' '.join(names)} 2>/dev/null",
        f"  _sq() {{ {sq} \"$@\"; }}",
        *(f"  {name}() {{ _sq {' '.join(q(arg) for arg in args)} \"$@\"; }}" for name, args in target.helpers),
        f"  help() {{ _sq {' '.join(q(arg) for arg in target.welcome)}; }}",
        # Prompt frameworks rebuild PROMPT, so re-add the label before every prompt.
        f"  _sq_prompt_label() {{ [[ $PROMPT == *'[sq:'* ]] || PROMPT=\"%F{{cyan}}[sq:{target.label}]%f $PROMPT\"; }}",
        "  add-zsh-hook precmd _sq_prompt_label",
        "  _sq_prompt_label",
        f"  cd -q {q(str(target.cwd))} 2>/dev/null",
        "  help",
        *([f"  print -z {q(target.prefill)}"] if prefill else []),
        "}",
        "autoload -Uz add-zsh-hook",
        "add-zsh-hook precmd _sq_session_setup",
        "",
    ])


def launcher(target: SessionTarget, session_dir: Path, name: str, kind: str, binary: str | None,
             existing_screen: str | None) -> str:
    q = shlex.quote
    shell = f'env ZDOTDIR={q(str(session_dir))} SQ_USER_ZDOTDIR="${{ZDOTDIR:-$HOME}}" {ZSH} -l -i'
    if kind == "tmux":
        # -A attaches when the session exists, so a second window mirrors the first.
        attach = f"exec {q(binary)} new-session -A -s {q(name)} -c {q(str(target.cwd))} {q(shell)}"
    elif kind == "screen" and existing_screen:
        # -x joins without detaching the other window; the pid-qualified name is unambiguous.
        attach = f"exec {q(binary)} -x {q(existing_screen)}"
    elif kind == "screen":
        attach = f"exec {q(binary)} -S {q(name)} {shell}"
    else:
        attach = f"exec {shell}"
    return "\n".join([
        "#!/bin/zsh",
        f"# Generated by Sidequestor for {target.key}; rewritten each time its terminal opens.",
        f"printf '\\033]0;%s\\007' {q('Sidequestor · ' + target.title)}",
        f"cd {q(str(target.cwd))} || exit 1",
        attach,
        "",
    ])


def open_terminal(workspace: Workspace, target: SessionTarget, app: str | None = None,
                  *, run=subprocess.run, now=time.time) -> dict:
    environment = _environment(workspace)
    app = app or environment.get("SIDEQUESTOR_TERMINAL_APP") or "Terminal"
    kind, binary = find_multiplexer(environment.get("PATH", ""))
    name = session_name(workspace, target)
    directory = _session_dir(workspace, target)
    command_file = directory / "open.command"
    result = {"ok": True, "target": target.key, "session": name, "multiplexer": kind, "app": app}
    # The lock serialises concurrent clicks; the throttle covers a double click whose first
    # window has not created the session yet (screen accepts duplicate session names).
    with os.fdopen(os.open(directory / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        running = session_running(kind, binary, name, run)
        try:
            recent = now() - command_file.stat().st_mtime < LAUNCH_THROTTLE_SECONDS
        except OSError:
            recent = False
        if recent and not running:
            return {**result, "status": "throttled"}
        _, agent_binary, _ = resolve_agent(workspace, environment.get("PATH", ""))
        existing = (_screen_sessions(binary, name, run) or [None])[0] if kind == "screen" and running else None
        _write_atomic(directory / ".zshenv", zshenv(workspace, target, prefill=bool(agent_binary)), 0o600)
        _write_atomic(command_file, launcher(target, directory, name, kind, binary, existing), 0o700)
        completed = run(["open", "-a", app, str(command_file)], capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "open failed").strip()[:200]
            raise QuestTerminalError(f"could not open {app}: {detail}")
    # `open` returns once the app accepts the file, before the session exists.
    return {**result, "status": "reattach_requested" if running else "launch_requested"}


def _agent_tries(workspace: Workspace, verb: str, purpose: str) -> tuple[list[tuple[str, str]], str | None]:
    """`purpose` names the agent through `{agent}`, e.g. "ask {agent} about this quest"."""
    path = _environment(workspace).get("PATH", "")
    agent, _, notice = resolve_agent(workspace, path)
    tries = []
    if agent:
        tries.append((f'{verb} "…"', purpose.format(agent=agent_label(agent))))
        others = [a for a in AGENT_ORDER if a != agent and find_agent(a, path)]
        if others:
            tries.append((f'{verb} --{others[0]} "…"', f"use {agent_label(others[0])} instead"))
    return tries, notice


def _try_block(tries: list[tuple[str, str]], notice: str | None) -> list[str]:
    width = max(len(command) for command, _ in tries) + 2
    lines = ["│", "│ Try", *(f"│   {command:<{width}}{text}" for command, text in tries)]
    return lines + (["│", f"│ {notice}"] if notice else [])


def render_welcome(workspace: Workspace, quest: Quest, now: float | None = None) -> str:
    mission = [line for line in clean(_read_text(quest.dir / "context.md")).splitlines()
               if line.strip() and not line.lstrip().startswith("#")][:3]
    recent = _trail_lines(quest, 3, time.time() if now is None else now)
    lines = [f"╭─ Sidequestor · {quest.title}", f"│ {quest.id} · {_status_line(quest)}", "│"]
    lines.extend(_clip(f"│ {'Mission' if i == 0 else '       '}  {line.strip()}", 100)
                 for i, line in enumerate(mission or ["No context.md yet."]))
    lines.extend(f"│ {'Recent' if i == 0 else '      '} {line}" for i, line in enumerate(recent))
    tries, notice = _agent_tries(workspace, "ask", "ask {agent} about this quest")
    tries.extend([
        ("ctx", "mission and watches"),
        ("trail", "recent timeline"),
        ('queue "…"', "queue an instruction for the next triage tick"),
        ("help", "show this again"),
    ])
    lines.extend(_try_block(tries, notice))
    lines.append("╰─ This is a normal shell: it is not limited to this quest.")
    return "\n".join(lines)


def render_new_quest_welcome(workspace: Workspace) -> str:
    lines = [
        "╭─ Sidequestor · New quest",
        f"│ {clean(workspace.display_name, single_line=True)} · {workspace.root}",
        "│",
        "│ Describe the mission; the agent asks what to watch and what done looks like,",
        "│ and shows you the quest before creating it.",
    ]
    tries, notice = _agent_tries(workspace, "create", "create a quest with {agent}")
    tries.append(("help", "show this again"))
    lines.extend(_try_block(tries, notice))
    lines.append("╰─ This is a normal shell in your workspace.")
    return "\n".join(lines)


# ── Command line ─────────────────────────────────────────────────────────────

COMMANDS = {
    "ctx": "show a quest's mission and watches",
    "trail": "show a quest's recent timeline",
    "ask": "start Codex, Claude Code or Cursor on a quest",
    "queue": "queue an instruction for a quest's next triage tick",
    "create": "create a quest interactively with Codex, Claude Code or Cursor",
    "terminal": "open a quest's terminal session, or one for creating a quest",
}

USAGE = {
    "ctx": "sidequestor [--workspace PATH] ctx QUEST_ID",
    "trail": "sidequestor [--workspace PATH] trail QUEST_ID [--limit N|--all]",
    "ask": "sidequestor [--workspace PATH] ask QUEST_ID [--codex|--claude|--cursor] [PROMPT...]",
    "queue": "sidequestor [--workspace PATH] queue QUEST_ID INSTRUCTION...",
    "create": "sidequestor [--workspace PATH] create [--codex|--claude|--cursor] [MISSION...]",
    "terminal": "sidequestor [--workspace PATH] terminal QUEST_ID|--new-quest [--welcome|--json] [--app APP]",
}


def _agent_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    for agent in AGENT_ORDER:
        group.add_argument(f"--{agent}", dest="agent", action="store_const", const=agent)


def _report_open(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result))
    elif result["status"] == "throttled":
        print(f"{result['session']} is already opening.")
    else:
        verb = "Reattaching to" if result["status"] == "reattach_requested" else "Opening"
        print(f"{verb} {result['session']} in {result['app']} ({result['multiplexer']}).")


def run_command(workspace: Workspace, command: str, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"sidequestor {command}")
    if command == "terminal":
        parser.add_argument("quest_id", nargs="?")
        parser.add_argument("--new-quest", action="store_true")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--welcome", action="store_true")
        mode.add_argument("--json", action="store_true")
        parser.add_argument("--app")
    elif command != "create":
        parser.add_argument("quest_id")
    if command == "trail":
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--limit", type=int, default=15)
        group.add_argument("--all", action="store_true")
    elif command in {"ask", "create"}:
        _agent_flags(parser)
        parser.add_argument("prompt", nargs="*")
    elif command == "queue":
        parser.add_argument("instruction", nargs="*")
    if command in {"ask", "create"}:
        # Preserve normal parsing, including literal flags after --. On Python
        # 3.11, interleaved agent flags can leave valid prompt words unconsumed.
        values, remaining = parser.parse_known_args(args)
        if remaining:
            values = parser.parse_intermixed_args(args)
    else:
        values = parser.parse_args(args)
    if command == "terminal" and bool(values.quest_id) == values.new_quest:
        parser.error("give either QUEST_ID or --new-quest")
    as_json = command == "terminal" and values.json
    try:
        if command == "create":
            return run_create(workspace, " ".join(values.prompt), values.agent)
        if command == "terminal" and values.new_quest:
            if values.welcome:
                print(render_new_quest_welcome(workspace))
            else:
                _report_open(open_terminal(workspace, new_quest_target(workspace), values.app), as_json)
            return 0
        quest = load_quest(workspace, values.quest_id)
        if command == "ctx":
            print(render_context(quest))
        elif command == "trail":
            print(render_trail(quest, None if values.all else max(1, values.limit)))
        elif command == "ask":
            return run_ask(workspace, quest, " ".join(values.prompt), values.agent)
        elif command == "queue":
            result = queue_instruction(workspace, quest, " ".join(values.instruction))
            print(f"Queued ({result.get('approval_id')}). The agent picks it up on the next triage tick.")
        elif values.welcome:
            print(render_welcome(workspace, quest))
        else:
            _report_open(open_terminal(workspace, quest_target(quest), values.app), as_json)
    except QuestTerminalError as error:
        if as_json:
            print(json.dumps({"ok": False, "error": str(error)}))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
