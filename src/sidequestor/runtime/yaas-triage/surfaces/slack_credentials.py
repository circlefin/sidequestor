#!/usr/bin/env python3
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Own the lifecycle of the Slack credential used by deterministic checkers."""

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REFRESH_WINDOW_SECONDS = 300
REFRESH_TOKEN_LIFETIME_SECONDS = 30 * 24 * 60 * 60
BUNDLE_SERVICE = "slack-oauth-token-bundle"
LEGACY_SERVICE = "slack-xoxp-token"
KEYCHAIN_ACCOUNT = "yaas"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
HTTP_TIMEOUT_SECONDS = 30


class CredentialError(Exception):
    """A local credential is missing, incomplete, or unusable."""


class AuthenticationError(CredentialError):
    """Slack authorization must be completed again by the user."""


class MissingCredentialError(AuthenticationError):
    """No usable Slack credential has been installed."""


class TransientCredentialError(CredentialError):
    """Credential acquisition may succeed on a later attempt."""


class RefreshError(CredentialError):
    """Slack did not return a usable replacement credential generation."""


class HelperUnavailableError(CredentialError):
    """The Keychain helper could not be built or run on this machine."""


class MacOSKeychain:
    """Use a stable local helper so Keychain values never enter argv."""

    def __init__(self):
        if sys_platform() != "darwin":
            raise CredentialError("Slack Keychain storage requires macOS")
        root = Path(__file__).resolve().parents[2]
        self.source = Path(__file__).resolve().with_name("keychain-helper.c")
        self.helper = root / "state" / "bin" / "yaas-keychain-helper"

    def _ensure_helper(self):
        if self.helper.is_file() and os.access(self.helper, os.X_OK):
            return
        self.helper.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.helper.with_name(f"{self.helper.name}.{os.getpid()}.tmp")
        try:
            result = subprocess.run(
                ["/usr/bin/clang", str(self.source), "-framework", "Security",
                 "-framework", "CoreFoundation", "-o", str(temporary)],
                capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                raise HelperUnavailableError("could not build the Slack Keychain helper")
            temporary.chmod(0o700)
            os.replace(temporary, self.helper)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HelperUnavailableError("could not build the Slack Keychain helper") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def read(self, service, account):
        if service == LEGACY_SERVICE:
            return self._read_legacy(service, account)
        self._ensure_helper()
        try:
            result = subprocess.run(
                [str(self.helper), "read", service, account],
                capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise TransientCredentialError(
                "macOS Keychain timed out during Slack credential read") from exc
        except OSError as exc:
            raise CredentialError("macOS Keychain command is unavailable") from exc
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 44:
            return None
        if "-25308" in result.stderr:
            raise TransientCredentialError(
                "macOS Keychain is locked during Slack credential read")
        raise CredentialError("macOS Keychain failed during Slack credential read")

    @staticmethod
    def _read_legacy(service, account):
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service,
                 "-a", account, "-w"], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise TransientCredentialError(
                "macOS Keychain timed out during Slack credential read") from exc
        except OSError as exc:
            raise CredentialError("macOS Keychain command is unavailable") from exc
        if result.returncode == 0:
            return result.stdout.rstrip("\n")
        low = result.stderr.lower()
        if "could not be found" in low or "item not found" in low:
            return None
        if "interaction is not allowed" in low or "user interaction" in low:
            raise TransientCredentialError(
                "macOS Keychain is locked during Slack credential read")
        raise CredentialError("macOS Keychain failed during Slack credential read")

    def write(self, service, account, value):
        self._ensure_helper()
        try:
            result = subprocess.run(
                [str(self.helper), "write", service, account],
                input=value, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise TransientCredentialError(
                "macOS Keychain timed out during Slack credential write") from exc
        except OSError as exc:
            raise CredentialError("Slack Keychain helper is unavailable") from exc
        if result.returncode != 0:
            if "-25308" in result.stderr:
                raise TransientCredentialError(
                    "macOS Keychain is locked during Slack credential write")
            raise CredentialError("macOS Keychain failed during Slack credential write")


def sys_platform():
    # Isolated for tests without mutating sys.platform globally.
    import sys
    return sys.platform


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FileLock:
    """Serialize consumption of Slack's single-use refresh token."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        self.handle = os.fdopen(fd, "r+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
        return False


class KeychainCredentialStore:
    """Store one credential generation as one Keychain value."""

    def __init__(self, keychain, account=KEYCHAIN_ACCOUNT):
        self.keychain = keychain
        self.account = account

    def load(self):
        try:
            raw = self.keychain.read(BUNDLE_SERVICE, self.account)
        except HelperUnavailableError:
            # Can't build the helper (e.g. no Command Line Tools) - fall through
            # to the legacy xoxp- read below, which only needs /usr/bin/security.
            raw = None
        if raw:
            try:
                bundle = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise CredentialError("Slack credential bundle is malformed") from exc
            if not isinstance(bundle, dict):
                raise CredentialError("Slack credential bundle is malformed")
            return bundle

        legacy = self.keychain.read(LEGACY_SERVICE, self.account)
        if not legacy:
            return None
        if legacy.startswith("xoxp-"):
            return {"mode": "legacy", "access_token": legacy}
        if legacy.startswith("xoxe.xoxp-"):
            return {"mode": "access-only", "access_token": legacy}
        raise CredentialError("Slack credential has an unsupported format")

    def save(self, bundle):
        encoded = json.dumps(bundle, separators=(",", ":"), sort_keys=True)
        self.keychain.write(BUNDLE_SERVICE, self.account, encoded)


