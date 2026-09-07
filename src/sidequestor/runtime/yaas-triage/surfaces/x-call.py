#!/usr/bin/env python3
"""Call one X API endpoint with a user OAuth token."""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from slack_credentials import CredentialError, TransientCredentialError
from x_credentials import get_access_token


OK, AUTH, ERROR, BAD_ARGS, TRANSIENT = 0, 1, 2, 3, 4
BASE_URL = "https://api.x.com"
METHODS = {"GET", "POST", "PUT", "DELETE"}


def call(method, path, query=None, body=None, credential_id="default"):
    method = method.upper()
    if method not in METHODS:
        raise ValueError(f"unsupported method: {method}")
    if not path.startswith("/2/"):
        raise ValueError("X path must start with /2/")
    if query is None:
        query = {}
    if not isinstance(query, dict):
        raise ValueError("query must be an object")
    if body is not None and not isinstance(body, dict):
        raise ValueError("body must be an object")

    encoded_query = urllib.parse.urlencode(query, doseq=True)
    url = f"{BASE_URL}{path}"
    if encoded_query:
        url = f"{url}?{encoded_query}"
    headers = {
        "Authorization": f"Bearer {get_access_token(credential_id)}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode()
    return json.loads(raw) if raw else {}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (4, 5):
        print(
            "usage: x-call.py METHOD /2/path QUERY_JSON [BODY_JSON] user[:CREDENTIAL_ID]",
            file=sys.stderr,
        )
        return BAD_ARGS
    method, path, query_json = argv[:3]
    body_json = None if len(argv) == 4 else argv[3]
    auth_spec = argv[-1]
    try:
        auth_mode, _, credential_id = auth_spec.partition(":")
        if auth_mode != "user":
            raise ValueError("auth mode must be user")
        query = json.loads(query_json)
        body = None if body_json is None else json.loads(body_json)
        result = call(method, path, query, body, credential_id or "default")
    except TransientCredentialError as exc:
        # Ordered before CredentialError, its base class: a locked or timing-out Keychain is
        # transient machine state, and returning AUTH would park the watch as misconfigured.
        # See the matching note in telegram-call.py for why client.classify_credential_exception
        # is not reused verbatim.
        print(f"ERROR: {exc}", file=sys.stderr)
        return TRANSIENT
    except CredentialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return AUTH
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:500]
        except Exception:
            detail = ""
        if exc.code in (401, 403):
            code = AUTH
        elif exc.code == 400:
            code = BAD_ARGS
        elif exc.code == 429 or exc.code >= 500:
            code = TRANSIENT
        else:
            code = ERROR
        print(f"ERROR: X HTTP {exc.code}: {detail}", file=sys.stderr)
        return code
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        print(f"ERROR: X transport failed: {type(exc).__name__}", file=sys.stderr)
        return TRANSIENT
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return BAD_ARGS
    print(json.dumps(result, separators=(",", ":")))
    return OK


if __name__ == "__main__":
    sys.exit(main())
