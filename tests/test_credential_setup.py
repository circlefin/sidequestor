from __future__ import annotations

import getpass
import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidequestor import credential_setup
from sidequestor.cli import _cmd_credentials
from sidequestor.workspace import init_workspace

SECRET = "tok_Secret-123=abc"
RUNTIME = Path(__file__).resolve().parents[1] / "src/sidequestor/runtime/yaas-triage"


class FakeKeychain:
    """Models `security` for find (presence only) and `-i` add-generic-password -U."""

    def __init__(self, items=None, find_code=None, write_code=0):
        self.items = dict(items or {})
        self.find_code = find_code
        self.write_code = write_code
        self.calls = []

    def __call__(self, args, input=None, **_kwargs):
        self.calls.append((list(args), input))
        if args[1] == "find-generic-password":
            if self.find_code is not None:
                return SimpleNamespace(returncode=self.find_code, stdout="", stderr="")
            path = args[6] if len(args) > 6 else None
            found = (args[3], args[5], path) in self.items
            return SimpleNamespace(returncode=0 if found else 44, stdout="", stderr="")
        assert args[1:] == ["-i"], args
        if self.write_code:
            return SimpleNamespace(returncode=self.write_code, stdout="", stderr="")
        words = shlex.split(input)
        assert words[:2] == ["add-generic-password", "-U"], words
        service, account, token = words[3], words[5], words[7]
        path = words[8] if len(words) > 8 else None
        self.items[(service, account, path)] = token
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def argv(self):
        return [arg for args, _ in self.calls for arg in args]


def run_set_token(integration, keychain, environment=None, answer=SECRET, tty=True):
    out, err = StringIO(), StringIO()
    prompts = []

    def prompt(text):
        prompts.append(text)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    with redirect_stdout(out), redirect_stderr(err):
        code = credential_setup.set_token(integration, environment or {}, prompt=prompt,
                                          is_tty=lambda: tty, run=keychain)
    return code, out.getvalue(), err.getvalue(), prompts


