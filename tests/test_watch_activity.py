from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
RUNTIME = PACKAGE / "src" / "sidequestor" / "runtime" / "yaas-triage"


def load_module(name: str, relative: str):
    path = RUNTIME / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


housekeep = load_module("sidequestor_test_housekeep", "ledger/housekeep.py")
tick = load_module("sidequestor_test_tick_activity", "tick.py")


class RetirementActivityTest(unittest.TestCase):
    def test_existing_watch_without_optional_fields_keeps_legacy_behavior(self) -> None:
        watch = {"type": "slack_thread", "thread_ts": "100.000000"}
        self.assertTrue(housekeep.retire_thread(watch, 200.0))

    def test_creation_or_new_activity_extends_an_old_thread(self) -> None:
        created = {"type": "slack_thread", "thread_ts": "100", "created_ts": "300"}
        active = {"type": "slack_thread", "thread_ts": "100", "last_activity_ts": "400"}
        self.assertFalse(housekeep.retire_thread(created, 200.0))
        self.assertFalse(housekeep.retire_thread(active, 200.0))

    def test_malformed_activity_is_ignored(self) -> None:
        watch = {
            "type": "slack_thread",
            "thread_ts": "100",
            "created_ts": "not-an-epoch",
            "last_activity_ts": "nan",
        }
        self.assertTrue(housekeep.retire_thread(watch, 200.0))


class CheckerActivityPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-activity-")
        self.quests = Path(self.temp.name)
        self.quest = self.quests / "quest-one"
        self.quest.mkdir()
        self.watch_path = self.quest / "watch.json"
        self.watch_path.write_text(json.dumps({"watches": [{
            "watch_id": "watch-a1",
            "type": "slack_thread",
            "thread_ts": "100.000000",
        }]}) + "\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _triage(self, advance_to: str):
        class Triage:
            pass

        triage = Triage()
        triage.quests_dir = self.quests
        triage.now_ts = time.time()
        triage.dirty_watches = [{
            "quest_id": "quest-one",
            "watch_id": "watch-a1",
            "type": "slack_thread",
            "advance_to": advance_to,
        }]
        triage._read_json = lambda path, default: json.loads(path.read_text())
        triage.log = lambda message: None
        return triage

    def test_dirty_checker_activity_is_monotonic(self) -> None:
        self.assertEqual(tick.record_thread_activity(self._triage("300.1234567")), set())
        first = json.loads(self.watch_path.read_text())["watches"][0]
        self.assertEqual(first["last_activity_ts"], "300.123456")

        self.assertEqual(tick.record_thread_activity(self._triage("200.000000")), set())
        second = json.loads(self.watch_path.read_text())["watches"][0]
        self.assertEqual(second["last_activity_ts"], "300.123456")

    def test_failed_activity_write_holds_housekeeping_for_that_quest(self) -> None:
        self.watch_path.write_text("not json")
        self.assertEqual(tick.record_thread_activity(self._triage("300")), {"quest-one"})


class AdoptionRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-adopt-")
        self.workspace = Path(self.temp.name)
        quest = self.workspace / "state" / "quests" / "active" / "quest-adopt"
        quest.mkdir(parents=True)
        (quest / "watch.json").write_text('{"watches": []}\n')
        (quest / "timeline.ndjson").touch()
        self.watch_path = quest / "watch.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _add(self, payload: dict) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["SIDEQUESTOR_WORKSPACE"] = str(self.workspace)
        env["YAAS_RUNTIME_ROOT"] = str(RUNTIME)
        return subprocess.run(
            [sys.executable, str(RUNTIME / "ledger" / "add-watch.py"),
             "quest-adopt", json.dumps(payload)],
            text=True,
            capture_output=True,
            env=env,
        )

    def test_adoption_refreshes_duplicate_without_adding_one(self) -> None:
        base = {
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "100.000000",
            "created_ts": "200.000000",
            "reason": "existing old thread",
        }
        first = self._add(base)
        self.assertEqual(first.returncode, 0, first.stderr)

        refreshed = self._add(dict(base, refresh_activity=True))
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        self.assertTrue(refreshed.stdout.startswith("skip:duplicate:"))
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(len(watches), 1)
        self.assertGreater(float(watches[0]["last_activity_ts"]), time.time() - 10)

    def test_ordinary_duplicate_does_not_refresh_activity(self) -> None:
        base = {
            "type": "slack_thread",
            "channel_id": "C123",
            "thread_ts": "100.000000",
            "created_ts": "200.000000",
            "last_activity_ts": "250.000000",
            "reason": "existing thread",
        }
        self.assertEqual(self._add(base).returncode, 0)
        self.assertEqual(self._add(base).returncode, 0)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(watches[0]["last_activity_ts"], "250.000000")

    def test_github_pr_searches_are_distinct_watches(self) -> None:
        base = {
            "type": "github_pr",
            "repo": "owner/repo",
            "reason": "track PR activity",
        }
        authored = self._add(dict(base, search="author:me"))
        involved = self._add(dict(base, search="involves:me"))

        self.assertEqual(authored.returncode, 0, authored.stderr)
        self.assertEqual(involved.returncode, 0, involved.stderr)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual([watch["search"] for watch in watches], ["author:me", "involves:me"])

    def test_github_pr_accounts_are_distinct_watches(self) -> None:
        base = {
            "type": "github_pr",
            "repo": "owner/repo",
            "search": "involves:me",
            "reason": "track PR activity",
        }
        personal = self._add(dict(base, gh_account="personal"))
        work = self._add(dict(base, gh_account="work"))

        self.assertEqual(personal.returncode, 0, personal.stderr)
        self.assertEqual(work.returncode, 0, work.stderr)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual([watch["gh_account"] for watch in watches], ["personal", "work"])

    def test_github_pr_exact_identity_is_duplicate(self) -> None:
        watch = {
            "type": "github_pr",
            "repo": "owner/repo",
            "search": "involves:me",
            "gh_account": "work",
            "reason": "track PR activity",
        }
        self.assertEqual(self._add(watch).returncode, 0)
        duplicate = self._add(dict(watch, reason="same result set"))

        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn("skip:duplicate:", duplicate.stdout)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(len(watches), 1)

    def test_github_pr_empty_identity_fields_match_absent_fields(self) -> None:
        base = {
            "type": "github_pr",
            "repo": "owner/repo",
            "reason": "track PR activity",
        }
        self.assertEqual(self._add(base).returncode, 0)
        duplicate = self._add(dict(base, search="  ", gh_account=""))

        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn("skip:duplicate:", duplicate.stdout)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(len(watches), 1)

    def test_github_pr_legacy_empty_identity_fields_match_new_absent_fields(self) -> None:
        legacy = {
            "type": "github_pr",
            "repo": "owner/repo",
            "search": " ",
            "gh_account": "",
            "reason": "legacy empty fields",
        }
        self.watch_path.write_text(json.dumps({"watches": [legacy]}))
        duplicate = self._add({
            "type": "github_pr",
            "repo": "owner/repo",
            "reason": "new absent fields",
        })

        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn("skip:duplicate:", duplicate.stdout)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(len(watches), 1)

    def test_github_pr_legacy_repo_watch_can_coexist_with_scoped_watch(self) -> None:
        legacy = {
            "type": "github_pr",
            "repo": "owner/repo",
            "reason": "legacy repo watch",
        }
        scoped = dict(legacy, search="author:me", gh_account="work")

        self.assertEqual(self._add(legacy).returncode, 0)
        self.assertEqual(self._add(scoped).returncode, 0)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual(len(watches), 2)

    def test_github_issue_accounts_are_distinct_watches(self) -> None:
        base = {
            "type": "github_issue",
            "repo": "owner/repo",
            "search": "label:docs",
            "reason": "track issue activity",
        }
        personal = self._add(dict(base, gh_account="personal"))
        work = self._add(dict(base, gh_account="work"))

        self.assertEqual(personal.returncode, 0, personal.stderr)
        self.assertEqual(work.returncode, 0, work.stderr)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual([watch["gh_account"] for watch in watches], ["personal", "work"])

    def test_slack_watch_rejects_user_id_as_channel_id(self) -> None:
        result = self._add({
            "type": "slack_thread",
            "channel_id": "U123",
            "thread_ts": "100.000000",
            "reason": "invalid user ID in channel field",
        })

        self.assertEqual(result.returncode, 2)
        self.assertIn("bad_channel_id_for_slack_thread:U123", result.stderr)
        self.assertIn("member id", result.stderr)
        self.assertEqual(json.loads(self.watch_path.read_text())["watches"], [])

    def test_slack_channel_watch_rejects_a_user_id(self) -> None:
        result = self._add({
            "type": "slack_channel",
            "channel_id": "U0AAAA1",
            "reason": "user ID in a channel watch",
        })

        self.assertEqual(result.returncode, 2)
        self.assertIn("bad_channel_id_for_slack_channel:U0AAAA1", result.stderr)
        self.assertEqual(json.loads(self.watch_path.read_text())["watches"], [])

    def test_slack_dm_watch_rejects_a_channel_id_in_the_user_field(self) -> None:
        """The mirror image of the channel bug: a conversation pasted into user_id is
        interpolated as <@C123> and matches nothing, silently."""
        result = self._add({
            "type": "slack_dm",
            "channel_id": "D456",
            "user_id": "C123",
            "reason": "channel ID in the user field",
        })

        self.assertEqual(result.returncode, 2)
        self.assertIn("bad_user_id_for_slack_dm:C123", result.stderr)
        self.assertEqual(json.loads(self.watch_path.read_text())["watches"], [])

    def test_slack_watch_rejects_the_imaginary_mp_prefix(self) -> None:
        """Slack has no MP namespace; a group DM is a G… conversation."""
        result = self._add({
            "type": "slack_channel",
            "channel_id": "MP123",
            "reason": "mpim prefix that does not exist",
        })

        self.assertEqual(result.returncode, 2)
        self.assertIn("bad_channel_id_for_slack_channel:MP123", result.stderr)

    def test_slack_watch_rejects_prose_and_non_strings(self) -> None:
        for bad in ("Channel", "C", "PROJ-1098", 12345, None):
            with self.subTest(channel_id=bad):
                result = self._add({
                    "type": "slack_channel",
                    "channel_id": bad,
                    "reason": "not a Slack ID",
                })
                self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(self.watch_path.read_text())["watches"], [])

    def test_slack_dm_watch_accepts_an_enterprise_grid_member_id(self) -> None:
        """W… is a real member prefix on Enterprise Grid; rejecting it would block a
        legitimate watch."""
        result = self._add({
            "type": "slack_dm",
            "channel_id": "D0AAAA9",
            "user_id": "W0AAAA9",
            "reason": "Enterprise Grid member",
        })

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slack_watch_accepts_a_group_dm_which_is_a_c_id(self) -> None:
        """A group DM is NOT a G… id: a live Enterprise Grid workspace returns C… for
        mpims (e.g. C0B2CJEPV7U), the same prefix as a public channel. G… still exists for
        legacy private channels (#se-team = G01EC0UKCSZ), so both must pass."""
        for good in ("C0B2CJEPV7U", "G01EC0UKCSZ"):
            with self.subTest(channel_id=good):
                result = self._add({
                    "type": "slack_thread",
                    "channel_id": good,
                    "thread_ts": "100.000000",
                    "reason": "a real conversation ID observed in production",
                })
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_slack_watch_accepts_every_real_conversation_prefix(self) -> None:
        for good in ("C0123456789", "D0AAAA0", "G0AAAA1"):
            with self.subTest(channel_id=good):
                result = self._add({
                    "type": "slack_channel",
                    "channel_id": good,
                    "reason": "a real conversation ID",
                })
                self.assertEqual(result.returncode, 0, result.stderr)
        watches = json.loads(self.watch_path.read_text())["watches"]
        self.assertEqual([w["channel_id"] for w in watches],
                         ["C0123456789", "D0AAAA0", "G0AAAA1"])


