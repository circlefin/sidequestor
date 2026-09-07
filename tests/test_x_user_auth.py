from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SURFACES = ROOT / "src/sidequestor/runtime/yaas-triage/surfaces"


def load(name, path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class FakeStore:
    def __init__(self, value=None):
        self.value = value

    def load(self):
        return self.value

    def save(self, value):
        self.value = value


class XUserAuthTests(unittest.TestCase):
    def setUp(self):
        self.module = load("x_credentials_user_test", SURFACES / "x_credentials.py")

    def test_authorize_stores_a_user_bundle_without_pkce_secrets(self):
        store = FakeStore()
        token = {
            "token_type": "bearer",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 7200,
            "scope": "tweet.read tweet.write users.read offline.access",
        }
        with mock.patch.object(self.module, "_store", return_value=store), \
                mock.patch.object(self.module, "_authorization_code", return_value=("code", "verifier")), \
                mock.patch.object(self.module, "_exchange_code", return_value=token), \
                mock.patch.object(self.module, "_identity", return_value={"id": "42", "username": "tester"}), \
                mock.patch.object(self.module.time, "time", return_value=1000):
            summary = self.module.authorize("client", "work")

        self.assertEqual({"credential_id": "work", "mode": "user", "user_id": "42",
                          "username": "tester"}, summary)
        self.assertEqual("user", store.value["mode"])
        self.assertEqual("client", store.value["client_id"])
        self.assertEqual("access", store.value["access_token"])
        self.assertEqual("refresh", store.value["refresh_token"])
        self.assertEqual(8200, store.value["expires_at"])
        self.assertNotIn("code_verifier", store.value)

    def test_access_token_refreshes_and_persists_the_rotated_bundle(self):
        store = FakeStore({
            "version": 2,
            "mode": "user",
            "client_id": "client",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 100,
            "scope": "tweet.read offline.access",
            "user_id": "42",
            "username": "tester",
        })
        replacement = {
            "token_type": "bearer",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
            "scope": "tweet.read offline.access",
        }
        with mock.patch.object(self.module, "_store", return_value=store), \
                mock.patch.object(self.module, "_refresh", return_value=replacement), \
                mock.patch.object(self.module.time, "time", return_value=1000):
            self.assertEqual("new-access", self.module.get_access_token("work"))

        self.assertEqual("new-access", store.value["access_token"])
        self.assertEqual("new-refresh", store.value["refresh_token"])
        self.assertEqual(8200, store.value["expires_at"])

    def test_status_rejects_legacy_app_only_credentials(self):
        store = FakeStore({"version": 1, "mode": "app", "access_token": "legacy"})
        with mock.patch.object(self.module, "_store", return_value=store), \
                self.assertRaisesRegex(Exception, "app-only credential is no longer supported"):
            self.module.load_bundle("default")


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = json.dumps(body).encode()
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class XUserSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.module = load("x_call_user_test", SURFACES / "x-call.py")

    def test_user_surface_posts_json_as_the_authorized_user(self):
        response = FakeResponse({"data": {"id": "99", "text": "hello"}}, status=201)
        with mock.patch.object(self.module, "get_access_token", return_value="user-token"), \
                mock.patch.object(self.module.urllib.request, "urlopen", return_value=response) as urlopen:
            value = self.module.call("POST", "/2/tweets", {}, {"text": "hello"}, "work")

        request = urlopen.call_args.args[0]
        self.assertEqual("POST", request.method)
        self.assertEqual("Bearer user-token", request.headers["Authorization"])
        self.assertEqual({"text": "hello"}, json.loads(request.data))
        self.assertEqual("99", value["data"]["id"])

    def test_surface_rejects_app_auth_mode(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.module.main(["GET", "/2/users/me", "{}", "app:default"])
        self.assertEqual(3, code)
        self.assertIn("auth mode must be user", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
