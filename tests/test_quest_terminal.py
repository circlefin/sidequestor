from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor import quest_terminal as qt
from sidequestor.cli import main
from sidequestor.workspace import init_workspace


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "src" / "sidequestor" / "runtime"
TRIAGE_ROOT = RUNTIME_ROOT / "yaas-triage"
DASHBOARD_HTML = RUNTIME_ROOT / "dashboard.html"


class FakeTTY(StringIO):
    def isatty(self) -> bool:
        return True


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class QuestTerminalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-quest-terminal-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"YAAS_CONFIG_HOME": str(root / "config")})
        env.start()
        self.addCleanup(env.stop)
        self.workspace = init_workspace(root / "ws")
        self.quest_dir = self.workspace.state / "quests" / "active" / "quest-demo"
        self.quest_dir.mkdir(parents=True)
        (self.quest_dir / "meta.json").write_text(json.dumps({
            "title": "Demo \x1b]0;spoofed\x07quest", "status": "active", "allow_send": False}))
        (self.quest_dir / "context.md").write_text("# Demo\n\nReply to partners within a day.\n")
        (self.quest_dir / "watch.json").write_text(json.dumps({
            "watches": [{"type": "slack_dm", "user_name": "partner-lead", "reason": "DMs",
                         "last_error": "429 rate limited"}],
            "channels": [{"channel_id": "C123", "reason": "whole channel"}],
        }))
        (self.quest_dir / "timeline.ndjson").write_text("\n".join([
            json.dumps({"ts": "2026-10-12T09:00:00Z", "event": "info_received", "note": "Docs?"}),
            "not json",
            json.dumps({"ts": 1791799200000, "event": "draft_posted", "note": "red \x1b[31mtext"}),
            json.dumps({"ts": "2026-10-12T11:00:00Z", "event": "note", "note": "newest"}),
        ]) + "\n")
        self.quest = qt.load_quest(self.workspace, "quest-demo")


class QuestLoadingAndRenderingTests(QuestTerminalTestCase):
    def test_rejects_invalid_and_unknown_quest_ids(self) -> None:
        for bad in ("", "../etc", "a/b", ".hidden", "-flag", "x" * 161, "quest-demo\n"):
            with self.assertRaises(qt.QuestTerminalError):
                qt.load_quest(self.workspace, bad)
        with self.assertRaises(qt.QuestTerminalError):
            qt.load_quest(self.workspace, "quest-missing")

    def test_non_object_meta_is_ignored(self) -> None:
        (self.quest_dir / "meta.json").write_text("[1, 2]")
        quest = qt.load_quest(self.workspace, "quest-demo")
        self.assertEqual(quest.title, "quest-demo")

    def test_title_strips_terminal_escapes(self) -> None:
        self.assertEqual(self.quest.title, "Demo quest")
        self.assertNotIn("\x1b", qt.clean("a\x1b[2Jb\x1b]52;c;ZXZpbA==\x07c\x9bd"))

    def test_context_lists_mission_and_both_watch_shapes(self) -> None:
        text = qt.render_context(self.quest)
        self.assertIn("Demo quest · active · normal priority · draft only", text)
        self.assertIn("Reply to partners within a day.", text)
        self.assertIn("slack_dm  partner-lead  — DMs", text)
        self.assertIn("last error: 429 rate limited", text)
        self.assertIn("slack_channel  C123  — whole channel", text)

    def test_trail_is_newest_first_bounded_and_clean(self) -> None:
        now = qt._timestamp("2026-10-12T12:00:00Z")
        lines = qt.render_trail(self.quest, limit=2, now=now).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("1h ago  note", lines[1])
        self.assertIn("newest", lines[1])
        self.assertIn("draft_posted", lines[2])  # epoch milliseconds are understood
        self.assertNotIn("\x1b", "\n".join(lines))

    def test_trail_reads_only_the_tail_of_a_large_timeline(self) -> None:
        line = json.dumps({"ts": "2026-10-12T09:00:00Z", "event": "note", "note": "x" * 200})
        (self.quest_dir / "timeline.ndjson").write_text((line + "\n") * 5000)
        with patch.object(qt, "TRAIL_TAIL_BYTES", 4096):
            entries = qt._timeline(self.quest)
        self.assertLess(len(entries), 30)
        self.assertTrue(all(entry["event"] == "note" for entry in entries))