class SlackOAuthTransport:
    """Exchange a single-use refresh token for one complete replacement pair."""

    def __init__(self, request=None, timeout=HTTP_TIMEOUT_SECONDS):
        self.request = request or self._request
        self.timeout = timeout

    def refresh(self, client_id, refresh_token):
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        })
        for attempt in range(2):
            try:
                status, text = self.request(SLACK_TOKEN_URL, body, self.timeout)
                break
            except (TimeoutError, ConnectionError, OSError, urllib.error.URLError) as exc:
                if attempt == 0:
                    continue
                raise TransientCredentialError(
                    "Slack credential refresh failed in transport") from exc

        if status == 429 or status in (502, 503, 504):
            raise TransientCredentialError(
                f"Slack credential refresh returned HTTP {status}")
        if status in (401, 403):
            raise AuthenticationError("Slack rejected the credential refresh")
        if not 200 <= status < 300:
            raise RefreshError(f"Slack credential refresh returned HTTP {status}")

        try:
            response = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RefreshError("Slack credential refresh returned malformed JSON") from exc
        if not isinstance(response, dict):
            raise RefreshError("Slack credential refresh returned malformed JSON")
        if response.get("ok") is not True:
            error = str(response.get("error", "unknown"))
            if error in ("invalid_grant", "invalid_refresh_token", "token_expired",
                         "token_revoked", "invalid_auth", "not_authed"):
                raise AuthenticationError("Slack authorization must be completed again")
            if error in ("ratelimited", "rate_limited", "service_unavailable",
                         "internal_error", "timeout"):
                raise TransientCredentialError("Slack credential refresh is temporarily unavailable")
            raise RefreshError("Slack rejected the credential refresh")
        return response

    @staticmethod
    def _request(url, body, timeout):
        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def bundle_from_oauth_response(response, client_id, now=None):
    """Validate an initial PKCE exchange before it reaches Keychain."""
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise CredentialError("Slack OAuth installation was not successful")
    user = response.get("authed_user")
    team = response.get("team")
    if not isinstance(user, dict) or not isinstance(team, dict):
        raise CredentialError("Slack OAuth installation response is incomplete")

    access_token = user.get("access_token")
    refresh_token = user.get("refresh_token")
    expires_in = user.get("expires_in")
    if (not access_token or not refresh_token
            or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))):
        raise CredentialError("Slack OAuth installation omitted rotating credentials")
    if expires_in <= 0 or not client_id or not user.get("id") or not team.get("id"):
        raise CredentialError("Slack OAuth installation response is incomplete")

    issued_at = int(time.time() if now is None else now)
    refresh_expires_in = user.get(
        "refresh_token_expires_in",
        response.get("refresh_token_expires_in", REFRESH_TOKEN_LIFETIME_SECONDS),
    )
    if (isinstance(refresh_expires_in, bool)
            or not isinstance(refresh_expires_in, (int, float)) or refresh_expires_in <= 0):
        raise CredentialError("Slack OAuth installation returned an invalid refresh expiry")
    return {
        "version": 1,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": issued_at + int(expires_in),
        "refresh_expires_at": issued_at + int(refresh_expires_in),
        "client_id": client_id,
        "user_id": user["id"],
        "team_id": team["id"],
    }


def install_oauth_response(response, client_id, store, lock=None, now=None):
    """Validate first, then install one complete credential generation under lock."""
    bundle = bundle_from_oauth_response(response, client_id, now=now)
    with lock or _NoopLock():
        store.save(bundle)
    return {
        "mode": "rotating",
        "user_id": bundle["user_id"],
        "team_id": bundle["team_id"],
        "expires_at": bundle["expires_at"],
    }


def credential_status(store, now=None):
    """Return operational metadata without returning credential material."""
    bundle = store.load()
    if not bundle:
        return {"mode": "missing", "complete": False}
    mode = bundle.get("mode")
    if mode == "legacy":
        return {"mode": "legacy", "complete": True}
    if mode == "access-only":
        return {"mode": "reauthorization-required", "complete": False}
    current_time = int(time.time() if now is None else now)
    required = ("access_token", "refresh_token", "expires_at", "refresh_expires_at",
                "client_id", "user_id", "team_id")
    complete = bundle.get("version") == 1 and all(bundle.get(key) for key in required)
    result = {"mode": "rotating", "complete": complete}
    if complete:
        result.update({
            "access_expires_in": int(bundle["expires_at"]) - current_time,
            "refresh_expires_in": int(bundle["refresh_expires_at"]) - current_time,
        })
    return result


