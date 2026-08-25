---
name: yaas-checker-authoring
description: Add a new watch type to the yaas triage loop by pairing an executable checkers/<type>.py with checkers/<type>.watch.json, then covering its behavior and affected docs. Load when asked to watch a source triage cannot watch yet (a new API, a queue, a feed), or when reviewing a checker someone else added.
---

# yaas-checker-authoring

A **checker** is one script that answers a single question for one watch entry: *has anything
happened on this since the watermark?* It runs on every tick, in Python, with no LLM. It is the
cheapest part of the system and the most dangerous: a checker that gets its answer wrong does
not throw, it just silently stops waking the worker, and nobody notices until someone asks why
a thread went unanswered for a day.

Two failure modes matter. Everything below exists to prevent one of them.

- **Silent data loss.** The watermark advances past activity that was never seen. The watch
  reports clean forever; the missed item never comes back.
- **Runaway cost.** The checker reports dirty when nothing happened, or holds forever, and
  every tick pays for an Opus dispatch. Unbounded, that compounds fast.

Read `checkers/result.py` before anything else. It is the contract, and it is short.

---

## 1. Decide whether you need a checker at all

Do not add one when:

- An existing type covers it with a `search`/`query`/`jql` narrowing. Prefer that.
- The thing you want fires on a clock, not on external change → use `schedule`.
- The source has no monotonic "last updated" field per item, and no cursor. Without one you
  cannot express a watermark, and everything below becomes guesswork. Find another surface
  (a webhook into a file, a feed, a search index) before writing a poller that diffs blobs.

A new type is justified when there is a **distinct external surface** with its own auth, its
own pagination, and its own idea of time.

---

## 2. Write `checkers/<type>.py`

Copy the closest sibling rather than starting blank — the doctrine below is already encoded in
`github_pr.py` (a bounded search index) and `slack_thread.py` + `slack_utils.py` (a paginated
conversation). Copying is also how doctrine rots, so §4 asks you to re-assert it in tests
instead of trusting the sibling's suite.

**Interface.** `argv[1]` is the watch entry as JSON, including `last_checked_ts`. Print exactly
one line of `result.py` JSON to stdout. Exit code is ignored; the line is everything. The file
must be executable (`chmod +x`) or triage classifies every watch of that type as misconfig and
freezes its watermark — silently, forever.

**Outcomes, and what each one costs.** Only `dirty` spends money.

| Outcome | Means | Triage does |
|---|---|---|
| `clean` | nothing new, window fully covered | advances the watermark |
| `dirty` | N new items | advances, then dispatches a worker |
| `hold` | cannot prove the window was drained | holds the watermark, no dispatch |
| `ratelimited` | transient upstream (429, 5xx, timeout, reset) | skips the tick, holds |
| `error` | hard failure, might be retryable | exponential backoff |
| `misconfig` | permanent, needs a human | holds and pages |

Classifying a transient as `error` costs you the backoff window. Classifying it as `clean` is
data loss. Classifying a permanent condition (bad credential, missing repo, deleted channel) as
`error` burns a day of retries before it is promoted to misconfig anyway — send those straight
to `misconfig` with a reason that names the fix.

**The watermark rules.** These are the ones that were learned the expensive way:

1. **Bound the low end of the query and sort ASCENDING.** A "newest N" query on a busy source
   returns a *suffix* of the unread gap, and a watermark can never cross a suffix — the unread
   part sits directly above it. Bounding low and sorting ascending makes the page a *prefix*,
   which is committable. This is the exact bug that parked a `github_pr` watch for 14 hours
   across 424 misconfigured events (2026-08-05).
2. **`complete: true` means "everything up to `advance_to` was seen"**, not "the whole backlog
   is done". Reporting `complete: false` on a safe prefix recreates the stall with code that
   looks right. `slack_utils.drain()` uses the same convention.
3. **Never advance onto a timestamp you may hold only partially.** If the page saturated at
   `limit`, the boundary row's timestamp may have more rows you never saw. Advance strictly
   *below* it, or `hold` if that leaves nothing.
4. **Re-apply the exact boundary in code.** Most upstream `since`/`updated:>=` filters are
   inclusive and coarse (whole seconds), so rows at or below the watermark come back. Filter
   them out yourself; do not trust the server's `>`.
