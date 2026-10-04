#!/usr/bin/env python3
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Own the lifecycle of the Slack credential used by deterministic checkers."""

import fcntl
import hashlib
import json
import os
import shutil
import stat
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
HELPER_VERSION = 1
HELPER_NAME = f"sidequestor-keychain-helper-v{HELPER_VERSION}"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
HTTP_TIMEOUT_SECONDS = 30
KEYCHAIN_TIMEOUT_SECONDS = 120


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


class HelperMigrationRequired(HelperUnavailableError):
    """The stable helper must be authorized in a foreground repair."""


def _state_root():
    """Find legacy per-workspace state during the one-time helper migration."""
    for name in ("SIDEQUESTOR_WORKSPACE", "YAAS_WORKSPACE", "REPO_ROOT"):
        value = os.environ.get(name)
        if value:
            candidate = Path(value).expanduser()
            if candidate.is_dir():
                return candidate.resolve()
    return Path(__file__).resolve().parents[2]


def _config_home():
    configured = (
        os.environ.get("SIDEQUESTOR_CONFIG_HOME")
        or os.environ.get("YAAS_CONFIG_HOME")
    )
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config"


def _credential_home():
    return _config_home() / "yaas" / "credentials"


def _ensure_credential_directories():
    """Create each app-owned directory at its final restrictive mode."""
    app_home = _config_home() / "yaas"
    app_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(app_home, 0o700)
    credential_home = _credential_home()
    credential_home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(credential_home, 0o700)
    return credential_home


def _keychain_helper_path():
    """Return one immutable, versioned helper path shared by this macOS user."""
    return _credential_home() / "bin" / HELPER_NAME


def _legacy_helper_candidates():
    """Helpers older releases may already have authorized in Keychain."""
    candidates = [
        Path(__file__).resolve().parents[2] / "state" / "bin" / "yaas-keychain-helper",
        _state_root() / "state" / "bin" / "yaas-keychain-helper",
    ]
    registry = _config_home() / "yaas" / "instances.json"
    try:
        rows = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        rows = []
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        root = Path(row["path"]).expanduser()
        if not (root / ".yaas" / "instance.json").is_file():
            continue
        candidates.extend([
            root / "state" / "bin" / "yaas-keychain-helper",
            root / ".local" / "yaas-package" / "src" / "sidequestor"
            / "runtime" / "state" / "bin" / "yaas-keychain-helper",
        ])
        manifest = root / ".yaas" / "launchd" / "production.json"
        try:
            production = json.loads(manifest.read_text(encoding="utf-8"))
            python = Path(production["python"]).expanduser()
        except (KeyError, OSError, TypeError, ValueError):
            python = None
        venv_roots = [root / ".local" / "yaas-package" / ".venv"]
        if python is not None and python.parent.name == "bin":
            venv_roots.insert(0, python.parent.parent)
        for venv in venv_roots:
            for package in ("sidequestor", "yaas_triage"):
                candidates.extend(
                    venv.glob(
                        f"lib/python*/site-packages/{package}/runtime/state/bin/"
                        "yaas-keychain-helper"
                    )
                )
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _helper_marker_path():
    return _credential_home() / f"{HELPER_NAME}.ready.json"


def _helper_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_legacy_helper(path):
    try:
        metadata = Path(path).lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and os.access(path, os.X_OK)
    )


def _install_global_helper(source, helper):
    """Install once under a global lock; never replace an existing identity."""
    helper = Path(helper)
    _ensure_credential_directories()
    helper.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(helper.parent, 0o700)
    lock = FileLock(helper.parent / f".{HELPER_NAME}.install.lock")
    with lock:
        if helper.exists():
            if not _trusted_legacy_helper(helper):
                raise HelperUnavailableError(
                    f"Keychain helper exists but is not a secure executable: {helper}; "
                    "refusing to replace it automatically"
                )
            if _helper_marker_path().exists() and not _helper_ready(helper):
                raise HelperUnavailableError(
                    f"Keychain helper identity changed unexpectedly: {helper}; "
                    "refusing to replace or execute it automatically"
                )
            return None

        # Candidate order begins with the exact package/workspace selection used by
        # older releases. Registered-workspace paths are fallbacks before compilation;
        # filesystem atime is deliberately not used as an authorization signal.
        migrated_from = next(
            (path for path in _legacy_helper_candidates() if _trusted_legacy_helper(path)),
            None,
        )
        temporary = helper.with_name(f".{helper.name}.{os.getpid()}.tmp")
        try:
            if migrated_from is not None:
                shutil.copyfile(migrated_from, temporary, follow_symlinks=False)
                if _helper_digest(temporary) != _helper_digest(migrated_from):
                    raise HelperUnavailableError(
                        "copied Keychain helper failed its integrity check")
            else:
                result = subprocess.run(
                    ["/usr/bin/clang", str(source), "-framework", "Security",
                     "-framework", "CoreFoundation", "-o", str(temporary)],
                    capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    raise HelperUnavailableError(
                        "could not build the Slack Keychain helper")
            temporary.chmod(0o700)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, helper)
            directory_fd = os.open(helper.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HelperUnavailableError(
                "could not install the Slack Keychain helper") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return migrated_from


def _helper_ready(helper):
    try:
        payload = json.loads(_helper_marker_path().read_text(encoding="utf-8"))
        return (payload.get("schema") == 1
                and payload.get("helper_sha256") == _helper_digest(helper))
    except (OSError, TypeError, ValueError):
        return False


def _mark_helper_ready(helper, credential_present):
    marker = _helper_marker_path()
    _ensure_credential_directories()
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": 1,
                "helper_sha256": _helper_digest(helper),
                "credential_present": bool(credential_present),
            }, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


class MacOSKeychain:
    """Use a stable local helper so Keychain values never enter argv."""

    def __init__(self, allow_unready=False):
        if sys_platform() != "darwin":
            raise CredentialError("Slack Keychain storage requires macOS")
        self.source = Path(__file__).resolve().with_name("keychain-helper.c")
        self.helper = _keychain_helper_path()
        self.allow_unready = allow_unready

    def _ensure_helper(self):
        _install_global_helper(self.source, self.helper)

    def _require_ready_helper(self):
        self._ensure_helper()
        if not self.allow_unready and not _helper_ready(self.helper):
            raise HelperMigrationRequired(
                "Keychain helper authorization is incomplete. Run "
                "`sq credentials repair-keychain` in a terminal."
            )

    def read(self, service, account):
        if service == LEGACY_SERVICE:
            return self._read_legacy(service, account)
        self._require_ready_helper()
        try:
            result = subprocess.run(
                [str(self.helper), "read", service, account],
                capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_SECONDS)
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
                 "-a", account, "-w"], capture_output=True, text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS)
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
        self._require_ready_helper()
        try:
            result = subprocess.run(
                [str(self.helper), "write", service, account],
                input=value, capture_output=True, text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS)
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
        if self.path.parent == _credential_home():
            _ensure_credential_directories()
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self.path, 0o600)
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
        except HelperMigrationRequired:
            raise
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
    return SlackCredentials(
        store=KeychainCredentialStore(MacOSKeychain()),
        oauth=SlackOAuthTransport(),
        lock=FileLock(_credential_home() / "slack-oauth.lock"),
    )


def keychain_helper_status():
    """Return helper migration metadata without reading any Keychain value."""
    helper = _keychain_helper_path()
    return {
        "version": HELPER_VERSION,
        "path": str(helper),
        "installed": helper.is_file() and os.access(helper, os.X_OK),
        "ready": helper.is_file() and _helper_ready(helper),
    }


def repair_keychain_helper(require_interactive=True):
    """Install the global helper and verify its Keychain trust without rewriting tokens."""
    keychain = MacOSKeychain(allow_unready=True)
    _install_global_helper(keychain.source, keychain.helper)
    # Refresh writes and these two trust-verification reads operate on the same global
    # Keychain item. Serialize both so a legitimate rotation cannot look like corruption.
    with FileLock(_credential_home() / "slack-oauth.lock"):
        with FileLock(_credential_home() / f".{HELPER_NAME}.repair.lock"):
            return _verify_keychain_helper(
                keychain, require_interactive=require_interactive)


def _verify_keychain_helper(keychain, require_interactive=True):
    if _helper_ready(keychain.helper):
        return keychain_helper_status()

    if require_interactive and not sys.stderr.isatty():
        raise HelperUnavailableError(
            "Keychain helper migration needs an interactive terminal. "
            "Run `sq credentials repair-keychain` in a terminal; the selected instance "
            "will remain stopped until the migration succeeds."
        )

    print(
        "Sidequestor is authorizing its stable Keychain helper. "
        "If macOS prompts, choose Always Allow once.",
        file=sys.stderr,
    )

    first = keychain.read(BUNDLE_SERVICE, KEYCHAIN_ACCOUNT)
    if first is None:
        _mark_helper_ready(keychain.helper, credential_present=False)
        return keychain_helper_status()

    # A second successful read catches a one-time `Allow` response before background
    # processes resume. Credential material stays in memory and is never printed.
    second = keychain.read(BUNDLE_SERVICE, KEYCHAIN_ACCOUNT)
    if second != first:
        raise CredentialError("Slack credential changed while Keychain trust was verified")
    _mark_helper_ready(keychain.helper, credential_present=True)
    return keychain_helper_status()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = False
    try:
        installing = len(argv) == 2 and argv[0] == "install"
        store = KeychainCredentialStore(MacOSKeychain(allow_unready=installing))
        lock = FileLock(_credential_home() / "slack-oauth.lock")
        if installing:
            summary = install_oauth_response(
                json.load(sys.stdin), argv[1], store, lock=lock)
            repair_keychain_helper(require_interactive=False)
        elif argv == ["status"]:
            summary = credential_status(store)
        elif argv == ["refresh-now"]:
            summary = SlackCredentials(
                store=store, oauth=SlackOAuthTransport(), lock=lock).refresh_now()
        elif argv == ["helper-status"]:
            summary = keychain_helper_status()
        elif argv in (["repair-keychain"], ["repair-keychain", "--quiet"]):
            quiet = "--quiet" in argv
            summary = repair_keychain_helper()
        else:
            print("usage: slack_credentials.py "
                  "<install CLIENT_ID|status|refresh-now|helper-status|repair-keychain>",
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
    if not quiet:
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