class AgentTests(QuestTerminalTestCase):
    def which(self, installed: set[str]):
        return lambda name, path=None: f"/bin/{name}" if name in installed else None

    def test_configured_agent_is_used(self) -> None:
        self.workspace.env_file.write_text("SIDEQUESTOR_AGENT=claude\n")
        with patch("sidequestor.job_path.shutil.which", self.which({"claude", "codex"})):
            self.assertEqual(qt.resolve_agent(self.workspace, "")[:2], ("claude", "/bin/claude"))

    def test_missing_configured_agent_falls_back_with_a_notice(self) -> None:
        with patch("sidequestor.job_path.shutil.which", self.which({"claude"})):
            agent, binary, notice = qt.resolve_agent(self.workspace, "")
        self.assertEqual((agent, binary), ("claude", "/bin/claude"))
        self.assertIn("Codex is not installed", notice)

    def test_explicit_agent_never_falls_back(self) -> None:
        with patch("sidequestor.job_path.shutil.which", self.which({"codex"})):
            with self.assertRaisesRegex(qt.QuestTerminalError, "Claude Code is not installed"):
                qt.resolve_agent(self.workspace, "", "claude")

    def test_cursor_resolves_to_cursor_agent(self) -> None:
        with patch("sidequestor.job_path.shutil.which", self.which({"cursor-agent"})):
            self.assertEqual(qt.resolve_agent(self.workspace, "", "cursor")[1], "/bin/cursor-agent")

    def test_ask_execs_the_agent_with_a_seeded_prompt_from_the_workspace(self) -> None:
        calls = []
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with patch("sidequestor.job_path.shutil.which", self.which({"codex"})):
            qt.run_ask(self.workspace, self.quest, "draft a reply",
                       execve=lambda binary, argv, env: calls.append((binary, argv, env, os.getcwd())))
        binary, argv, env, workdir = calls[0]
        self.assertEqual(binary, "/bin/codex")
        self.assertEqual(len(argv), 2)
        self.assertIn("quest-demo", argv[1])
        self.assertIn("Work only on this quest", argv[1])
        self.assertIn("My request: draft a reply", argv[1])
        self.assertEqual(env["SQ_QUEST_ID"], "quest-demo")
        self.assertEqual(Path(workdir).resolve(), self.workspace.root.resolve())


class QueueTests(QuestTerminalTestCase):
    def test_queue_requires_an_interactive_confirmation(self) -> None:
        with self.assertRaisesRegex(qt.QuestTerminalError, "interactive terminal"):
            qt.queue_instruction(self.workspace, self.quest, "do it", stdin=StringIO())
        with self.assertRaisesRegex(qt.QuestTerminalError, "not queued"), redirect_stdout(StringIO()):
            qt.queue_instruction(self.workspace, self.quest, "do it", stdin=FakeTTY(), ask=lambda _: "n")

    def test_queue_enforces_the_composer_limit(self) -> None:
        with self.assertRaisesRegex(qt.QuestTerminalError, "too long"):
            qt.queue_instruction(self.workspace, self.quest, "x" * 4001, stdin=FakeTTY())
        with self.assertRaisesRegex(qt.QuestTerminalError, "required"):
            qt.queue_instruction(self.workspace, self.quest, "  ", stdin=FakeTTY())

    def test_queue_uses_the_dashboard_enqueue_helper(self) -> None:
        seen = []

        def run(cmd, **kwargs):
            seen.append((cmd, kwargs))
            return completed(cmd, stdout='{"queued": true, "approval_id": "appr-1"}\n')

        with redirect_stdout(StringIO()):
            result = qt.queue_instruction(self.workspace, self.quest, " check again ",
                                          stdin=FakeTTY(), ask=lambda _: "y", run=run)
        self.assertEqual(result["approval_id"], "appr-1")
        cmd, kwargs = seen[0]
        self.assertTrue(cmd[1].endswith("yaas-triage/ledger/approval-helper.py"))
        self.assertEqual(cmd[2], "enqueue-instruction")
        payload = json.loads(cmd[3])
        self.assertEqual(payload["quest_id"], "quest-demo")
        self.assertEqual(payload["instruction"], "check again")
        self.assertEqual(kwargs["cwd"], str(self.workspace.root))

    def test_queue_reports_helper_failure(self) -> None:
        run = lambda cmd, **kwargs: completed(cmd, returncode=1, stderr="locked")
        with self.assertRaisesRegex(qt.QuestTerminalError, "could not be queued: locked"), \
                redirect_stdout(StringIO()):
            qt.queue_instruction(self.workspace, self.quest, "x", stdin=FakeTTY(),
                                 ask=lambda _: "yes", run=run)