5. **Prove monotonicity.** Feed tick N's `advance_to` back in as tick N+1's watermark and
   assert it goes strictly up. A checker that cannot advance is a livelock that looks healthy.

**Pre-dispatch filters.** If the type can wake the worker on things nobody cares about, accept
filter fields (`filter_user_ids`, `filter_keywords` on the Slack types) and evaluate them
*inside the checker*. A rejection there is free; a rejection after dispatch costs a model
round-trip. Note in the docstring that extra watch fields are passed through unvalidated, so a
misspelled filter key is silently ignored.

**Credentials.** Never mutate global auth state (`gh auth switch`, writing a config file, or
exporting into the parent env) — the tick runs many checkers in one process tree and the next
one inherits your mess. Resolve a credential per subprocess and pass it in that subprocess's
`env` only. Never let it reach stdout: the result line goes into the log.

**Argument injection.** If a watch field is spliced into a command line, treat it as hostile
even though a human wrote it. Refuse anything that could become a flag rather than a value,
and refuse it as `misconfig` before invoking the tool. A `search` string that smuggles
`--include-prs` into an issues query silently doubles both the reports and the bill.

**`<type>.lag`.** Optional file next to the checker holding one integer: seconds to hold back
the watermark, for sources whose index is eventually consistent (`github_pr` and
`github_issue` = 30, `email` = 120, `slack_mention` = 90, `jira` = 15). Missing file means 0.
Keep it as small as the source allows: every second of lag widens the window in which an item
is reported twice.

---

## 3. Registration — all of it, or the type is broken in a way nothing catches

A checker file alone does nothing, but the registration is now **two files**, not a dozen. The
manifest is the single declaration; everything else derives from it by glob.

| File | What to add | If you skip it |
|---|---|---|
| `checkers/<type>.py` | the checker, **executable** | every watch of the type is misconfig |
| `checkers/<type>.watch.json` | the manifest, see below | the bijection test fails; nothing can use the type |
| `checkers/<type>.lag` | integer seconds, if the source lags | duplicate reports, or missed items |

The manifest carries `schema_version`, `required` (an AND-of-OR list), `identity`,
`checker_example`, `open_loop`, `user_creatable` and `upstream`.

**Derived automatically. Do not hand-edit these; they used to be separate lists and are not
any more:**

| Consumer | Reads |
|---|---|
| `ledger/add-watch.py` | `required` + `identity` via `load_watch_manifests` |
| `skills/yaas-quest-creation/new-quest.py` | `required` + `user_creatable` |
| `ops/dashboard-server.py` | `open_loop`, for the open-items display |
| `tests/lib/scenario.py` | the type set |
| `tests/behaviour/checker-contract.test.sh` | `checker_example`, via `entry_for()` |

**Still hand-maintained, but under contract:** the watch-type tables in `README.md` and
`ARCHITECTURE.md`, the `.lag` values in `yaas-ops/SKILL.md`, and the creation guidance in
`yaas-quest-creation/SKILL.md`. `tests/behaviour/doc-contracts.test.sh` fails with the exact file
and expectation if you add a type or change a `.lag` value without updating them.

**On `identity`:** think about it rather than copying the sibling. `github_issue` uses
`["repo", "search"]` while `github_pr` uses `["repo"]`, because two issue watches on one repo
with different qualifiers are genuinely different watches and collapsing them would drop one.

**Rule: Slack-backed types MUST be named `slack_*`.** `tick.py` and `tick_dispatch.py` still group
Slack types by the `startswith("slack_")` prefix rather than by the manifest's `upstream` field,
and `tests/behaviour/checker-contract.test.sh` enforces that the two agree in both directions. This
is deliberate because loading manifests in the tick hot path would give every 60s liveness pass a
hard dependency on those files, but it means the name is still part of the contract for Slack.

---

## 4. Tests — what a checker has to prove

Suites live at `tests/unit/checkers/<type>.test.sh`, mirroring the source path.
`tests/coverage.sh` reports any source file with no test at the matching path; it does not fail
the build, so a missing suite is visible but not blocking — write it anyway.
`tests/run-all.sh` runs everything. Copy the harness (`_find_triage`, `ok`/`bad`/`eq`, a fake
binary on `$PATH` via `GH_BIN`-style indirection) from `github_pr.test.sh` or
`github_issue.test.sh`.

