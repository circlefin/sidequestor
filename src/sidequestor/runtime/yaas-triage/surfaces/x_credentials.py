#!/usr/bin/env python3
"""Store an X application bearer token in macOS Keychain."""

import json
import getpass
import sys

from credential_store import CredentialStore
from slack_credentials import CredentialError


SERVICE = "sidequestor-x-token"


def _store(credential_id):
    return CredentialStore(SERVICE, credential_id)


def load_bundle(credential_id="default", store=None):
    bundle = (store or _store(credential_id)).load()
    if not bundle:
        raise CredentialError(
            f"X credential {credential_id!r} is missing; install an app bearer token")
    if bundle.get("mode") == "app" and bundle.get("access_token"):
        return bundle
    raise CredentialError(f"X credential {credential_id!r} is incomplete")


def get_access_token(credential_id="default"):
    return load_bundle(credential_id)["access_token"]


def _install(credential_id):
    if sys.stdin.isatty():
        value = {"access_token": getpass.getpass("X bearer token: ").strip()}
    else:
        raw = sys.stdin.read()
        if not raw.strip():
            raise CredentialError("credential JSON is required on stdin")
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise CredentialError("credential JSON must be an object")
    value = dict(value)
    value["version"] = 1
    value["mode"] = "app"
    if not value.get("access_token"):
        raise CredentialError("app credential requires access_token")
    _store(credential_id).save(value)
    return {"credential_id": credential_id, "mode": "app"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "install-app" and len(argv) <= 2:
            credential_id = argv[1] if len(argv) == 2 else "default"
            summary = _install(credential_id)
        elif argv and argv[0] == "status" and len(argv) <= 2:
            credential_id = argv[1] if len(argv) == 2 else "default"
            bundle = load_bundle(credential_id)
            summary = {"credential_id": credential_id, "configured": True,
                       "mode": bundle["mode"]}
        else:
            print("usage: x_credentials.py install-app [CREDENTIAL_ID] < bundle.json\n"
                  "       x_credentials.py status [CREDENTIAL_ID]", file=sys.stderr)
            return 3
    except (CredentialError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
