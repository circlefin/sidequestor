#!/bin/bash
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

set -u

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"

python3 - "$ROOT" <<'PYEOF'
import importlib.util
import json
import sys


def load_module(root):
    path = f"{root}/yaas-triage/surfaces/slack_credentials.py"
    spec = importlib.util.spec_from_file_location("slack_credentials", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryStore:
    def __init__(self, bundle):
        self.bundle = bundle

    def load(self):
        return json.loads(json.dumps(self.bundle))

    def save(self, bundle):
        self.bundle = json.loads(json.dumps(bundle))


class FakeOAuth:
    def __init__(self):
        self.calls = []

    def refresh(self, client_id, refresh_token):
        self.calls.append((client_id, refresh_token))
        return {
            "ok": True,
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 43200,
        }


class EnterAction:
    def __init__(self, action):
        self.action = action

    def __enter__(self):
        self.action()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


credentials = load_module(sys.argv[1])
now = 1_787_240_000

fresh_store = MemoryStore({
    "version": 1,
    "access_token": "fresh-access",
    "refresh_token": "fresh-refresh",
    "expires_at": now + 301,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "client-123",
    "user_id": "U123",
    "team_id": "T123",
})
fresh_oauth = FakeOAuth()
fresh = credentials.SlackCredentials(
    store=fresh_store, oauth=fresh_oauth, clock=lambda: now)
assert fresh.get_access_token() == "fresh-access"
assert fresh_oauth.calls == []
print("  PASS credentials outside the refresh window are returned unchanged")

legacy_store = MemoryStore({"mode": "legacy", "access_token": "xoxp-legacy"})
legacy_oauth = FakeOAuth()
legacy = credentials.SlackCredentials(
    store=legacy_store, oauth=legacy_oauth, clock=lambda: now)
assert legacy.get_access_token() == "xoxp-legacy"
assert legacy_oauth.calls == []
print("  PASS legacy long-lived credentials bypass refresh")

access_only = credentials.SlackCredentials(
    store=MemoryStore({"mode": "access-only", "access_token": "xoxe.xoxp-access"}),
    oauth=FakeOAuth(),
    clock=lambda: now,
)
assert access_only.get_access_token() == "xoxe.xoxp-access"
try:
    access_only.get_access_token(rejected_token="xoxe.xoxp-access")
except credentials.AuthenticationError:
    pass
else:
    raise AssertionError("a rejected access-only token must require reauthorization")
print("  PASS rejected access-only credentials require reauthorization")

store = MemoryStore({
    "version": 1,
    "access_token": "old-access",
    "refresh_token": "old-refresh",
    "expires_at": now + 299,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "client-123",
    "user_id": "U123",
    "team_id": "T123",
})
oauth = FakeOAuth()
manager = credentials.SlackCredentials(store=store, oauth=oauth, clock=lambda: now)

token = manager.get_access_token()

assert token == "new-access", token
assert oauth.calls == [("client-123", "old-refresh")], oauth.calls
assert store.bundle["access_token"] == "new-access"
assert store.bundle["refresh_token"] == "new-refresh"
assert store.bundle["expires_at"] == now + 43200
assert store.bundle["refresh_expires_at"] == now + 2_592_000
print("  PASS expiring credentials refresh and persist one complete generation")

rejected_store = MemoryStore({
    "version": 1,
    "access_token": "rejected-access",
    "refresh_token": "rejected-refresh",
    "expires_at": now + 40_000,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "client-123",
    "user_id": "U123",
    "team_id": "T123",
})
rejected_oauth = FakeOAuth()
rejected = credentials.SlackCredentials(
    store=rejected_store, oauth=rejected_oauth, clock=lambda: now)
assert rejected.get_access_token(rejected_token="rejected-access") == "new-access"
assert rejected_oauth.calls == [("client-123", "rejected-refresh")]
print("  PASS a definitively rejected current token refreshes before expiry")

newer_bundle = {
    "version": 1,
    "access_token": "other-process-access",
    "refresh_token": "other-process-refresh",
    "expires_at": now + 43_200,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "client-123",
    "user_id": "U123",
    "team_id": "T123",
}
race_store = MemoryStore({
    **newer_bundle,
    "access_token": "race-old-access",
    "refresh_token": "race-old-refresh",
})
race_oauth = FakeOAuth()
race = credentials.SlackCredentials(
    store=race_store,
    oauth=race_oauth,
    clock=lambda: now,
    lock=EnterAction(lambda: race_store.save(newer_bundle)),
)
assert race.get_access_token(rejected_token="race-old-access") == "other-process-access"
assert race_oauth.calls == []
print("  PASS a newer token installed under the lock prevents duplicate refresh")

broken_store = MemoryStore({
    "version": 1,
    "access_token": "still-working-access",
    "refresh_token": "still-working-refresh",
    "expires_at": now,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "client-123",
    "user_id": "U123",
    "team_id": "T123",
})
before = json.loads(json.dumps(broken_store.bundle))

class BrokenOAuth:
    def refresh(self, client_id, refresh_token):
        return {"ok": True, "access_token": "partial", "expires_in": 43200}

broken = credentials.SlackCredentials(
    store=broken_store, oauth=BrokenOAuth(), clock=lambda: now)
try:
    broken.get_access_token()
except credentials.RefreshError:
    pass
else:
    raise AssertionError("an incomplete refresh response must fail")
assert broken_store.bundle == before
print("  PASS malformed refresh responses preserve the previous generation")

class InvalidExpiryOAuth:
    def refresh(self, client_id, refresh_token):
        return {
            "ok": True,
            "access_token": "partial-access",
            "refresh_token": "partial-refresh",
            "expires_in": 43200,
            "refresh_token_expires_in": -1,
        }

invalid_expiry_store = MemoryStore(before)
invalid_expiry = credentials.SlackCredentials(
    store=invalid_expiry_store, oauth=InvalidExpiryOAuth(), clock=lambda: now)
try:
    invalid_expiry.get_access_token()
except credentials.RefreshError:
    pass
else:
    raise AssertionError("an invalid refresh-token expiry must fail")
assert invalid_expiry_store.bundle == before
print("  PASS invalid refresh expiry cannot replace the previous generation")

unsupported = credentials.SlackCredentials(
    store=MemoryStore({"version": 2, "access_token": "must-not-leak"}),
    oauth=FakeOAuth(), clock=lambda: now)
try:
    unsupported.get_access_token()
except credentials.CredentialError as exc:
    assert "must-not-leak" not in str(exc)
else:
    raise AssertionError("unsupported bundle versions must fail closed")
print("  PASS unsupported bundle versions fail closed without secret output")

class MemoryKeychain:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def read(self, service, account):
        return self.values.get((service, account))

    def write(self, service, account, value):
        self.writes.append((service, account, value))
        self.values[(service, account)] = value


keychain = MemoryKeychain({
    (credentials.BUNDLE_SERVICE, "yaas"): json.dumps(newer_bundle),
})
keychain_store = credentials.KeychainCredentialStore(keychain)
assert keychain_store.load() == newer_bundle
keychain_store.save({**newer_bundle, "access_token": "saved-access"})
service, account, stored_json = keychain.writes[-1]
assert (service, account) == (credentials.BUNDLE_SERVICE, "yaas")
assert json.loads(stored_json)["access_token"] == "saved-access"
print("  PASS the Keychain store reads and writes one complete JSON bundle")

legacy_keychain = MemoryKeychain({
    (credentials.LEGACY_SERVICE, "yaas"): "xoxp-old-install",
})
assert credentials.KeychainCredentialStore(legacy_keychain).load() == {
    "mode": "legacy", "access_token": "xoxp-old-install"}

access_keychain = MemoryKeychain({
    (credentials.LEGACY_SERVICE, "yaas"): "xoxe.xoxp-access-only",
})
assert credentials.KeychainCredentialStore(access_keychain).load() == {
    "mode": "access-only", "access_token": "xoxe.xoxp-access-only"}
print("  PASS the Keychain store preserves legacy and access-only compatibility")

oauth_requests = []
def successful_request(url, body, timeout):
    oauth_requests.append((url, body, timeout))
    return 200, json.dumps({
        "ok": True,
        "access_token": "transport-access",
        "refresh_token": "transport-refresh",
        "expires_in": 43200,
    })

transport = credentials.SlackOAuthTransport(request=successful_request)
transport_result = transport.refresh("public-client", "secret-refresh")
assert transport_result["access_token"] == "transport-access"
url, body, timeout = oauth_requests[0]
assert url == credentials.SLACK_TOKEN_URL
assert "grant_type=refresh_token" in body
assert "client_id=public-client" in body
assert "refresh_token=secret-refresh" in body
assert "client_secret" not in body
print("  PASS OAuth refresh sends the PKCE grant without a client secret")

def invalid_grant(url, body, timeout):
    return 200, '{"ok":false,"error":"invalid_grant"}'

try:
    credentials.SlackOAuthTransport(request=invalid_grant).refresh(
        "public-client", "must-not-leak")
except credentials.AuthenticationError as exc:
    assert "must-not-leak" not in str(exc)
else:
    raise AssertionError("invalid_grant must require reauthorization")
print("  PASS invalid_grant is a redacted authentication failure")

attempts = []
def one_timeout(url, body, timeout):
    attempts.append(1)
    if len(attempts) == 1:
        raise TimeoutError("ambiguous timeout")
    return successful_request(url, body, timeout)

retry_result = credentials.SlackOAuthTransport(request=one_timeout).refresh(
    "public-client", "retry-refresh")
assert retry_result["access_token"] == "transport-access"
assert len(attempts) == 2
print("  PASS one ambiguous refresh timeout receives exactly one immediate retry")

def rate_limited(url, body, timeout):
    return 429, '{"ok":false,"error":"ratelimited"}'

try:
    credentials.SlackOAuthTransport(request=rate_limited).refresh(
        "public-client", "must-not-leak")
except credentials.TransientCredentialError as exc:
    assert "must-not-leak" not in str(exc)
else:
    raise AssertionError("rate limiting must be transient")
print("  PASS refresh rate limiting is transient and redacted")

install_response = {
    "ok": True,
    "authed_user": {
        "id": "U-INSTALL",
        "access_token": "install-access",
        "refresh_token": "install-refresh",
        "expires_in": 43200,
    },
    "team": {"id": "T-INSTALL", "name": "Install Team"},
}
install_bundle = credentials.bundle_from_oauth_response(
    install_response, "install-client", now=now)
assert install_bundle == {
    "version": 1,
    "access_token": "install-access",
    "refresh_token": "install-refresh",
    "expires_at": now + 43200,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "install-client",
    "user_id": "U-INSTALL",
    "team_id": "T-INSTALL",
}
print("  PASS setup converts the complete OAuth response into a credential bundle")

incomplete_install = json.loads(json.dumps(install_response))
del incomplete_install["authed_user"]["refresh_token"]
try:
    credentials.bundle_from_oauth_response(incomplete_install, "install-client", now=now)
except credentials.CredentialError:
    pass
else:
    raise AssertionError("setup must reject a rotating response without a refresh token")
print("  PASS setup rejects rotating credentials that cannot be refreshed")

installed_store = MemoryStore({"sentinel": "working-credential"})
installed = credentials.install_oauth_response(
    install_response, "install-client", installed_store, now=now)
assert installed == {
    "mode": "rotating",
    "user_id": "U-INSTALL",
    "team_id": "T-INSTALL",
    "expires_at": now + 43200,
}
assert installed_store.bundle == install_bundle
print("  PASS setup installation stores one validated generation")

preserved_store = MemoryStore({"sentinel": "working-credential"})
try:
    credentials.install_oauth_response(
        incomplete_install, "install-client", preserved_store, now=now)
except credentials.CredentialError:
    pass
else:
    raise AssertionError("incomplete installation must fail")
assert preserved_store.bundle == {"sentinel": "working-credential"}
print("  PASS failed setup validation preserves the working credential")

manual_store = MemoryStore({
    "version": 1,
    "access_token": "manual-old-access",
    "refresh_token": "manual-old-refresh",
    "expires_at": now + 40_000,
    "refresh_expires_at": now + 2_592_000,
    "client_id": "client-123",
    "user_id": "U123",
    "team_id": "T123",
})
manual_oauth = FakeOAuth()
manual = credentials.SlackCredentials(
    store=manual_store, oauth=manual_oauth, clock=lambda: now)
manual_summary = manual.refresh_now()
assert manual_summary == {
    "mode": "rotating",
    "user_id": "U123",
    "team_id": "T123",
    "expires_at": now + 43200,
}
assert manual_oauth.calls == [("client-123", "manual-old-refresh")]
assert "access_token" not in manual_summary and "refresh_token" not in manual_summary
print("  PASS manual refresh rotates immediately and returns only redacted status")

status = credentials.credential_status(manual_store, now=now)
assert status == {
    "mode": "rotating",
    "complete": True,
    "access_expires_in": 43200,
    "refresh_expires_in": 2_592_000,
}
assert "access_token" not in status and "refresh_token" not in status
print("  PASS credential status reports expiry without token material")
PYEOF