class SessionTests(QuestTerminalTestCase):
    def test_session_names_are_workspace_qualified(self) -> None:
        name = qt.session_name(self.workspace, qt.quest_target(self.quest))
        self.assertEqual(name, f"sq-{self.workspace.instance_id}-quest-demo")
        dotted = qt.Quest("quest.v2", self.quest_dir, {})
        self.assertNotIn(".", qt.session_name(self.workspace, qt.quest_target(dotted)))
        underscored = qt.Quest("quest_v2", self.quest_dir, {})
        self.assertNotEqual(qt.session_name(self.workspace, qt.quest_target(dotted)),
                            qt.session_name(self.workspace, qt.quest_target(underscored)))
        other = qt.Workspace(self.workspace.root, self.workspace.instance_id[:8] + "different", "other")
        self.assertNotEqual(name, qt.session_name(other, qt.quest_target(self.quest)))

    def test_screen_session_lookup_is_exact(self) -> None:
        listing = ("There are screens on:\n\t123.sq-abc-quest-demo\t(Detached)\n"
                   "\t456.sq-abc-quest-demo-2\t(Detached)\n")
        run = lambda cmd, **kwargs: completed(cmd, stdout=listing)
        self.assertEqual(qt._screen_sessions("/usr/bin/screen", "sq-abc-quest-demo", run),
                         ["123.sq-abc-quest-demo"])

    def test_launchers_for_each_multiplexer(self) -> None:
        directory = self.workspace.state / "terminals" / "quest-demo"
        tmux = qt.launcher(qt.quest_target(self.quest), directory, "sq-x-quest-demo", "tmux", "/opt/homebrew/bin/tmux", None)
        self.assertIn("exec /opt/homebrew/bin/tmux new-session -A -s sq-x-quest-demo", tmux)
        joined = qt.launcher(qt.quest_target(self.quest), directory, "sq-x-quest-demo", "screen", "/usr/bin/screen",
                             "123.sq-x-quest-demo")
        self.assertIn("exec /usr/bin/screen -x 123.sq-x-quest-demo", joined)
        created = qt.launcher(qt.quest_target(self.quest), directory, "sq-x-quest-demo", "screen", "/usr/bin/screen", None)
        self.assertIn("exec /usr/bin/screen -S sq-x-quest-demo env ZDOTDIR=", created)
        plain = qt.launcher(qt.quest_target(self.quest), directory, "sq-x-quest-demo", "none", None, None)
        self.assertIn("exec env ZDOTDIR=", plain)
        for script in (tmux, joined, created, plain):
            self.assertTrue(script.startswith("#!/bin/zsh\n"))
            self.assertIn("'Sidequestor · Demo quest'", script)
            self.assertNotIn("\x1b", script)

    def test_generated_shell_is_quoted_and_contains_no_quest_text(self) -> None:
        text = qt.zshenv(self.workspace, qt.quest_target(self.quest), prefill=True)
        self.assertNotIn("Reply to partners", text)
        self.assertNotIn("Demo", text)
        self.assertIn("help() { _sq terminal quest-demo --welcome; }", text)
        self.assertIn("print -z 'ask \"'", text)
        self.assertNotIn("print -z", qt.zshenv(self.workspace, qt.quest_target(self.quest), prefill=False))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_generated_files_are_valid_zsh(self) -> None:
        directory = self.workspace.state / "terminals"
        directory.mkdir()
        files = {
            "zshenv": qt.zshenv(self.workspace, qt.quest_target(self.quest), prefill=True),
            "launcher": qt.launcher(qt.quest_target(self.quest), directory, "sq-x-quest-demo", "tmux", "/usr/bin/tmux", None),
        }
        for name, text in files.items():
            path = directory / name
            path.write_text(text)
            result = subprocess.run(["/bin/zsh", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_helpers_survive_user_zshenv_aliases_and_custom_zdotdir(self) -> None:
        directory = self.workspace.state / "terminals"
        directory.mkdir()
        user_config = Path(self.temp.name) / "user-shell"
        user_config.mkdir()
        (user_config / ".zshenv").write_text("alias help=false ctx=false\nexport SQ_STARTUP_LOADED=yes\n")
        startup = directory / ".zshenv"
        startup.write_text(qt.zshenv(self.workspace, qt.quest_target(self.quest), prefill=False))
        result = subprocess.run(
            ["/bin/zsh", "-f", "-c",
             'source "$1"; _sq_session_setup; eval ctx; '
             'print -r -- "startup=$SQ_STARTUP_LOADED config=$ZDOTDIR"', "test", str(startup)],
            capture_output=True, text=True,
            env=dict(os.environ, SQ_USER_ZDOTDIR=str(user_config)),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reply to partners within a day.", result.stdout)
        self.assertIn(f"startup=yes config={user_config}", result.stdout)
        self.assertNotIn("parse error", result.stderr)

    def open_terminal(self, *, running=False, open_code=0, now=None, target=None):
        calls = []
        self.target = target or qt.quest_target(self.quest)

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "open":
                return completed(cmd, returncode=open_code, stderr="no such app" if open_code else "")
            if cmd[-1] == "-ls":
                return completed(cmd, stdout=f"\t99.{qt.session_name(self.workspace, self.target)}\t(Attached)\n"
                                 if running else "No Sockets found.\n")
            return completed(cmd, returncode=0 if running else 1)

        with patch.object(qt, "find_multiplexer", return_value=("screen", "/usr/bin/screen")), \
                patch("sidequestor.job_path.shutil.which", return_value=None):
            kwargs = {"run": run} if now is None else {"run": run, "now": now}
            result = qt.open_terminal(self.workspace, self.target, **kwargs)
        return result, calls

    def test_open_terminal_writes_private_files_and_requests_launch(self) -> None:
        result, calls = self.open_terminal()
        self.assertEqual(result["status"], "launch_requested")
        directory = self.workspace.state / "terminals" / "quest-demo"
        command = directory / "open.command"
        self.assertIn(["open", "-a", "Terminal", str(command)], calls)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(command.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / ".zshenv").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("print -z", (directory / ".zshenv").read_text())  # no agent installed

    def test_second_click_while_launching_is_throttled(self) -> None:
        self.open_terminal()
        result, calls = self.open_terminal()
        self.assertEqual(result["status"], "throttled")
        self.assertFalse(any(cmd[0] == "open" for cmd in calls))

    def test_running_session_is_joined_not_duplicated(self) -> None:
        result, _ = self.open_terminal(running=True)
        self.assertEqual(result["status"], "reattach_requested")
        launcher = (self.workspace.state / "terminals" / "quest-demo" / "open.command").read_text()
        self.assertIn(f"-x 99.{qt.session_name(self.workspace, qt.quest_target(self.quest))}", launcher)

    def test_open_failure_is_reported(self) -> None:
        with self.assertRaisesRegex(qt.QuestTerminalError, "could not open Terminal: no such app"):
            self.open_terminal(open_code=1)

    def test_symlinked_session_directory_is_refused(self) -> None:
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.workspace.state / "terminals").mkdir()
        (self.workspace.state / "terminals" / "quest-demo").symlink_to(elsewhere)
        with self.assertRaisesRegex(qt.QuestTerminalError, "symlinked"):
            self.open_terminal()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_welcome_names_the_agent_and_the_helpers(self) -> None:
        with patch("sidequestor.job_path.shutil.which", lambda name, path=None: "/bin/codex" if name == "codex" else None):
            text = qt.render_welcome(self.workspace, self.quest, now=qt._timestamp("2026-10-12T12:00:00Z"))
        self.assertIn("╭─ Sidequestor · Demo quest", text)
        self.assertIn("ask Codex about this quest", text)
        for helper in ("ctx", "trail", 'queue "…"', "help"):
            self.assertIn(helper, text)
        self.assertIn("not limited to this quest", text)


    def test_new_quest_session_is_workspace_level_and_cannot_clash_with_a_quest(self) -> None:
        target = qt.new_quest_target(self.workspace)
        self.assertEqual(target.cwd, self.workspace.root)
        name = qt.session_name(self.workspace, target)
        self.assertTrue(name.endswith("-_new-quest"))
        self.assertFalse(qt.QUEST_ID.match(target.key))
        text = qt.zshenv(self.workspace, target, prefill=True)
        self.assertIn('create() { _sq create "$@"; }', text)
        self.assertIn("help() { _sq terminal --new-quest --welcome; }", text)
        self.assertIn("print -z 'create \"'", text)
        self.assertNotIn("ask()", text)
        self.assertNotIn("SQ_QUEST_ID", text)

    def test_new_quest_session_opens_under_its_own_key(self) -> None:
        result, calls = self.open_terminal(target=qt.new_quest_target(self.workspace))
        self.assertEqual((result["status"], result["target"]), ("launch_requested", "_new-quest"))
        command = self.workspace.state / "terminals" / "_new-quest" / "open.command"
        self.assertIn(["open", "-a", "Terminal", str(command)], calls)
        self.assertIn("'Sidequestor · New quest'", command.read_text())

    def test_create_execs_the_agent_on_the_creation_skill(self) -> None:
        calls = []
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with patch("sidequestor.job_path.shutil.which", lambda name, path=None: f"/bin/{name}" if name == "codex" else None):
            qt.run_create(self.workspace, "follow up with legal",
                          execve=lambda binary, argv, env: calls.append((binary, argv, os.getcwd())))
        binary, argv, workdir = calls[0]
        self.assertEqual(binary, "/bin/codex")
        self.assertIn("quest-creation skill", argv[1])
        self.assertIn("sq new-quest", argv[1])
        self.assertIn("The mission: follow up with legal", argv[1])
        self.assertEqual(Path(workdir).resolve(), self.workspace.root.resolve())
        self.assertIn("Start by asking me", qt.creation_prompt(""))

    def test_new_quest_welcome(self) -> None:
        with patch("sidequestor.job_path.shutil.which", lambda name, path=None: f"/bin/{name}" if name in {"codex", "claude"} else None):
            text = qt.render_new_quest_welcome(self.workspace)
        self.assertIn("╭─ Sidequestor · New quest", text)
        self.assertIn("create a quest with Codex", text)
        self.assertIn('create --claude "…"', text)

class CliTests(QuestTerminalTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--workspace", str(self.workspace.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_commands_are_listed_with_help(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            main(["--help"])
        for command in ("ctx", "trail", "ask", "queue", "terminal"):
            self.assertIn(f"  {command} ", out.getvalue())
        with redirect_stdout(out):
            main(["ask", "--help"])
        self.assertIn("ask QUEST_ID [--codex|--claude|--cursor] [PROMPT...]", out.getvalue())

    def test_ctx_and_trail(self) -> None:
        code, out, _ = self.run_cli("ctx", "quest-demo")
        self.assertEqual(code, 0)
        self.assertIn("Mission", out)
        code, out, _ = self.run_cli("trail", "quest-demo", "--limit", "1")
        self.assertEqual(code, 0)
        self.assertIn("newest", out)
        self.assertNotIn("Docs?", out)

    def test_errors_are_messages_not_tracebacks(self) -> None:
        code, _, err = self.run_cli("ctx", "../etc")
        self.assertEqual(code, 1)
        self.assertIn("error: invalid quest id", err)

    def test_terminal_json_reports_errors_as_json(self) -> None:
        code, out, _ = self.run_cli("terminal", "quest-missing", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"ok": False, "error": "no active quest named quest-missing"})

    def test_terminal_needs_exactly_one_target(self) -> None:
        for args in (("terminal",), ("terminal", "quest-demo", "--new-quest")):
            with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
                main(["--workspace", str(self.workspace.root), *args])
        with patch("sidequestor.job_path.shutil.which", return_value=None):
            code, out, _ = self.run_cli("terminal", "--new-quest", "--welcome")
        self.assertEqual(code, 0)
        self.assertIn("New quest", out)

    def test_create_passes_the_mission_and_agent(self) -> None:
        with patch.object(qt, "run_create", return_value=0) as run_create:
            self.run_cli("create", "--claude", "track", "the launch")
        self.assertEqual(run_create.call_args.args[1:], ("track the launch", "claude"))

    def test_ask_flag_after_quest_id_selects_the_agent(self) -> None:
        with patch.object(qt, "run_ask", return_value=0) as run_ask:
            self.run_cli("ask", "quest-demo", "--claude", "draft", "a reply")
        args = run_ask.call_args.args
        self.assertEqual((args[2], args[3]), ("draft a reply", "claude"))


class DashboardTerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-dashboard-terminal-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "state" / "quests" / "active" / "quest-demo").mkdir(parents=True)
        env = patch.dict(os.environ, {"YAAS_WORKSPACE": str(self.workspace),
                                      "YAAS_RUNTIME_ROOT": str(RUNTIME_ROOT)})
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(TRIAGE_ROOT))
        self.addCleanup(lambda: sys.path.remove(str(TRIAGE_ROOT)))
        spec = importlib.util.spec_from_file_location(
            "sidequestor_dashboard_terminal_test", TRIAGE_ROOT / "ops" / "dashboard-server.py")
        self.server = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with patch.object(sys, "argv", ["dashboard-server.py"]):
            spec.loader.exec_module(self.server)

    def handle(self, quest_id: str, run=None):
        handler = self.server.Handler.__new__(self.server.Handler)
        sent = []
        handler._send_json = lambda data, status=200: sent.append((status, data))
        with patch.object(self.server.subprocess, "run", run or (lambda *a, **k: None)):
            handler._handle_open_terminal(quest_id)
        return sent[0]

    def test_rejects_bad_and_unknown_ids_without_running_anything(self) -> None:
        def run(*args, **kwargs):
            raise AssertionError("must not launch")
        self.assertEqual(self.handle("../x", run)[0], 400)
        self.assertEqual(self.handle(".hidden", run)[0], 400)
        self.assertEqual(self.handle("quest-demo\n", run)[0], 400)
        self.assertEqual(self.handle("quest-missing", run)[0], 404)

    def test_runs_sq_terminal_for_the_server_workspace(self) -> None:
        seen = []

        def run(cmd, **kwargs):
            seen.append(cmd)
            return completed(cmd, stdout='{"ok": true, "status": "launch_requested", "app": "Terminal"}\n')

        status, body = self.handle("quest-demo", run)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "launch_requested")
        self.assertEqual(seen[0][1:], ["-m", "sidequestor", "--workspace", str(self.server.REPO_ROOT),
                                       "terminal", "quest-demo", "--json"])

    def test_relays_cli_errors(self) -> None:
        run = lambda cmd, **kwargs: completed(cmd, returncode=1, stdout='{"ok": false, "error": "could not open Terminal"}')
        status, body = self.handle("quest-demo", run)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "could not open Terminal")

    def test_new_quest_terminal_route_runs_sq_terminal_new_quest(self) -> None:
        seen = []
        handler = self.server.Handler.__new__(self.server.Handler)
        sent = []
        handler._send_json = lambda data, status=200: sent.append((status, data))

        def run(cmd, **kwargs):
            seen.append(cmd)
            return completed(cmd, stdout='{"ok": true, "status": "reattach_requested", "app": "Terminal"}')

        with patch.object(self.server.subprocess, "run", run):
            handler._run_terminal_command(["--new-quest"])
        self.assertEqual(sent[0][0], 200)
        self.assertEqual(seen[0][-3:], ["terminal", "--new-quest", "--json"])
        source = (TRIAGE_ROOT / "ops" / "dashboard-server.py").read_text()
        self.assertIn('if path == "/api/quests/terminal":', source)

    def test_create_dialog_links_to_a_terminal_instead_of_a_copied_prompt(self) -> None:
        html = DASHBOARD_HTML.read_text()
        self.assertIn('placeholder="Queue a precise instruction for this quest…"', html)
        self.assertIn('id="open-creation-terminal"', html)
        self.assertIn(">Create interactively in Terminal instead</button>", html)
        self.assertIn("post('/api/quests/terminal')", html)
        for removed in ("questGuideSnippet", "copy-quest-guide", "quest-guide-snippet", "clipboard"):
            self.assertNotIn(removed, html)

    def test_button_is_static_and_posts_to_the_terminal_route(self) -> None:
        html = DASHBOARD_HTML.read_text()
        static_html = html.split("<script>", 1)[0]
        self.assertEqual(html.count('id="open-terminal"'), 1)
        self.assertIn('class="button terminal-launch" id="open-terminal"', static_html)
        footer = static_html[static_html.index('<footer class="focus-footer">'):]
        self.assertLess(footer.index('</form>'), footer.index('id="open-terminal"'))
        self.assertLess(footer.index('id="open-terminal"'), footer.index('</footer>'))
        self.assertIn("/api/quest/${encodeURIComponent(questId)}/terminal", html)
        self.assertIn("if(e.target.closest('#open-terminal')){await openTerminal();return}", html)


if __name__ == "__main__":
    unittest.main()
