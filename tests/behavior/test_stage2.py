from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from urllib.request import build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar


PACKAGE = Path(__file__).resolve().parents[2]
YAAS = Path(os.environ.get("SIDEQUESTOR_BIN", PACKAGE / ".venv" / "bin" / "sq"))


def run(*args: str, home: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        [str(YAAS), *args], text=True, capture_output=True, env=env, check=check,
    )


class Stage2BehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home_dir = tempfile.TemporaryDirectory(prefix="yaas-stage2-home-")
        self.workspace_dir = tempfile.TemporaryDirectory(prefix="yaas-stage2-workspace-")
        self.home = Path(self.home_dir.name)
        self.workspace = Path(self.workspace_dir.name) / "workspace"
        result = run("init", str(self.workspace), "--name", "behavior", home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.instance_id = json.loads(
            (self.workspace / ".yaas" / "instance.json").read_text()
        )["instance_id"]

    def tearDown(self) -> None:
        self.workspace_dir.cleanup()
        self.home_dir.cleanup()

    def invoke(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = run("--workspace", str(self.workspace), *args, home=self.home)
        if check:
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr}\n{result.stdout}")
        return result

    def _quest(self, quest_id: str = "quest-stage2") -> Path:
        quest = self.workspace / "state" / "quests" / "active" / quest_id
        quest.mkdir(parents=True, exist_ok=True)
        (quest / "watch.json").write_text('{"watches": []}\n')
        (quest / "timeline.ndjson").touch()
        return quest

    def test_local_ledger_commands_use_the_workspace_projection(self) -> None:
        quest = self._quest()
        watch = json.dumps({
            "type": "schedule",
            "cron": "* * * * *",
            "tz": "UTC",
            "reason": "Stage 2 behavior test",
        })
        result = self.invoke("watch", "quest-stage2", watch)
        watch_id = result.stdout.strip()
        self.assertTrue(watch_id.startswith("watch-"))

        result = self.invoke("log", json.dumps({
            "quest_id": "quest-stage2",
            "event": "note",
            "message_text": "written through package shell",
        }))
        self.assertEqual(json.loads(result.stdout)["event"], "note")

        dispatch = self.workspace / "state" / "triage" / "dispatch-stage2.json"
        dispatch.parent.mkdir(parents=True, exist_ok=True)
        dispatch.write_text(json.dumps({
            "run_id": "run-stage2",
            "target": "quest-stage2",
            "kind": "quest",
            "items": [{"item_id": "item-stage2", "type": "schedule", "status": "pending"}],
        }))
        self.invoke(
            "ack", "open", "run-stage2", "quest-stage2", "quest",
            '[{"item_id":"item-stage2","type":"schedule"}]',
        )
        self.invoke("ack", "ack", "run-stage2", "item-stage2", "handled", "safe")
        self.assertIn("item-stage2", self.invoke("ack", "acked", "run-stage2").stdout)

        self.invoke("approval", "ensure-inbox")
        approval = self.invoke("approval", "write", json.dumps({
            "source": "stage2-test",
            "target": {"channel_id": "D0AAAA0", "thread_ts": "1.000001"},
            "message_text": "isolated draft",
        })).stdout.strip()
        self.assertTrue(approval.startswith("appr-"))
        self.assertTrue((quest / "timeline.ndjson").exists())

    def test_external_surfaces_are_isolated_and_recorded(self) -> None:
        self.invoke("slack-send", "--channel-id", "D0AAAA0", "--message", "hello", "--draft")
        self.invoke("react", "advance", "C0AAAA0", "1.000001", "loading")
        self.invoke("mcp-call", "fake_tool", "{}")
        self.invoke("jira-call", "GET", "/rest/api/2/issue/NOPE")

        events = self.workspace / ".yaas" / "stage2-adapters" / "events.jsonl"
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual([row["surface"] for row in rows], [
            "slack-send", "react", "mcp-call", "jira-call",
        ])
        self.assertFalse(rows[0]["payload"]["delivered"])
        self.assertEqual(
            json.loads((self.workspace / ".yaas" / "stage2-adapters" / "reactions.json").read_text())[
                "C0AAAA0/1.000001"
            ],
            "loading",
        )

    def test_workspace_management_and_dry_tick(self) -> None:
        self.assertIn(str(self.workspace), self.invoke("instances", "list").stdout)
        self.invoke("instances", "register", str(self.workspace))
        self.invoke("instances", "rekey", str(self.workspace))
        self.invoke("instances", "doctor")
        self.invoke("doctor")
        self.invoke("migrate")
        self.invoke("sync-resources")
        self.invoke("tick", "--dry-run")

    def test_resources_are_loaded_from_package_data(self) -> None:
        skill = self.workspace / ".yaas" / "engine" / "current" / "skills" / "yaas-ops" / "SKILL.md"
        self.assertTrue(skill.exists())
        self.assertIn("SIDEQUESTOR", skill.read_text())
        operating = self.workspace / ".yaas" / "engine" / "current" / "OPERATING.md"
        operating_text = operating.read_text()
        self.assertIn("Sidequestor Worker Operating Instructions", operating_text)
        self.assertIn("absolute packaged runtime root", operating_text)
        self.assertIn("A `reviewed` approval records the user's authorization", operating_text)
        self.assertIn("do not apply\n  `allow_send` again as a permanent veto", operating_text)
        self.assertNotIn("$SIDEQUESTOR_RUNTIME_ROOT", operating_text)
        self.assertNotIn("Never send unless the quest allows it", operating_text)
        self.assertIn("SIDEQUESTOR_CLAUDE_PERMISSION_MODE=acceptEdits", (self.workspace / ".env.example").read_text())
        self.assertIn("yaas_v2_auto_pull", (self.workspace / "settings.json.example").read_text())
        watermark = self.workspace / "state" / "triage" / "reaction-watermark.json"
        self.assertEqual(
            json.loads(watermark.read_text())["initialized_at"],
            json.loads((self.workspace / ".yaas" / "instance.json").read_text())["created_at"],
        )
        self.assertFalse((PACKAGE / ".compat" / self.instance_id).exists())

    def test_reaction_sweep_starts_at_workspace_initialization(self) -> None:
        marker = json.loads((self.workspace / ".yaas" / "instance.json").read_text())
        initialized = datetime.fromisoformat(marker["created_at"].replace("Z", "+00:00")).timestamp()
        old_ts = f"{int(initialized) - 10}.000000"
        new_ts = f"{int(initialized) + 10}.000000"
        adapter = self.workspace / "fake-reaction-search.py"
        adapter.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({{'results': '### Result 1 of 2\\nMessage_ts: {old_ts}\\n### Result 2 of 2\\nMessage_ts: {new_ts}', 'pagination_info': ''}}))\n"
        )
        adapter.chmod(0o755)
        pending = self.workspace / "state" / "triage" / "pending_reactions.json"
        checker = PACKAGE / "src" / "sidequestor" / "runtime" / "yaas-triage" / "checkers" / "reactions.py"
        result = subprocess.run(
            [sys.executable, str(checker), str(adapter), "1970-01-01", str(self.workspace), str(pending)],
            text=True, capture_output=True, env={**os.environ, "HOME": str(self.home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        queued = json.loads(pending.read_text())
        self.assertTrue(queued)
        self.assertTrue(all(new_ts in timestamps for timestamps in queued.values()))
        self.assertTrue(all(old_ts not in timestamps for timestamps in queued.values()))

    def test_bounded_loop_rendered_jobs_and_dashboard(self) -> None:
        self.invoke("loop", "--max-ticks", "2")
        rendered = self.invoke("setup", "--render-only").stdout
        self.assertIn("rendered launchd jobs", rendered)
        plists = list((self.workspace / ".yaas" / "rendered-launchd").glob("*.plist"))
        self.assertEqual(len(plists), 2)
        self.assertFalse(any("heartbeat" in path.name for path in plists))

        process = subprocess.Popen(
            [str(YAAS), "--workspace", str(self.workspace), "dashboard", "serve", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": str(self.home)},
        )
        try:
            url_file = self.workspace / "state" / "dashboard-url.txt"
            deadline = time.time() + 3
            while time.time() < deadline and not url_file.exists() and process.poll() is None:
                time.sleep(0.05)
            if process.poll() is not None:
                error = process.stderr.read() if process.stderr else ""
                if "PermissionError" in error and "bind" in error:
                    self.skipTest("sandbox does not permit localhost socket binding")
                self.fail(error)
            url = url_file.read_text().strip()
            self.assertTrue(url)
            browser = build_opener(HTTPCookieProcessor(CookieJar()))
            with browser.open(url + "/", timeout=3) as response:
                html = response.read().decode()
            self.assertIn("Sidequestor", html)
            with browser.open(url + "/api/dashboard", timeout=3) as response:
                payload = json.loads(response.read())
            self.assertIn("quests", payload)
            self.assertEqual(payload["workspace"]["path"], str(self.workspace.resolve()))
            self.assertEqual(payload["workspace"]["display_name"], "behavior")
            self.assertEqual(self.invoke("dashboard", "url").stdout.strip(), url)
        finally:
            process.terminate()
            process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def test_shadow_setup_lifecycle_is_workspace_scoped(self) -> None:
        installed = self.invoke("setup", "install")
        self.assertIn("installed shadow jobs", installed.stdout)
        self.assertIn(json.loads((self.workspace / ".yaas" / "instance.json").read_text())["instance_id"], installed.stdout)
        status = self.invoke("setup", "status")
        self.assertIn("shadow jobs: installed", status.stdout)
        self.assertEqual(len(list((self.workspace / ".yaas" / "launchd" / "installed").glob("*.plist"))), 2)
        self.invoke("setup", "install")
        self.assertEqual(len(list((self.workspace / ".yaas" / "launchd" / "installed").glob("*.plist"))), 2)
        self.invoke("setup", "uninstall")
        self.assertIn("not installed", self.invoke("setup", "status").stdout)


if __name__ == "__main__":
    unittest.main()
