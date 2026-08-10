#!/usr/bin/env python3
# Copyright 2026 Circle Internet Group, Inc. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
checkers/result.py — the checker result contract.

Checkers used to print `count|preview`, which could express only "how many" and
overloaded the count field with the strings `error` and `ratelimited`. Two things
it could not express turned into silent data loss:

  * **Was the window fully drained?** Every checker reads a BOUNDED window (30
    Slack messages, 20 search hits, 10 Gmail messages). If more activity arrived
    than the window returned, the older items were never counted — and triage
    advanced the watermark to "now" anyway, skipping them permanently.
  * **What is the safe cursor?** Triage guessed `now - lag[type]` from a static
    per-type table. Only the checker knows the newest timestamp it actually
    covered.

So a checker now prints ONE LINE of JSON:

    {"outcome":"dirty","count":3,"preview":"...","advance_to":"1785920000.000000",
     "complete":true,"reason":""}

  outcome     clean | dirty | hold | ratelimited | error | misconfig
              hold        → the checker saw the window but will not let the
                            watermark move yet (e.g. changes it has not proven
                            the worker consumed). No dispatch.
              ratelimited → transient, triage holds the watermark, no dispatch.
              error       → triage backs off exponentially, no dispatch.
              misconfig   → permanent, needs a human, no dispatch.
  count       number of new items (only meaningful for clean/dirty)
  preview     short human string for the log line
  advance_to  OPTIONAL epoch float: the newest timestamp this check actually
              covered. Triage advances to this instead of now-minus-lag.
  complete    false means the window saturated and older items may be unseen.
              Triage then refuses to advance that watch's cursor at all.
  reason      OPTIONAL detail, appended to the log line.

Legacy `count|preview` output is still parsed by triage, so a checker that has
not been converted keeps working (as complete=true, advance_to unset).
"""

import json
import sys

CLEAN       = "clean"
DIRTY       = "dirty"
HOLD        = "hold"
RATELIMITED = "ratelimited"
ERROR       = "error"
MISCONFIG   = "misconfig"


class Transient(Exception):
    """A retryable upstream condition — rate limit, 5xx, timeout, network.

    Lives here rather than in each checker because the verdict it maps to is part
    of the contract: run() turns it into `ratelimited`, which holds the watermark
    so the next tick re-reads the same window. Anything else becomes `error`.
    """


def emit(outcome, count=0, preview="", advance_to=None, complete=True, reason=""):
    """Print the single-line JSON result. Never raises."""
    obj = {
        "outcome":  outcome,
        "count":    int(count or 0),
        "preview":  (preview or "")[:200],
        "complete": bool(complete),
    }
    if advance_to is not None:
        try:
            obj["advance_to"] = f"{float(advance_to):.6f}"
        except (TypeError, ValueError):
            pass
    if reason:
        obj["reason"] = str(reason)[:300]
    print(json.dumps(obj, separators=(",", ":")))


def counted(count, preview="", advance_to=None, complete=True):
    """Emit clean or dirty based on the count. The common checker exit."""
    emit(DIRTY if (count or 0) > 0 else CLEAN,
         count=count, preview=preview, advance_to=advance_to, complete=complete)


def ratelimited(reason="transient rate limit; watermark held"):
    emit(RATELIMITED, reason=reason, complete=False)


def transient_cause(stderr, tool):
    """Name WHY a client.py exit-4 happened, from the stderr it already wrote.

    Exit 4 is one bucket for several unrelated failures — HTTP 429, a 5xx, a socket
    timeout, DNS/connection refused — and every checker used to report all of them as
    "rate limit or network". That reads as a rate limit, so 2,236 events in a single day
    were filed under "Slack is throttling us" without evidence; only 50 that day actually
    said `ratelimited`. Tuning concurrency against a number that mostly measures timeouts
    would have optimised the wrong thing.

    client.py already prints a precise `ERROR: ...` line to stderr before exiting; the
    checkers simply discarded it. This reads it back and returns a short stable token
    (`slack ratelimited` / `slack 5xx` / `timeout` / `network`) for the reason string, so
    the run-log can be counted by actual cause. Unrecognised text stays "transient" rather
    than being forced into a category — an honest unknown beats a confident wrong label.
    """
    s = (stderr or "").strip().lower()
    if "429" in s or "ratelimited" in s or "rate_limited" in s or "rate limit" in s:
        return "slack ratelimited"
    if any(c in s for c in ("http 500", "http 502", "http 503", "http 504",
                            "service_unavailable", "internal_error")):
        return "slack 5xx"
    if "timeout" in s or "timederror" in s or "timed out" in s:
        return "timeout"
    if any(c in s for c in ("urlerror", "connectionerror", "oserror", "refused",
                            "reset", "dns", "name or service", "unreachable")):
        return "network"
    return "transient"


def error(reason):
    # complete=False on principle: a failed check saw nothing, so its cursor must
    # never advance even if some other code path decides to dispatch.
    emit(ERROR, reason=reason, complete=False)


def misconfig(reason):
    emit(MISCONFIG, reason=reason, complete=False)


def guard(main_fn):
    """Run a zero-argument checker main() and convert any escaping exception into a
    parseable verdict rather than a traceback on stdout that triage cannot parse.

    Transient → ratelimited (watermark held, next tick retries the same window).
    Anything else → error (triage backs off exponentially).
    """
    try:
        main_fn()
    except Transient as exc:
        ratelimited(str(exc))
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all at the boundary
        error(f"{type(exc).__name__}: {exc}")
        sys.exit(0)


def run(main_fn):
    """Entry point for a per-entry checker: parse the watch entry from argv and run it.

    Every per-entry checker is invoked as `<checker>.py '<watch json>'`, so the argv
    parse belongs here rather than being re-derived in each one. Parse failures go
    through the same guard, so a malformed entry is an `error` verdict, not a
    traceback triage would have to treat as a crash.
    """
    if len(sys.argv) < 2:
        error("missing watch entry argument")
        sys.exit(0)
    guard(lambda: main_fn(json.loads(sys.argv[1])))
