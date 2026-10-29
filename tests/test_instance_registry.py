import json
import os
import shlex
import sys
import tempfile
import unittest
from contextlib import chdir, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from sidequestor.cli import _cmd_init, _cmd_instances, _dispatch, _workspace, main
from sidequestor.workspace import (
    default_workspace, init_workspace, list_instances, register_workspace,
    rekey_workspace,
)


class InstanceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sq-instance-registry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.environment = patch.dict(os.environ, {"SIDEQUESTOR_CONFIG_HOME": str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.registry = self.config / "yaas/instances.json"
        self.workspace = init_workspace(self.root / "workspace", "current")
        self.other = init_workspace(self.root / "other", "other")

    def stale_rows(self):
        rows = list_instances()
        rows[:0] = [dict(instance_id=f"old-{i}", display_name=f"old-{i}",
                        path=str(self.workspace.root)) for i in range(3)]
        self.registry.write_text(json.dumps(rows))
        return rows

    def test_stale_stop_and_pause_never_reach_lifecycle(self):
        self.stale_rows()
        before = self.registry.read_bytes()
        with patch("sidequestor.cli._cmd_stop") as stop:
            for command in ("stop", "pause"):
                for args, instance in ((["old-0"], None), ([], "old-0")):
                    with self.subTest(command=command, args=args, instance=instance):
                        with self.assertRaisesRegex(SystemExit, "stale.*old-0"):
                            _dispatch(command, args, None, instance)
            stop.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_stale_instance_selector_is_rejected_for_other_commands(self):
        self.stale_rows()
        with self.assertRaisesRegex(SystemExit, "stale.*old-1"):
            _workspace(None, "old-1")
        with patch("sidequestor.cli._cmd_start") as start:
            with self.assertRaisesRegex(SystemExit, "stale.*old-1"):
                _dispatch("start", [], None, "old-1")
            start.assert_not_called()

    def test_stale_recovery_hint_shell_quotes_the_exact_folder(self):
        target = init_workspace(self.root / "both'\" $HOME folder")
        rows = list_instances()
        rows.append(dict(instance_id="stale-quoted", path=str(target.root)))
        self.registry.write_text(json.dumps(rows))
        with self.assertRaises(SystemExit) as error:
            _workspace(None, "stale-quoted")
        command = str(error.exception).split(". Run ", 1)[1].removesuffix(" to refresh its registration.")
        self.assertEqual(shlex.split(command),
                         ["sq", "--workspace", str(target.root), "instances", "register"])

    def test_missing_runtime_recovery_hint_targets_its_workspace(self):
        from sidequestor.launchd import LaunchdLifecycleError, start_executable
        target = init_workspace(self.root / "repair's workspace")
        with patch("sidequestor.launchd.production_status", return_value={"python": str(self.root / "missing-python")}), \
                self.assertRaises(LaunchdLifecycleError) as error:
            start_executable(target, Path(sys.executable))
        command = str(error.exception).split("Run ", 1)[1].split(" to repair", 1)[0]
        self.assertEqual(shlex.split(command), ["sq", "--workspace", str(target.root), "setup"])

    def test_valid_stop_targets_selected_workspace_not_default(self):
        for args, instance in (([self.other.instance_id], None),
                               ([], self.other.instance_id), (["other"], None)):
            with self.subTest(args=args, instance=instance), \
                    patch("sidequestor.launchd.stop_production", return_value=True) as stop, \
                    patch("sidequestor.cli.stop_dashboard_process", return_value=False), \
                    redirect_stdout(StringIO()) as output:
                self.assertEqual(_dispatch("stop", args, None, instance), 0)
                stop.assert_called_once_with(self.other)
                self.assertIn(self.other.instance_id, output.getvalue())
                self.assertNotIn(self.workspace.instance_id, output.getvalue())

    def test_listing_does_not_count_stale_ids_as_running_instances(self):
        self.stale_rows()
        before = self.registry.read_bytes()
        with patch("sidequestor.cli.production_is_running", return_value=True) as running, \
                redirect_stdout(StringIO()) as output:
            self.assertEqual(_cmd_instances([]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertNotIn("old-", output.getvalue())
        self.assertEqual(running.call_count, 2)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_all_listing_marks_stale_ids_without_checking_their_jobs(self):
        self.stale_rows()
        with patch("sidequestor.cli.production_is_running", return_value=True) as running, \
                redirect_stdout(StringIO()) as output:
            self.assertEqual(_cmd_instances(["list", "--all"]), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len([line for line in lines if line.startswith("STALE")]), 3)
        self.assertEqual(len([line for line in lines if line.startswith("RUNNING")]), 2)
        self.assertEqual(running.call_count, 2)

    def test_register_removes_only_old_entries_for_the_same_resolved_path(self):
        rows = self.stale_rows()
        alias = self.root / "workspace-alias"
        alias.symlink_to(self.workspace.root, target_is_directory=True)
        rows[0]["path"] = str(alias)
        rows[0]["default"] = True
        for row in rows[3:]:
            row.pop("default", None)
        rows.append(dict(instance_id="missing-workspace", path=str(self.root / "missing")))
        self.registry.write_text(json.dumps(rows))
        register_workspace(self.workspace)
        remaining = list_instances()
        self.assertEqual({row["instance_id"] for row in remaining},
                         {self.workspace.instance_id, self.other.instance_id, "missing-workspace"})
        self.assertEqual(default_workspace(), self.workspace)
        self.assertEqual(next(row for row in remaining if row["instance_id"] == self.other.instance_id),
                         next(row for row in rows if row["instance_id"] == self.other.instance_id))

    def test_rekey_replaces_old_registration_and_preserves_default_workspace(self):
        updated = rekey_workspace(self.workspace.root)
        self.assertNotEqual(updated.instance_id, self.workspace.instance_id)
        self.assertEqual({row["instance_id"] for row in list_instances()},
                         {updated.instance_id, self.other.instance_id})
        self.assertEqual(default_workspace(), updated)

    def test_conflicting_workspace_and_instance_never_stop_either_workspace(self):
        with patch("sidequestor.cli._cmd_stop") as stop:
            for command in ("stop", "pause"):
                with self.subTest(command=command), \
                        self.assertRaisesRegex(SystemExit, "either --workspace or --instance"):
                    _dispatch(command, [], str(self.workspace.root), self.other.instance_id)
            stop.assert_not_called()

    def test_picker_offers_only_valid_workspaces(self):
        rows = self.stale_rows()
        rows.append(dict(instance_id="missing", display_name="missing", path=str(self.root / "missing")))
        self.registry.write_text(json.dumps(rows))
        with patch("builtins.input", return_value="2"), redirect_stdout(StringIO()) as output:
            self.assertEqual(_dispatch("setup", ["--select-workspace"], None, None), 0)
        self.assertEqual(default_workspace(), self.other)
        self.assertNotIn("old-", output.getvalue())
        self.assertNotIn("missing", output.getvalue())
        self.assertIn(self.other.instance_id, output.getvalue())

    def test_picker_with_no_valid_entries_does_not_ask_or_change_registry(self):
        rows = self.stale_rows()[:3]
        self.registry.write_text(json.dumps(rows))
        before = self.registry.read_bytes()
        with patch("builtins.input") as question:
            with self.assertRaisesRegex(SystemExit, "No valid registered workspaces"):
                _dispatch("setup", ["--select-workspace"], None, None)
            question.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_picker_can_select_a_workspace_with_duplicate_valid_rows(self):
        rows = list_instances()
        rows.append(dict(rows[1]))
        self.registry.write_text(json.dumps(rows))
        with patch("builtins.input", return_value="2"), redirect_stdout(StringIO()):
            self.assertEqual(_dispatch("setup", ["--select-workspace"], None, None), 0)
        self.assertEqual(default_workspace(), self.other)
        self.assertEqual(len(list_instances()), 2)

    def test_picker_and_registration_skip_corrupt_path_rows(self):
        loop = self.root / "loop"
        loop.symlink_to(loop)
        rows = list_instances() + [dict(instance_id="loop", path=str(loop)),
                                   dict(instance_id="nul", path="bad\0path")]
        self.registry.write_text(json.dumps(rows))
        with patch("builtins.input", return_value="2"), redirect_stdout(StringIO()):
            self.assertEqual(_dispatch("setup", ["--select-workspace"], None, None), 0)
        self.assertEqual(default_workspace(), self.other)

    def test_corrupt_selected_registry_paths_fail_with_guidance(self):
        rows = list_instances()
        rows[0]["path"] = "bad\0path"
        self.registry.write_text(json.dumps(rows))
        with self.assertRaisesRegex(SystemExit, "Default workspace is unavailable"):
            default_workspace()
        with self.assertRaisesRegex(SystemExit, "Registered workspace is unavailable"):
            _workspace(None, self.workspace.instance_id)

    def test_picker_rechecks_identity_after_owner_selects(self):
        before = self.registry.read_bytes()
        def choose(_prompt):
            marker = self.other.yaas_dir / "instance.json"
            data = json.loads(marker.read_text())
            data["instance_id"] = "replacement"
            marker.write_text(json.dumps(data))
            return "2"
        with patch("builtins.input", side_effect=choose), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(SystemExit, "stale instance registration"):
                _dispatch("setup", ["--select-workspace"], None, None)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(default_workspace(), self.workspace)

    def test_init_next_setup_command_selects_new_folder_despite_existing_default_and_environment(self):
        target = self.root / "new workspace's folder"
        with patch("sidequestor.cli.sync_resources"), redirect_stdout(StringIO()) as output:
            self.assertEqual(_cmd_init([str(target)]), 0)
        next_line = next(line for line in output.getvalue().splitlines() if line.startswith("Next: "))
        command = shlex.split(next_line.removeprefix("Next: "))
        self.assertEqual(command[:2], ["sq", "--workspace"])
        with patch.dict(os.environ, {"SIDEQUESTOR_WORKSPACE": str(self.workspace.root)}), \
                patch("sidequestor.launchd.start_executable", return_value=Path(sys.executable)), \
                patch("sidequestor.cli._cmd_setup", return_value=0) as setup, \
                redirect_stdout(StringIO()):
            self.assertEqual(main(command[1:]), 0)
        self.assertEqual(setup.call_args.args[0].root, target.resolve())
        # Only dispatch/selection runs here: real setup subsequently saves its target as default.
        self.assertEqual(default_workspace(), self.workspace)

    def test_readme_install_setup_explicitly_targets_current_new_workspace(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        install_block = readme.split("```bash", 1)[1].split("```", 1)[0]
        commands = [shlex.split(line) for line in install_block.splitlines()
                    if line.startswith("sq ")]
        setup_command = next(command for command in commands
                             if command[-1] == "setup" and "--manifest" not in command)
        with patch.dict(os.environ, {"SIDEQUESTOR_WORKSPACE": str(self.workspace.root)}), \
                chdir(self.other.root), \
                patch("sidequestor.launchd.start_executable", return_value=Path(sys.executable)), \
                patch("sidequestor.cli._cmd_setup", return_value=0) as setup, \
                redirect_stdout(StringIO()):
            self.assertEqual(main(setup_command[1:]), 0)
        self.assertEqual(setup.call_args.args[0], self.other)
        # _cmd_setup is mocked; this does not assert that a real setup preserves the old default.
        self.assertEqual(default_workspace(), self.workspace)
