#!/usr/bin/env python3
"""Call one X API endpoint with a Keychain credential and stable exit taxonomy."""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from slack_credentials import CredentialError, TransientCredentialError
from x_credentials import get_access_token


OK, AUTH, ERROR, BAD_ARGS, TRANSIENT = 0, 1, 2, 3, 4
BASE_URL = "https://api.x.com"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 4 or argv[0] != "GET" or not argv[1].startswith("/2/"):
        print("usage: x-call.py GET /2/path QUERY_JSON app[:CREDENTIAL_ID]", file=sys.stderr)
        return BAD_ARGS
    _method, path, query_json, auth_spec = argv
    try:
        query = json.loads(query_json)
        if not isinstance(query, dict):
            raise ValueError("query must be an object")
        auth_mode, _, credential_id = auth_spec.partition(":")
        if auth_mode != "app":
            raise ValueError("auth mode must be app")
        token = get_access_token(credential_id or "default")
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
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
    print(body)
    return OK


if __name__ == "__main__":
    sys.exit(main())