class NewQuestSlackIdTest(unittest.TestCase):
    """The other watch-creation path. A check on add-watch.py alone is not a check:
    whichever path is unguarded is the one the next broken watch arrives through."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sidequestor-new-quest-")
        self.workspace = Path(self.temp.name)
        (self.workspace / "state" / "quests" / "active").mkdir(parents=True)
        (self.workspace / "yaas-triage").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(self, watches: list[dict]) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["SIDEQUESTOR_WORKSPACE"] = str(self.workspace)
        env["SIDEQUESTOR_RUNTIME_ROOT"] = str(RUNTIME.parent)
        spec = {"title": "Watch ID shapes", "context": "c", "watches": watches}
        return subprocess.run(
            [sys.executable,
             str(RUNTIME / "skills" / "yaas-quest-creation" / "new-quest.py"),
             json.dumps(spec)],
            text=True, capture_output=True, env=env,
        )

    def _quest_dirs(self) -> list[Path]:
        return list((self.workspace / "state" / "quests" / "active").iterdir())

    def test_quest_creation_rejects_a_user_id_as_channel_id(self) -> None:
        result = self._create([{
            "type": "slack_thread",
            "channel_id": "U123",
            "thread_ts": "100.000000",
            "reason": "user ID pasted into the channel field",
        }])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("member id", result.stderr + result.stdout)
        self.assertEqual(self._quest_dirs(), [])

    def test_quest_creation_rejects_a_channel_id_in_the_user_field(self) -> None:
        result = self._create([{
            "type": "slack_mention",
            "user_id": "C0123456789",
            "reason": "channel ID pasted into the user field",
        }])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user_id", result.stderr + result.stdout)
        self.assertEqual(self._quest_dirs(), [])

    def test_quest_creation_still_accepts_well_formed_ids(self) -> None:
        result = self._create([{
            "type": "slack_dm",
            "channel_id": "D0AAAA0",
            "user_id": "U0AAAA0",
            "reason": "a correctly shaped DM watch",
        }])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._quest_dirs()), 1)

    def test_quest_creation_preserves_legacy_watch_mode(self) -> None:
        result = self._create([{
            "type": "slack_channel",
            "channel_id": "C0123456789",
            "reason": "legacy watch compatibility",
            "watch_mode": "read_only",
        }])

        self.assertEqual(result.returncode, 0, result.stderr)
        quest = self._quest_dirs()[0]
        watch = json.loads((quest / "watch.json").read_text())["watches"][0]
        self.assertEqual(watch["watch_mode"], "read_only")


if __name__ == "__main__":
    unittest.main()
