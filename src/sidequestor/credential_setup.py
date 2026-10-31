"""Store a Jira or Notion API token in the Keychain item the runtime already reads.

This only writes. It resolves the same location the 0.2.0 readers use
(``surfaces/client.py`` for Jira, ``surfaces/notion-call.py`` for Notion) and never
renames, relocates or recreates other records. The token is read from a hidden
terminal prompt and handed to ``security`` on stdin, so it never appears in argv,
the environment, output, logs or temporary files.
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass
from typing import Callable, Mapping

SECURITY = "/usr/bin/security"
INTEGRATIONS = ("jira", "notion")
JIRA_SERVICE, JIRA_ACCOUNT = "jira-api-token", "yaas"
NOTION_SERVICES = ("sidequestor-notion", "sidequestor-notion-test")
NOTION_ACCOUNT = "sidequestor"
# `security -i` tokenizes its stdin like a shell line. Restricting values to
# characters that need no escaping keeps the secret out of argv without a parser.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=:-]+")
SAFE_FIELD = re.compile(r'[^"\\\n\r]*')
NOT_FOUND = 44


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class Location:
    service: str
    account: str
    path: str | None = None
    exists: bool = False

    def describe(self) -> str:
        where = f"service={self.service}, account={self.account}"
        return where + (f", keychain={self.path}" if self.path else "")


def _lookup(service: str, account: str, path: str | None, run) -> bool:
    # No -w: this checks presence without ever reading the secret.
    args = [SECURITY, "find-generic-password", "-s", service, "-a", account]
    if path:
        args.append(path)
    try:
        proc = run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError("macOS Keychain is unavailable") from exc
    if proc.returncode == 0:
        return True
    if proc.returncode == NOT_FOUND:
        return False
    raise SetupError("could not inspect the Keychain (is it locked?); nothing was changed")


def resolve(integration: str, environment: Mapping[str, str], run=subprocess.run) -> Location:
    """Return the Keychain item the runtime reads for ``integration``."""
    if integration == "jira":
        # client.py reads this fixed item from the default keychain search list.
        return Location(JIRA_SERVICE, JIRA_ACCOUNT,
                        exists=_lookup(JIRA_SERVICE, JIRA_ACCOUNT, None, run))
    if integration != "notion":
        raise SetupError(f"unsupported integration; choose one of: {', '.join(INTEGRATIONS)}")
    # Mirror notion-call.py exactly: an explicitly empty account stays empty.
    account = environment.get("NOTION_KEYCHAIN_ACCOUNT", NOTION_ACCOUNT)
    path = environment.get("NOTION_KEYCHAIN_PATH")
    path = os.path.expanduser(path) if path else None
    configured = environment.get("NOTION_KEYCHAIN_SERVICE")
    services = [configured] if configured else list(NOTION_SERVICES)
    # Update whichever item notion-call.py would read first; create the documented
    # default only when none exists.
    for service in services:
        if _lookup(service, account, path, run):
            return Location(service, account, path, exists=True)
    return Location(services[0], account, path, exists=False)


def write(location: Location, token: str, run=subprocess.run) -> None:
    fields = [location.service, location.account] + ([location.path] if location.path else [])
    if not all(SAFE_FIELD.fullmatch(field) for field in fields):
        raise SetupError("Keychain service, account or path contains unsupported characters")
    # -U updates the existing item in place, keeping its access list, so headless
    # reads by `security find-generic-password` keep working without new prompts.
    command = f'add-generic-password -U -s "{location.service}" -a "{location.account}" -w "{token}"'
    if location.path:
        command += f' "{location.path}"'
    try:
        proc = run([SECURITY, "-i"], input=command + "\n", capture_output=True,
                   text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError("macOS Keychain write did not complete; the existing token is unchanged") from exc
    if proc.returncode:
        raise SetupError("macOS Keychain refused the write (locked or cancelled); "
                         "the existing token is unchanged")


def hidden_prompt(text: str) -> str:
    # getpass warns and then reads with echo on when it cannot disable terminal echo.
    # Turning that warning into an error stops before anything is read or shown.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(text)
        except getpass.GetPassWarning as exc:
            raise SetupError("this terminal cannot hide input; nothing was changed") from exc


def set_token(integration: str, environment: Mapping[str, str], *,
              prompt: Callable[[str], str] = hidden_prompt,
              is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
              run=subprocess.run) -> int:
    if integration not in INTEGRATIONS:
        print(f"error: choose one of: {', '.join(INTEGRATIONS)}", file=sys.stderr)
        return 2
    if not is_tty():
        # getpass falls back to echoing input without a terminal.
        print("error: run this in an interactive terminal; tokens are not read from "
              "pipes or arguments", file=sys.stderr)
        return 2
    try:
        location = resolve(integration, environment, run)
        label = "Jira API token" if integration == "jira" else "Notion integration token"
        action = "replace the existing" if location.exists else "create a new"
        print(f"This will {action} Keychain item ({location.describe()}).")
        try:
            token = prompt(f"{label} (input hidden, empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled; nothing was changed.", file=sys.stderr)
            return 130
        if not token:
            print("Cancelled; nothing was changed.", file=sys.stderr)
            return 1
        if not TOKEN_PATTERN.fullmatch(token):
            print("error: that does not look like an API token (unexpected characters); "
                  "nothing was changed", file=sys.stderr)
            return 2
        write(location, token, run)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {label} to Keychain ({location.describe()}).")
    if integration == "notion" and environment.get("NOTION_TOKEN", "").strip():
        print("Note: NOTION_TOKEN is set and still takes precedence over the Keychain item.")
    if integration == "jira" and not (environment.get("JIRA_EMAIL") and environment.get("JIRA_BASE_URL")):
        print("Note: also set JIRA_EMAIL and JIRA_BASE_URL in the workspace .env.")
    return 0