class SlackCredentials:
    """Return a usable access token while hiding rotation from callers."""

    def __init__(self, store, oauth, clock=None, lock=None):
        self.store = store
        self.oauth = oauth
        self.clock = clock or time.time
        self.lock = lock or _NoopLock()

    def get_access_token(self, rejected_token=None):
        bundle = self._load_bundle()
        if bundle.get("mode") in ("legacy", "access-only"):
            if rejected_token == bundle["access_token"]:
                if bundle["mode"] == "access-only":
                    raise AuthenticationError(
                        "Slack authorization must be completed again")
                raise AuthenticationError("Slack rejected the long-lived credential")
            return bundle["access_token"]
        now = int(self.clock())
        if not self._needs_refresh(bundle, now, rejected_token):
            return bundle["access_token"]

        with self.lock:
            bundle = self._load_bundle()
            now = int(self.clock())
            if not self._needs_refresh(bundle, now, rejected_token):
                return bundle["access_token"]

            response = self.oauth.refresh(bundle["client_id"], bundle["refresh_token"])
            replacement = self._replacement_bundle(bundle, response, now)
            self.store.save(replacement)
            return replacement["access_token"]

    def refresh_now(self):
        """Rotate immediately and return redacted metadata for an interactive caller."""
        current = self._load_bundle()
        if current.get("mode") in ("legacy", "access-only"):
            raise AuthenticationError(
                "this Slack credential cannot be refreshed; complete authorization again")
        self.get_access_token(rejected_token=current["access_token"])
        replacement = self._load_bundle()
        return {
            "mode": "rotating",
            "user_id": replacement.get("user_id"),
            "team_id": replacement.get("team_id"),
            "expires_at": replacement["expires_at"],
        }

    def _load_bundle(self):
        bundle = self.store.load()
        if not isinstance(bundle, dict):
            raise MissingCredentialError("Slack credential bundle is missing")
        if bundle.get("mode") in ("legacy", "access-only") and bundle.get("access_token"):
            return bundle
        if bundle.get("version") != 1:
            raise CredentialError("Slack credential bundle has an unsupported version")
        required = ("access_token", "refresh_token", "expires_at", "client_id")
        if any(not bundle.get(field) for field in required):
            raise CredentialError("Slack credential bundle is incomplete")
        return bundle

    @staticmethod
    def _needs_refresh(bundle, now, rejected_token):
        if rejected_token is not None:
            return rejected_token == bundle["access_token"]
        return int(bundle["expires_at"]) - now <= REFRESH_WINDOW_SECONDS

    @staticmethod
    def _replacement_bundle(current, response, now):
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RefreshError("Slack rejected the credential refresh")
        # A user-token refresh nests the pair under authed_user, same as the
        # install response; only bot-token refreshes are flat at the top level.
        user = response.get("authed_user")
        source = user if isinstance(user, dict) else response
        access_token = source.get("access_token")
        refresh_token = source.get("refresh_token")
        expires_in = source.get("expires_in")
        if (not isinstance(access_token, str) or not access_token
                or not isinstance(refresh_token, str) or not refresh_token
                or isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float))):
            raise RefreshError("Slack returned an incomplete credential refresh")
        if expires_in <= 0:
            raise RefreshError("Slack returned an invalid access-token expiry")

        refresh_expires_in = source.get(
            "refresh_token_expires_in",
            response.get("refresh_token_expires_in", REFRESH_TOKEN_LIFETIME_SECONDS))
        if (isinstance(refresh_expires_in, bool)
                or not isinstance(refresh_expires_in, (int, float))
                or refresh_expires_in <= 0):
            raise RefreshError("Slack returned an invalid refresh-token expiry")

        replacement = dict(current)
        replacement.update({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": now + int(expires_in),
            "refresh_expires_at": now + int(refresh_expires_in),
        })
        return replacement


def get_access_token(rejected_token=None):
    """Production entry point, installed after adapters are configured."""
    return _default_credentials().get_access_token(rejected_token=rejected_token)


def _default_credentials():
    root = Path(__file__).resolve().parents[2]
    return SlackCredentials(
        store=KeychainCredentialStore(MacOSKeychain()),
        oauth=SlackOAuthTransport(),
        lock=FileLock(root / "state" / "triage" / "slack-oauth.lock"),
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        root = Path(__file__).resolve().parents[2]
        store = KeychainCredentialStore(MacOSKeychain())
        lock = FileLock(root / "state" / "triage" / "slack-oauth.lock")
        if len(argv) == 2 and argv[0] == "install":
            summary = install_oauth_response(
                json.load(sys.stdin), argv[1], store, lock=lock)
        elif argv == ["status"]:
            summary = credential_status(store)
        elif argv == ["refresh-now"]:
            summary = SlackCredentials(
                store=store, oauth=SlackOAuthTransport(), lock=lock).refresh_now()
        else:
            print("usage: slack_credentials.py <install CLIENT_ID|status|refresh-now>",
                  file=sys.stderr)
            return 3
    except TransientCredentialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except AuthenticationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (CredentialError, RefreshError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
