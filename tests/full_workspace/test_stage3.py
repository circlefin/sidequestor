from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar


PACKAGE = Path(__file__).resolve().parents[2]
YAAS = Path(os.environ.get("SIDEQUESTOR_BIN", PACKAGE / ".venv" / "bin" / "sq"))


def invoke(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run([str(YAAS), *args], text=True, capture_output=True, env=env)


class Stage3FullWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="yaas-stage3-")
        root = Path(self.temp.name)
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.home.mkdir()
        result = invoke("init", str(self.workspace), "--name", "stage3", home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.instance_id = json.loads(
            (self.workspace / ".yaas" / "instance.json").read_text()
        )["instance_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = invoke("--workspace", str(self.workspace), *args, home=self.home)
        if check:
            self.assertEqual(result.returncode, 0, f"{args}: {result.stderr}\n{result.stdout}")
        return result

    def _quest(self, quest_id: str = "quest-stage3") -> Path:
        quest = self.workspace / "state" / "quests" / "active" / quest_id
        quest.mkdir(parents=True, exist_ok=True)
        (quest / "watch.json").write_text('{"watches": []}\n')
        (quest / "timeline.ndjson").touch()
        return quest

    def _due_quest(self, quest_id: str = "quest-stage3-dispatch") -> Path:
        quest = self._quest(quest_id)
        (quest / "meta.json").write_text(json.dumps({
            "id": quest_id,
            "title": "Stage 3 fake dispatch",
            "status": "active",
            "priority": "normal",
            "allow_send": False,
        }) + "\n")
        (quest / "watch.json").write_text(json.dumps({"watches": [{
            "type": "schedule",
            "next_fire_ts": "1",
            "last_checked_ts": "0",
            "reason": "Stage 3 fake dispatch",
            "watch_id": "watch-stage3-dispatch",
        }]}) + "\n")
        return quest

    def test_every_public_command_runs_against_the_shadow(self) -> None:
        self.assertIn(str(self.workspace), self.invoke("instances", "list").stdout)
        self.invoke("instances", "register", str(self.workspace))
        rekeyed = self.invoke("instances", "rekey", str(self.workspace))
        self.instance_id = json.loads(
            (self.workspace / ".yaas" / "instance.json").read_text()
        )["instance_id"]
        self.assertIn(self.instance_id, rekeyed.stdout)
        self.invoke("instances", "doctor")
        self.invoke("doctor")
        self.invoke("migrate")
        self.invoke("sync-resources")
        self.invoke("tick", "--dry-run")
        isolated_tick = self.invoke("tick", "--isolated")
        self.assertIn("Triage starting.", isolated_tick.stderr)
        self.invoke("loop", "--max-ticks", "2")
        self.invoke("setup", "--render-only")
        self.invoke("setup", "install")
        self.invoke("setup", "status")

        quest = self._quest()
        watch_id = self.invoke(
            "watch", "quest-stage3", json.dumps({
                "type": "schedule", "cron": "* * * * *", "tz": "UTC",
                "reason": "Stage 3 command surface",
            })
        ).stdout.strip()
        self.assertTrue(watch_id.startswith("watch-"))
        logged = self.invoke("log", json.dumps({
            "quest_id": "quest-stage3", "event": "note", "message_text": "shadow log",
        }))
        self.assertEqual(json.loads(logged.stdout)["event"], "note")
        self.invoke("approval", "ensure-inbox")
        self.invoke("approval", "write", json.dumps({
            "source": "stage3-test",
            "target": {"channel_id": "D0AAAA0", "thread_ts": "1.000001"},
            "message_text": "shadow draft",
        }))

        dispatch = self.workspace / "state" / "triage" / "dispatch-stage3.json"
        dispatch.parent.mkdir(parents=True, exist_ok=True)
        dispatch.write_text(json.dumps({
            "run_id": "run-stage3", "target": "quest-stage3", "kind": "quest",
            "items": [{"item_id": "item-stage3", "type": "schedule", "status": "pending"}],
        }))
        self.invoke(
            "ack", "open", "run-stage3", "quest-stage3", "quest",
            '[{"item_id":"item-stage3","type":"schedule"}]',
        )
        self.invoke("ack", "ack", "run-stage3", "item-stage3", "handled", "safe")
        self.assertIn("item-stage3", self.invoke("ack", "acked", "run-stage3").stdout)

        self.invoke("slack-send", "--channel-id", "D0AAAA0", "--message", "shadow", "--draft")
        self.invoke("react", "advance", "C0AAAA0", "1.000001", "loading")
        self.invoke("mcp-call", "fake_tool", "{}")
        self.invoke("jira-call", "GET", "/rest/api/2/issue/NOPE")
        self.invoke("setup", "uninstall")
        self.invoke("setup", "install")
        self.assertTrue(quest.exists())

    def test_install_reinstall_and_uninstall_are_uuid_scoped(self) -> None:
        first = self.invoke("setup", "install")
        self.assertIn(self.instance_id, first.stdout)
        installed = self.workspace / ".yaas" / "launchd" / "installed"
        manifest = json.loads((installed / "manifest.json").read_text())
        self.assertEqual(manifest["backend"], "workspace-shadow")
        self.assertEqual(manifest["instance_id"], self.instance_id)
        self.assertEqual(set(manifest["jobs"]), {"triage", "dashboard"})
        for job in manifest["jobs"].values():
            plist = (installed / job["plist"]).read_text()
            self.assertIn("-m</string>", plist)
            self.assertIn("sidequestor</string>", plist)
            self.assertIn(str(self.workspace), plist)
            self.assertIn(self.instance_id, plist)

        self.invoke("setup", "install")
        self.assertEqual(len(list(installed.glob("*.plist"))), 2)
        self.assertIn("shadow jobs: installed", self.invoke("setup", "status").stdout)
        self.invoke("setup", "uninstall")
        self.assertFalse(installed.exists())
        self.assertIn("not installed", self.invoke("setup", "status").stdout)

    def test_shadow_loop_and_dashboard_run_from_package(self) -> None:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["YAAS_LOOP_INTERVAL"] = "0.01"
        bounded = subprocess.run(
            [str(YAAS), "--workspace", str(self.workspace), "loop", "--isolated", "--max-ticks", "2"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(bounded.returncode, 0, bounded.stderr)
        self.assertEqual(bounded.stderr.count("Triage starting."), 2, bounded.stderr)
        self.assertTrue((self.workspace / "state" / "triage" / "last-run.json").exists())

        process = subprocess.Popen(
            [str(YAAS), "--workspace", str(self.workspace), "dashboard", "serve", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
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
            self.assertEqual(self.invoke("dashboard", "url").stdout.strip(), url)
        finally:
            process.terminate()
            process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def test_isolated_tick_dispatches_to_fake_worker(self) -> None:
        quest = self._due_quest()
        result = self.invoke("tick", "--isolated", "--fake-worker")
        self.assertIn("DISPATCH DONE", result.stderr)

        called = json.loads((self.workspace / "state" / "fake-worker-called.json").read_text())
        self.assertEqual(called["target"], "quest-stage3-dispatch")
        self.assertEqual(len(called["items"]), 1)

        manifests = sorted((self.workspace / "state" / "triage").glob("dispatch-run-*.json"))
        self.assertTrue(manifests)
        manifest = json.loads(manifests[-1].read_text())
        self.assertEqual(called["items"], [manifest["items"][0]["item_id"]])
        self.assertEqual(manifest["items"][0]["status"], "handled")
        self.assertNotEqual(manifest["items"][0]["acked_utc"], None)
        self.assertTrue(quest.exists())

    def test_long_running_shadow_loop_advances_state_and_stops(self) -> None:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["YAAS_LOOP_INTERVAL"] = "0.05"
        process = subprocess.Popen(
            [str(YAAS), "--workspace", str(self.workspace), "loop", "--isolated"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        last_run = self.workspace / "state" / "triage" / "last-run.json"
        completed = None
        try:
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    candidate = json.loads(last_run.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    candidate = {}
                if candidate.get("last_triage_completed_utc"):
                    completed = candidate
                    break
                time.sleep(0.05)
            self.assertIsNotNone(completed, "isolated loop never completed a real tick")
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        self.assertIsNotNone(process.returncode)

    def test_two_shadow_workspaces_do_not_share_job_identity(self) -> None:
        other = self.workspace.parent / "other-workspace"
        result = invoke("init", str(other), "--name", "other", home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        other_id = json.loads((other / ".yaas" / "instance.json").read_text())["instance_id"]
        self.invoke("setup", "install")
        other_result = invoke("--workspace", str(other), "setup", "install", home=self.home)
        self.assertEqual(other_result.returncode, 0, other_result.stderr)
        self.assertNotEqual(self.instance_id, other_id)
        first_manifest = json.loads((self.workspace / ".yaas" / "launchd" / "installed" / "manifest.json").read_text())
        second_manifest = json.loads((other / ".yaas" / "launchd" / "installed" / "manifest.json").read_text())
        self.assertNotEqual(first_manifest["jobs"]["triage"]["label"], second_manifest["jobs"]["triage"]["label"])
        self.invoke("setup", "uninstall")
        self.assertTrue((other / ".yaas" / "launchd" / "installed" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