class SetTokenTests(unittest.TestCase):
    def test_jira_updates_the_existing_item_without_exposing_the_token(self) -> None:
        keychain = FakeKeychain({("jira-api-token", "yaas", None): "old"})
        code, out, err, _ = run_set_token("jira", keychain,
                                          {"JIRA_EMAIL": "a@b.c", "JIRA_BASE_URL": "https://x"})
        self.assertEqual(code, 0, err)
        self.assertEqual(keychain.items, {("jira-api-token", "yaas", None): SECRET})
        self.assertIn("replace the existing", out)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn(SECRET, " ".join(keychain.argv()))

    def test_jira_creates_the_fixed_item_when_missing(self) -> None:
        keychain = FakeKeychain()
        code, out, _, _ = run_set_token("jira", keychain)
        self.assertEqual(code, 0)
        self.assertEqual(keychain.items, {("jira-api-token", "yaas", None): SECRET})
        self.assertIn("JIRA_EMAIL and JIRA_BASE_URL", out)

    def test_notion_creates_the_documented_default_when_nothing_exists(self) -> None:
        keychain = FakeKeychain()
        self.assertEqual(run_set_token("notion", keychain)[0], 0)
        self.assertEqual(keychain.items, {("sidequestor-notion", "sidequestor", None): SECRET})

    def test_notion_updates_the_legacy_test_item_instead_of_relocating_it(self) -> None:
        keychain = FakeKeychain({("sidequestor-notion-test", "sidequestor", None): "old"})
        self.assertEqual(run_set_token("notion", keychain)[0], 0)
        self.assertEqual(keychain.items, {("sidequestor-notion-test", "sidequestor", None): SECRET})

    def test_notion_prefers_the_default_item_like_the_reader(self) -> None:
        keychain = FakeKeychain({("sidequestor-notion", "sidequestor", None): "a",
                                 ("sidequestor-notion-test", "sidequestor", None): "b"})
        self.assertEqual(run_set_token("notion", keychain)[0], 0)
        self.assertEqual(keychain.items[("sidequestor-notion", "sidequestor", None)], SECRET)
        self.assertEqual(keychain.items[("sidequestor-notion-test", "sidequestor", None)], "b")

    def test_notion_honours_explicit_service_account_and_keychain_path(self) -> None:
        keychain = FakeKeychain()
        environment = {"NOTION_KEYCHAIN_SERVICE": "custom-notion",
                       "NOTION_KEYCHAIN_ACCOUNT": "me",
                       "NOTION_KEYCHAIN_PATH": "~/Library/Keychains/other.keychain-db"}
        self.assertEqual(run_set_token("notion", keychain, environment)[0], 0)
        path = os.path.expanduser("~/Library/Keychains/other.keychain-db")
        self.assertEqual(keychain.items, {("custom-notion", "me", path): SECRET})
        # A configured service is used alone, exactly as notion-call.py does.
        services = [args[3] for args, _ in keychain.calls if args[1] == "find-generic-password"]
        self.assertEqual(services, ["custom-notion"])

    def test_notion_explicitly_empty_account_matches_the_reader(self) -> None:
        keychain = FakeKeychain()
        self.assertEqual(run_set_token("notion", keychain, {"NOTION_KEYCHAIN_ACCOUNT": ""})[0], 0)
        self.assertEqual(keychain.items, {("sidequestor-notion", "", None): SECRET})

    def test_terminal_that_cannot_hide_input_stops_before_reading(self) -> None:
        read = []

        def degraded_getpass(_text):
            # Mirrors getpass.fallback_getpass: warn, then read with echo on.
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            read.append(True)
            return SECRET

        keychain = FakeKeychain({("jira-api-token", "yaas", None): "old"})
        out, err = StringIO(), StringIO()
        with patch.object(credential_setup.getpass, "getpass", degraded_getpass), \
                redirect_stdout(out), redirect_stderr(err):
            code = credential_setup.set_token("jira", {}, is_tty=lambda: True, run=keychain)
        self.assertEqual(code, 1)
        self.assertEqual(read, [])
        self.assertIn("cannot hide input", err.getvalue())
        self.assertEqual(keychain.items, {("jira-api-token", "yaas", None): "old"})

    def test_notion_token_precedence_is_reported(self) -> None:
        code, out, _, _ = run_set_token("notion", FakeKeychain(), {"NOTION_TOKEN": "env"})
        self.assertEqual(code, 0)
        self.assertIn("NOTION_TOKEN is set and still takes precedence", out)

    def test_empty_input_and_interrupts_leave_credentials_untouched(self) -> None:
        for answer, expected in (("", 1), ("   ", 1), (EOFError(), 130), (KeyboardInterrupt(), 130)):
            with self.subTest(answer=answer):
                keychain = FakeKeychain({("jira-api-token", "yaas", None): "old"})
                code, _, err, _ = run_set_token("jira", keychain, answer=answer)
                self.assertEqual(code, expected)
                self.assertIn("nothing was changed", err)
                self.assertEqual(keychain.items, {("jira-api-token", "yaas", None): "old"})
                self.assertFalse(any(args[1] == "-i" for args, _ in keychain.calls))

    def test_non_terminal_input_is_refused_before_prompting(self) -> None:
        keychain = FakeKeychain()
        code, _, err, prompts = run_set_token("jira", keychain, tty=False)
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", err)
        self.assertEqual((prompts, keychain.calls), ([], []))

    def test_locked_or_unavailable_keychain_does_not_write(self) -> None:
        keychain = FakeKeychain(find_code=36)
        code, _, err, prompts = run_set_token("notion", keychain)
        self.assertEqual(code, 1)
        self.assertIn("nothing was changed", err)
        self.assertEqual(prompts, [])
        self.assertFalse(any(args[1] == "-i" for args, _ in keychain.calls))

    def test_refused_write_reports_unchanged_and_hides_the_token(self) -> None:
        keychain = FakeKeychain({("jira-api-token", "yaas", None): "old"}, write_code=128)
        code, out, err, _ = run_set_token("jira", keychain)
        self.assertEqual(code, 1)
        self.assertIn("existing token is unchanged", err)
        self.assertNotIn(SECRET, out + err)
        self.assertEqual(keychain.items, {("jira-api-token", "yaas", None): "old"})

    def test_tokens_that_cannot_be_passed_safely_are_rejected(self) -> None:
        for bad in ('a"b', "a b", "a\\b", "tök"):
            with self.subTest(bad=bad):
                keychain = FakeKeychain()
                code, out, err, _ = run_set_token("jira", keychain, answer=bad)
                self.assertEqual(code, 2)
                self.assertNotIn(bad, out + err)
                self.assertEqual(keychain.items, {})


class CredentialsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_home = tempfile.TemporaryDirectory(prefix="sidequestor-cli-config-")
        self.config_patch = patch.dict("os.environ", {"YAAS_CONFIG_HOME": self.config_home.name})
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
        self.config_home.cleanup()

    def test_token_arguments_are_rejected_without_being_echoed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            for args in (["set-token", "jira", SECRET], ["set-token", SECRET], ["set-token"]):
                with self.subTest(args=args):
                    err = StringIO()
                    with patch.object(credential_setup, "set_token") as set_token, redirect_stderr(err):
                        self.assertEqual(_cmd_credentials(workspace, args), 2)
                    set_token.assert_not_called()
                    self.assertNotIn(SECRET, err.getvalue())

    def test_workspace_env_selects_the_notion_item(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sidequestor-cli-") as raw:
            workspace = init_workspace(Path(raw))
            workspace.env_file.write_text("NOTION_KEYCHAIN_SERVICE=from-dotenv\n")
            with patch.dict("os.environ", {}, clear=False), \
                    patch.object(credential_setup, "set_token", return_value=0) as set_token:
                os.environ.pop("NOTION_KEYCHAIN_SERVICE", None)
                self.assertEqual(_cmd_credentials(workspace, ["set-token", "notion"]), 0)
            integration, environment = set_token.call_args.args
            self.assertEqual(integration, "notion")
            self.assertEqual(environment["NOTION_KEYCHAIN_SERVICE"], "from-dotenv")


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("SIDEQUESTOR_KEYCHAIN_TESTS") == "1",
                     "opt-in: set SIDEQUESTOR_KEYCHAIN_TESTS=1 on macOS to use a throwaway keychain")
class RealKeychainTests(unittest.TestCase):
    def test_written_token_is_read_back_by_the_notion_runtime_reader(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sq-keychain-") as raw:
            path = str(Path(raw) / "throwaway.keychain-db")
            subprocess.run(["/usr/bin/security", "create-keychain", "-p", "pw", path], check=True)
            self.addCleanup(subprocess.run, ["/usr/bin/security", "delete-keychain", path],
                            capture_output=True)
            subprocess.run(["/usr/bin/security", "unlock-keychain", "-p", "pw", path], check=True)
            environment = {"NOTION_KEYCHAIN_PATH": path}
            for token in ("ntn_first", SECRET):  # create, then update in place
                code, _, err, _ = run_set_token("notion", subprocess.run, environment, answer=token)
                self.assertEqual(code, 0, err)
            spec = importlib.util.spec_from_file_location("notion_call", RUNTIME / "surfaces/notion-call.py")
            reader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reader)
            with patch.dict("os.environ", environment):
                os.environ.pop("NOTION_TOKEN", None)
                os.environ.pop("NOTION_KEYCHAIN_SERVICE", None)
                os.environ.pop("NOTION_KEYCHAIN_ACCOUNT", None)
                self.assertEqual(reader.token(), SECRET)


if __name__ == "__main__":
    unittest.main()
