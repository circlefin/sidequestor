#!/usr/bin/env python3
"""Authorize and maintain an X OAuth 2.0 user credential in macOS Keychain."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from credential_store import CredentialStore, account_name
from slack_credentials import AuthenticationError, CredentialError, TransientCredentialError


SERVICE = "sidequestor-x-token"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
ME_URL = "https://api.x.com/2/users/me?user.fields=id,username,name"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
REFRESH_WINDOW_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 30
AUTH_TIMEOUT_SECONDS = 300
DEFAULT_SCOPES = (
    "tweet.read", "tweet.write", "users.read", "offline.access",
    "dm.read", "dm.write", "like.read", "like.write",
    "follows.read", "follows.write", "bookmark.read", "bookmark.write",
    "mute.read", "mute.write", "block.read", "block.write", "media.write",
)


def _store(credential_id):
    return CredentialStore(SERVICE, credential_id)


def _request_json(request, timeout=HTTP_TIMEOUT_SECONDS):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:500]
        except Exception:
            detail = ""
        if exc.code in (400, 401, 403):
            raise AuthenticationError(f"X authorization returned HTTP {exc.code}: {detail}") from exc
        if exc.code == 429 or exc.code >= 500:
            raise TransientCredentialError(f"X authorization returned HTTP {exc.code}") from exc
        raise CredentialError(f"X authorization returned HTTP {exc.code}: {detail}") from exc
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        raise TransientCredentialError("X authorization transport failed") from exc
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CredentialError("X authorization returned malformed JSON") from exc
    if not 200 <= status < 300 or not isinstance(value, dict):
        raise CredentialError("X authorization returned an invalid response")
    return value


def _form_request(url, values):
    return urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )


def _token_bundle(token, *, client_id, identity, now=None):
    if not token.get("access_token") or not token.get("refresh_token"):
        raise CredentialError("X did not return both access and refresh tokens")
    try:
        expires_in = int(token.get("expires_in") or 0)
    except (TypeError, ValueError) as exc:
        raise CredentialError("X returned an invalid token expiry") from exc
    if expires_in <= 0:
        raise CredentialError("X did not return a usable token expiry")
    current = int(time.time() if now is None else now)
    return {
        "version": 2,
        "mode": "user",
        "client_id": str(client_id),
        "access_token": str(token["access_token"]),
        "refresh_token": str(token["refresh_token"]),
        "expires_at": current + expires_in,
        "scope": str(token.get("scope") or ""),
        "token_type": str(token.get("token_type") or "bearer"),
        "user_id": str(identity.get("id") or ""),
        "username": str(identity.get("username") or ""),
    }


def load_bundle(credential_id="default", store=None):
    bundle = (store or _store(credential_id)).load()
    if not bundle:
        raise CredentialError(
            f"X credential {credential_id!r} is missing; run x-auth authorize CLIENT_ID")
    if bundle.get("mode") == "app":
        raise CredentialError(
            "app-only credential is no longer supported; run x-auth authorize CLIENT_ID")
    required = ("client_id", "access_token", "refresh_token", "expires_at", "user_id")
    if bundle.get("mode") != "user" or any(not bundle.get(key) for key in required):
        raise CredentialError(f"X user credential {credential_id!r} is incomplete")
    return bundle


def _exchange_code(client_id, code, verifier, redirect_uri):
    return _request_json(_form_request(TOKEN_URL, {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }))


def _refresh(client_id, refresh_token):
    return _request_json(_form_request(TOKEN_URL, {
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "client_id": client_id,
    }))


def _identity(access_token):
    request = urllib.request.Request(
        ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    value = _request_json(request)
    identity = value.get("data")
    if not isinstance(identity, dict) or not identity.get("id"):
        raise CredentialError("X user lookup did not return an account identity")
    return identity


def _authorization_code(client_id, redirect_uri, scopes=DEFAULT_SCOPES):
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise CredentialError("X redirect URI must use a local http callback")
    if not parsed.port:
        raise CredentialError("X redirect URI must include a callback port")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    expected_state = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": expected_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            callback = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(callback.query)
            if callback.path != parsed.path:
                status, body = 404, b"Not found."
            elif query.get("state", [""])[0] != expected_state:
                result["error"] = "X callback state did not match"
                status, body = 400, b"Authorization failed. Return to Sidequestor."
            elif query.get("error"):
                result["error"] = query.get("error_description", query["error"])[0]
                status, body = 400, b"Authorization was denied. Return to Sidequestor."
            elif query.get("code"):
                result["code"] = query["code"][0]
                status, body = 200, b"X authorization complete. You can close this tab."
            else:
                result["error"] = "X callback did not include an authorization code"
                status, body = 400, b"Authorization failed. Return to Sidequestor."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer((parsed.hostname, parsed.port), CallbackHandler)
    server.timeout = 1
    print(f"Open this URL to authorize Sidequestor:\n{authorize_url}")
    try:
        webbrowser.open(authorize_url)
        deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if result.get("error"):
        raise AuthenticationError(str(result["error"]))
    if not result.get("code"):
        raise TransientCredentialError("X authorization callback timed out")
    return result["code"], verifier


def authorize(client_id, credential_id="default", redirect_uri=None):
    redirect = redirect_uri or os.environ.get(
        "SIDEQUESTOR_X_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    code, verifier = _authorization_code(client_id, redirect)
    token = _exchange_code(client_id, code, verifier, redirect)
    identity = _identity(token["access_token"])
    bundle = _token_bundle(token, client_id=client_id, identity=identity)
    _store(credential_id).save(bundle)
    return {
        "credential_id": credential_id,
        "mode": "user",
        "user_id": bundle["user_id"],
        "username": bundle["username"],
    }


def get_access_token(credential_id="default"):
    store = _store(credential_id)
    account = getattr(store, "account", account_name(credential_id))
    lock_path = Path("/tmp") / f"sidequestor-x-refresh-{os.getuid()}-{account}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            bundle = load_bundle(credential_id, store)
            if float(bundle["expires_at"]) > time.time() + REFRESH_WINDOW_SECONDS:
                return bundle["access_token"]
            token = _refresh(bundle["client_id"], bundle["refresh_token"])
            identity = {"id": bundle["user_id"], "username": bundle.get("username", "")}
            replacement = _token_bundle(token, client_id=bundle["client_id"], identity=identity)
            store.save(replacement)
            return replacement["access_token"]
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def user_identity(credential_id="default"):
    bundle = load_bundle(credential_id)
    return {"id": bundle["user_id"], "username": bundle.get("username", "")}


def revoke(credential_id="default"):
    store = _store(credential_id)
    bundle = load_bundle(credential_id, store)
    _request_json(_form_request(REVOKE_URL, {
        "token": bundle["refresh_token"],
        "client_id": bundle["client_id"],
    }))
    store.save({"version": 2, "mode": "revoked"})
    return {"credential_id": credential_id, "revoked": True}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "authorize" and 2 <= len(argv) <= 3:
            summary = authorize(argv[1], argv[2] if len(argv) == 3 else "default")
        elif argv and argv[0] == "status" and len(argv) <= 2:
            credential_id = argv[1] if len(argv) == 2 else "default"
            bundle = load_bundle(credential_id)
            summary = {
                "credential_id": credential_id,
                "configured": True,
                "mode": "user",
                "user_id": bundle["user_id"],
                "username": bundle.get("username", ""),
                "scope": bundle.get("scope", ""),
                "expires_at": bundle["expires_at"],
            }
        elif argv and argv[0] == "revoke" and len(argv) <= 2:
            summary = revoke(argv[1] if len(argv) == 2 else "default")
        else:
            print("usage: x_credentials.py authorize CLIENT_ID [CREDENTIAL_ID]\n"
                  "       x_credentials.py status [CREDENTIAL_ID]\n"
                  "       x_credentials.py revoke [CREDENTIAL_ID]", file=sys.stderr)
            return 3
    except (CredentialError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