**Fake the external binary, and assert the query it was given.** The query shape *is* the
watermark fix, so record argv and grep it. Asserting only on counts passes happily while the
query has been "simplified" back into a suffix scan.

Every checker suite should cover, at minimum:

- **Query shape** — bounded low, ascending, boundary backed off by the source's granularity.
- **Full page, distinct timestamps** — `complete: true`, `advance_to` strictly below the final
  row, count limited to the safe prefix, backlog surfaced in the preview.
- **Short page** — drained, no backlog claim.
- **Monotonic advance** — tick 2 starts strictly above tick 1.
- **Tie safety, both shapes** — a full page whose new rows share one timestamp → `hold`; a full
  page with *nothing* past the watermark → `hold`, never `clean` (this is the dangerous one:
  clean here lets triage jump to now-minus-lag and skip the whole backlog).
- **Empty result** — `clean`, and specifically *not* an error. Check how the tool signals empty:
  `gh --json` prints `[]`, so a blank-stdout check would misread it.
- **Boundary re-filter** — a row at or below the watermark is dropped.
- **Failure classification** — one case each for transient → `ratelimited`, permanent →
  `misconfig`, unknown → `error`.
- **Credential handling, if any** — the token reaches the call that needs it and no other, never
  appears in the emitted line, and global auth state is untouched.
- **Injection, if any field reaches a command line** — refused as `misconfig`, *before* the
  tool is invoked (assert the fake binary was never called).

Then run the wider suites, because they exercise the manifest registry and dispatch behavior:
`tests/behaviour/checker-contract.test.sh` (every checker honours the contract on both a
plausible and a nonsense entry, and reports no `AttributeError`/`NameError`-class bug),
`tests/behaviour/dirty-watch-dispatch.test.sh`, and `tests/run-all.sh`.

Finally, run it **against the real source once** with a recent watermark and eyeball the
output. Fakes agree with whatever you believed when you wrote them.

---

## 5. Documentation — the tree that goes stale

All of these are in the public `yaas-v2` mirror, and each is read by someone (or some future
session) deciding whether the type exists. Update them in the same commit as the checker:

| Path | What it holds |
|---|---|
| `ARCHITECTURE.md` | the watch-type table, above the result contract |
| `README.md` | the short "things you can watch" table |
| `yaas-triage/skills/yaas-ops/SKILL.md` | the `checkers/` tree diagram, the `.lag` list, the example watch entry, and the per-type option notes |
| `yaas-triage/skills/yaas-quest-creation/SKILL.md` | the watch-type list the creation flow offers, with its traps |
| `checkers/<type>.watch.json` | the manifest fields and whether quest creation offers the type |
| `yaas-triage/skills/yaas-quest-dispatch/SKILL.md` | **what a worker should DO when this type fires** — the commands to read the item, and the out-of-scope rule |
| the checker's own module docstring | the real reference: every option, every trap, and the incident that motivated each rule |

The dispatch skill is the one people forget. A watch type with no entry there fires correctly
and then the worker has to improvise what to do about it, at Opus prices, every time.

Sanity check before committing, in a source checkout: run the repository's
`tests/behaviour/doc-contracts.test.sh`. It fails with the exact stale doc file and expected contract when a new type or lag value is missing from
the maintained tables in `README.md`, `ARCHITECTURE.md`, `yaas-ops/SKILL.md`, or
`yaas-quest-creation/SKILL.md`.

---

## 6. Review it adversarially before trusting it

A checker is ~200 lines that runs unattended every 60 seconds against money and other people's
attention. Get a second model to review it (`codex exec` works well headless) and point the
review at the two failure modes explicitly rather than asking for general feedback — ask
whether any path can advance a watermark past unseen activity, whether transients can be
misread as clean or permanent conditions as retryable, and whether any field can escape into
the command line. The `github_issue` review that way surfaced both a flag-injection path and a
misclassified 404 that the author's own tests had not thought to cover.

---

## 7. Rollout

New watches start their watermark at creation time, so an existing backlog will **not** fire.
Say so explicitly when you hand the quest over — the difference between "watching" and "caught
up" is exactly the kind of thing a user assumes the other way.

Then watch the first real tick: `DRY_RUN=1 sq tick` proves the checker is
wired in and clean; `logs/` and the dashboard show what it does once it is live.
