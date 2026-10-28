#!/usr/bin/env python3
"""Read-only Notion REST bridge. Usage: notion-call.py METHOD /v1/path [BODY_JSON].

POST is allowed only for data-source queries. No remote writes are exposed.
Exit codes: 0 success, 1 credentials/access, 2 malformed/upstream failure,
3 invalid arguments, 4 transient, 5 resource/query unavailable (400/404). Tokens stay in process memory.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

VERSION = "2026-03-11"


class CredentialError(Exception):
    pass


class TransientCredentialError(Exception):
    pass


def token():
    value = os.environ.get("NOTION_TOKEN", "").strip()
    if value:
        return value
    configured = os.environ.get("NOTION_KEYCHAIN_SERVICE")
    services = [configured] if configured else ["sidequestor-notion", "sidequestor-notion-test"]
    for service in services:
        args = ["security", "find-generic-password", "-s", service,
                "-a", os.environ.get("NOTION_KEYCHAIN_ACCOUNT", "sidequestor"), "-w"]
        if os.environ.get("NOTION_KEYCHAIN_PATH"):
            args.append(os.path.expanduser(os.environ["NOTION_KEYCHAIN_PATH"]))
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise TransientCredentialError("Notion Keychain lookup timed out") from exc
        except OSError as exc:
            raise CredentialError("set NOTION_TOKEN or configure macOS Keychain") from exc
        if proc.returncode == 44:
            continue
        if proc.returncode:
            # Only proven lock/interaction failures are transient. Unknown failures
            # use normal checker backoff, eventually surfacing for operator repair.
            if proc.returncode == 36 or "-25308" in (proc.stderr or ""):
                raise TransientCredentialError("Notion Keychain unavailable or locked")
            raise RuntimeError("Notion Keychain read failed")
        if not proc.stdout.strip():
            raise CredentialError("Notion token is empty")
        return proc.stdout.strip()
    raise CredentialError("Notion Keychain item not found; check service/account/path")



class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(method, path, body=None):
    # Only fixed Notion API paths; never allow watch input to change host or headers.
    uuid = r"[0-9a-fA-F-]{32,36}"
    allowed = (method == "GET" and re.fullmatch(
        rf"/v1/(?:pages/{uuid}(?:/properties/[^/?#]+)?|databases/{uuid}|data_sources/{uuid}|blocks/{uuid}(?:/children)?|comments)(?:\?[^#\s]*)?", path))
    allowed = allowed or (method == "POST" and re.fullmatch(rf"/v1/data_sources/{uuid}/query", path))
    if not allowed or (method == "GET" and body is not None):
        raise ValueError("only Notion read endpoints and data-source queries are allowed")
    if body is not None and not isinstance(body, dict):
        raise ValueError("body must be an object")
    req = urllib.request.Request("https://api.notion.com" + path,
        data=None if body is None else json.dumps(body).encode(), method=method,
        headers={"Authorization": "Bearer " + token(), "Notion-Version": VERSION,
                 "Content-Type": "application/json"})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
        try:
            data = json.load(response)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("invalid Notion JSON response") from exc
    if not isinstance(data, dict) or data.get("object") == "error":
        raise RuntimeError("invalid Notion response")
    return data


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) not in (2, 3):
            raise ValueError("usage: notion-call.py METHOD /v1/path [BODY_JSON]")
        value = call(args[0], args[1], json.loads(args[2]) if len(args) == 3 else None)
    except CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TransientCredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except urllib.error.HTTPError as exc:
        # Deliberately omit remote bodies and headers: no credential or content in errors.
        retry = exc.headers.get("Retry-After", "") if exc.headers else ""
        retry = int(retry) if str(retry).isdigit() else 60
        print(f"Notion HTTP {exc.code}" + (f" retry_after={retry}" if exc.code in (429, 529) else "") + ("; check page access and capabilities" if exc.code in (403, 404) else ""), file=sys.stderr)
        exc.close()
        if exc.code in (408, 409, 429) or exc.code >= 500:
            return 4
        if exc.code in (400, 404):
            return 5
        return 1 if exc.code in (401, 403) else 2
    except (TimeoutError, OSError, urllib.error.URLError):
        print("Notion network/timeout failure", file=sys.stderr)
        return 4
    except ValueError:
        print("invalid Notion arguments or JSON response", file=sys.stderr)
        return 3
    except Exception:
        print("invalid Notion response", file=sys.stderr)
        return 2
    print(json.dumps(value, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
