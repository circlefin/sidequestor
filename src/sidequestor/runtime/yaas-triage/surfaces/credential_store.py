"""Small namespaced credential bundles backed by Sidequestor's Keychain helper."""

import json
import re

from slack_credentials import CredentialError, MacOSKeychain


_CREDENTIAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def account_name(credential_id):
    value = str(credential_id or "default")
    if not _CREDENTIAL_ID.fullmatch(value):
        raise CredentialError(
            "credential_id must contain only letters, digits, dot, underscore, or hyphen")
    return f"yaas:{value}"


class CredentialStore:
    def __init__(self, service, credential_id="default", keychain=None):
        self.service = service
        self.account = account_name(credential_id)
        self.keychain = keychain or MacOSKeychain()

    def load(self):
        raw = self.keychain.read(self.service, self.account)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CredentialError(f"{self.service} credential bundle is malformed") from exc
        if not isinstance(value, dict):
            raise CredentialError(f"{self.service} credential bundle is malformed")
        return value

    def save(self, value):
        if not isinstance(value, dict):
            raise CredentialError("credential bundle must be an object")
        self.keychain.write(
            self.service, self.account,
            json.dumps(value, separators=(",", ":"), sort_keys=True),
        )
