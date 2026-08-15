# PR #1 review response — plan and outcome

Source: an automated code review left on the upstream PR, 2026-08-14. 11 findings.
Verified against the working tree at `0bd911d` before writing this. Started as a plan; kept
updated in place as each item resolved, so it is now the record of what actually happened.

**Final outcome: all 11 findings were legitimate. 10 fixed, 1 rejected as unsafe.**

That is not what this document originally said. It opened with "10 of 11 are legitimate, declining
1 and deferring 1". Both of those calls were overturned, and in opposite directions:

- **Finding 10 (declined) was real.** A colleague hit it while installing. See below.
- **Finding 11 (deferred) was cheaper than predicted.** Done on request; see below.
- **Finding 3, which I planned to fix, was the one that had to be rejected.** It would have caused
  silent data loss. See below.

The pattern is worth keeping: every one of those three reversals came from evidence outside the
review loop — a real install, a revert-test, a reading of the actual sort key — and none came from
a reviewer agreeing with another reviewer. Codex confirmed two of my wrong calls.

Two of them share a shape worth naming up front, because it is the same class of defect in two
files: **an unknown value is treated as infinitely old, so the highest-consequence branch fires.**
Both files already contain the safe version of that pattern elsewhere, which is what makes them
bugs rather than choices.

---

## Fix now

### 1. `housekeep.py` — `retire_thread` deletes on unknown age (highest severity)

`_thread_epoch` (`:121`) returns `0.0` for a missing or non-numeric `thread_ts`, and
`retire_thread` (`:132`) is `_thread_epoch(w) < cutoff_epoch`. `0.0 < now-14d` is always true, so
the watch is deleted.

The same file's `_created_epoch` (`:150`) carries a long docstring about routing unknown age to
*backfill* precisely so a watch is not lost. `retire_thread` contradicts it, and deletion is the
highest-consequence operation in the file.

**Fix:** unknown age is young. Return early (keep) when `thread_ts` is missing or unparseable,
rather than comparing `0.0`. Do not change behaviour for a valid `thread_ts`.

**Refinement (Codex, accepted).** "Keep forever" alone trades a delete-bug for an immortality-bug:
a `slack_thread` watch with a missing or bad `thread_ts` is malformed at creation and unusable at
check time (`checkers/slack_thread.py:22,50`), so silently keeping it strands a broken watch — and
`_created_epoch`'s own docstring calls immortality "the exact immortality this rule exists to end".

So keep **and surface**: retain the watch, and report it as malformed on the housekeep output so it
is visible and repairable, rather than either deleting it silently or keeping it silently. Do not
attempt to auto-repair `thread_ts` here; housekeep's job is retention, not reconstruction.

### 2. `rotate-logs.py` — archives untimestamped run-log events regardless of age

`:86` does `ts = json.loads(line).get("ts", "")`, and `:90` archives when `ts < cutoff`. Empty
string sorts before every date, so any event without a `ts` is archived out of the live log.

Same file, two lines apart, already gets this right twice: unparseable lines are kept with the
comment "never silently dropped" (`:88`), and approval pruning defaults `created_at` to `"9999"`
(`:142`) so unknown means keep.

**Fix:** treat a missing `ts` as keep, matching the two neighbouring conventions.

**CORRECTION (Codex, verified).** My first draft of this section claimed the fix "restores spend
accounting". **That is wrong.** `spend-window.py:65` returns `None` from `_parse_ts` for a missing
`ts`, and `:124` skips on `t is None`, so an untimestamped event is ignored whether it sits in the
live log or the archive. And that is unavoidable rather than a second bug: an event with no
timestamp cannot be placed in a 24-hour window at all.

So the real and only benefit is narrower: **stop silently moving malformed lines out of the live
log**, matching the file's own "never silently dropped" rule. Do not claim a spend-accounting fix.

**Known downside, accepted:** if a producer ever starts emitting untimestamped events in volume,
these lines now accumulate in the live log unbounded. That is the same trade the existing
unparseable-line branch already makes, so it stays consistent; flag it if it ever shows up.

### 3. `reactions.py` — sweep never early-stops — **REJECTED AFTER EXECUTION, NOT FIXED**

**This fix was implemented, then reverted. Both the reviewer and Codex signed it off, and I
wrote the flawed premise into this plan. It would have caused silent data loss.**

The premise was "results are sorted timestamp-desc, so once a page has no new ts, no later page
can either". That conflates two different clocks. The sort key is the **message** timestamp
(`Message_ts`, `reactions.py:112`), and the processed-state identity is also the message ts
(`:78,84`) — neither has anything to do with *when the reaction was applied*. The query is
`hasmy::<emoji>: after:<60d cutoff>`.

So: react today to a three-week-old message and that message enters the result set for the first
time, ordered by its old timestamp, on a late page. An early-stop on the first all-known page
never reaches it. Reacting to a message is the primary way this system is triggered, so the
failure is both silent and central. The test written for the fix *encoded* the loss, asserting
that a genuinely-new timestamp should no longer be collected.

**No safe optimization exists inside this search design** (Codex, verified): a narrower `after:`
window is unsafe *and strictly worse than today*; a lower `MAX_PAGES` has the same miss class; a
per-emoji high-water mark on message ts has the same miss class. A safe version would need a
signal keyed by reaction-event time, which this search model does not provide.

**Answer to the reviewer for finding 7:** the concern is legitimate — ~120 searches per run is real
budget on a rate-limited upstream — but the proposed remedy is unsafe and the
issue is **left unresolved pending a design change**, not fixed.

*(Separate pre-existing gap, noted not fixed: `after:<cutoff>` already means a reaction on a
message older than 60 days is never seen at all.)*

---

### 3b. Original analysis, retained for the record

The page loop (`:108-128`) breaks only on cursor exhaustion, an empty page, or `MAX_PAGES = 30`.
Results are sorted timestamp-descending and everything older is already in `known`, so once a full
page yields no unknown `ts`, no later page can either. On a busy workspace that is up to
30 pages x 4 emojis ~= 120 Slack searches per run.

Slack is rate-limited and every search costs budget, so conserving it is not theoretical.

**Fix:** break when a page produced results but none were new. Keep the existing `truncated`
reporting for the genuine `MAX_PAGES` case, and do not break on an empty page differently than
today.

### 4. `watch-guard.py` — repair can duplicate a watch entry

In the repair loop (`:168-178`), every entry whose `watch_id` is in `before` is replaced with
`before[wid]`. Two entries sharing a `watch_id` therefore both become the same snapshot value,
producing two identical entries.

Reachability is low (`add-watch.py` dedups on identity, so this needs a hand-edit or an upstream
bug) but this is the repair path for the file that guards watermark integrity: it should not be
able to corrupt what it repairs.

**Fix:** dedup by `watch_id` when rewriting. Keep the first occurrence, preserve order.

### 5. `watch-guard.py` — dead `seen` set

`seen` is built at `:168` and `:177` and never read. Delete it. It implies dedup is enforced when
it is not, which is doubly misleading next to finding 4.

### 6. `log-event.py` — uncaught crash on NUL in `quest_id`

`:192` rejects `/`, `""`, `.`, `..` but not `\x00`, so a NUL reaches `Path.is_dir()` and raises
`ValueError: embedded null byte` as a raw traceback instead of the intended
`error: invalid quest id` exit 1.

**Fix:** reject control characters alongside `/`. This is an input-validation trust boundary; it
should fail as designed rather than crash.

### 7. `spend-window.py` — docstring contradicts enforcement

`:44` documents `--cap-6h` as "accepted but unused", but `:171` actively enforces it and emits a
breach. A caller trusting the docstring would be blocked by a cap it believed inert.

**Fix:** correct the docstring to match the enforcement. Do **not** remove the enforcement: the
ladder is live behaviour and changing it is a behaviour change nobody asked for.

### 8. `yaas-quest-dispatch/SKILL.md` — residual hand-write guidance

`:26` and `:42` correctly say "via `log-event.py`", but `:99`, `:121`, `:148` and `:209` say
"Log X to `timeline.ndjson`" without naming the helper. That nudges a worker toward hand-writing a
line with an invented `ts`, which is exactly what `log-event.py` exists to prevent and what
CLAUDE.md behavioural rule 4 forbids.

**Fix:** route those four through `log-event.py` in the wording. Prose only, no behaviour change.

### 9. `approval_state.py` — `_lease_expired` naive/aware mismatch

`:73` parses `lease` with bare `fromisoformat` while `now` goes through `_as_dt` (`:58`), which
Z-normalizes. A naive/aware mismatch raises `TypeError`, which `:74` swallows as `False` — so a
stalled `executing` item never reports lease-expired, `available_actions` never offers `reclaim`,
and the item is stuck forever.

**Honest severity note:** I checked every caller. `checkers/approval.py:116`,
`dashboard-server.py:469`, and all of `approval-helper.py` pass `datetime.now(timezone.utc)`,
which is aware. **This is not reachable today.** It is a latent trap that fires the moment someone
passes a naive `now`, and the failure is silent and permanent.

**Fix anyway:** normalize both sides through `_as_dt`. One line, removes the trap.

---

## Declined, then overturned by field evidence

### 10. PEP 604 union without `from __future__ import annotations` — **NOW FIXED**

`-> Path | None` breaks on Python < 3.10.

**I declined this, and I was wrong.** My argument was that it "buys nothing for a 3.9 deployment
that does not exist". It does exist: a colleague hit exactly this while installing, and the only
reason I could not see it is that I was reasoning from inside a repo running 3.14. Codex confirmed
the decline as defensible, so neither of us caught it — the evidence had to come from outside.

**Fixed properly, because the annotation was only the symptom:**

1. `from __future__ import annotations` added to all four files carrying PEP 604 unions
   (`approval-helper.py`, `dashboard-server.py`, `log-event.py`, `slack-send.py`; 8 annotations),
   inserted after each module docstring where Python requires it. Verified by asserting the
   annotation is now the *string* `'Path | None'` rather than an evaluated type, which is what
   makes the `def` line safe on 3.9.
2. **The real floor is 3.9, not 3.7.** `checkers/cron-due.py` imports `zoneinfo` and
   `dashboard-server.py:551` uses `str.removeprefix`, both 3.9+. This change moves the floor from
   3.10 to 3.9; it does not reach 3.8.
3. **Nothing validated the version at install time**, which is why it surfaced as a confusing
   `TypeError` mid-dispatch rather than a clear message. `doctor.sh` now checks explicitly and
   fails with "yaas needs 3.9 or newer (zoneinfo and str.removeprefix)", and the README install
   section states the requirement.

**Lesson worth keeping:** "no such deployment exists" is not something this repo can tell me. A
portability finding should be declined only against a stated minimum-version policy, never against
my assumption about who is running it.

## Deferred, then done on request

### 11. Three helpers duplicated from `slack-send.py` — **NOW FIXED**

`_quest_dir`, `_append_timeline` and `_utc_now` are byte-identical copies, and the drift risk is
real.

But the fix is a shared module imported by two standalone CLIs, and this repo has just paid that
tax three times: sharing `tick_check` into `dashboard-server.py` broke six minimal test fixtures,
and the same pattern recurred in Stages 3 and 4. `log-event.py` and `slack-send.py` are both
invoked as subprocesses by workers, so a new import widens their dependency surface for a
duplication that is currently 3 small functions.

**Original recommendation:** do it as its own change with the fixture survey done up front.

**Done, and the deferral over-priced it.** The fixture survey found exactly TWO consumers, using
different variable spellings (`$HERE` in `log-event.test.sh`, `$SCRIPT_DIR` in
`slack-send.test.sh`) — the same mismatch that made an earlier survey wrong.

`surfaces/timeline_io.py` now holds `utc_now`, `quest_dir(repo_root, quest_id)` and
`append_timeline`. Two design choices did the work:

- **`repo_root` is passed explicitly.** `_quest_dir` previously read a module global; the shared
  module carries no global.
- **The import is a sibling lookup** via `Path(__file__).resolve().parent`, so the shared module
  never needs to know the repo root. That is what avoids the depth-sensitivity behind the repo's
  documented duplication rule.

`_repo_root` stays duplicated and untouched in both files — verified by checking for `+`/`-` lines
touching it, not just that it still exists. That rule genuinely applies to the bootstrap; it does
not extend to the three non-bootstrap helpers, which is the distinction I under-weighted when
deferring.

Verified by mutation, not assertion:
- Removing the `timeline_io.py` copy from a fixture produces `ModuleNotFoundError` — so the
  provisioning is load-bearing, and the fixture tax was really paid.
- Re-adding `_utc_now` to `log-event.py` fails the new `timeline-helper-dedup.test.sh` with
  "still defines _utc_now" — so the duplication cannot silently return.

---

## Also in this batch (user request, not from the review)

### 12. Make the quest name in the agent trail clickable

In the activity/trail rows, the quest name should be clickable and bring that quest into focus.
Surface is `dashboard-v2.html`. Must reuse the existing quest-open path rather than inventing a
second one, and must not introduce an inline handler (CSP is `script-src 'nonce-...'`, so inline
`onclick` is silently dead — there is already one such dead handler in v1).

---

## Codex review of this plan (verified)

Codex reviewed the plan before execution. Its confirmations and the one correction:

| Question | Outcome |
|---|---|
| Is `retire_thread` the worst? | **Confirmed.** Worst *reachable* correctness bug: silently deletes live watches on bad input. `_lease_expired` correctly ranked below it because it is latent. |
| Is `_lease_expired` unreachable today? | **Confirmed.** No non-test caller passes a naive `now`; all use `datetime.now(timezone.utc)`. |
| Is declining PEP 604 defensible? | **Confirmed.** `Path \| None` without the future import is already the convention in `approval-helper.py:111`, `slack-send.py:104` and `log-event.py:125`. Changing one file reduces consistency. |
| Is deferring helper dedup defensible? | **Confirmed.** The repo *documents* deliberate byte-identical duplication for standalone scripts (`approval-helper.py:91`, `slack-send.py:85`) because shared imports reintroduce depth-sensitive path problems. |
| Is the reactions early-stop safe? | **This answer was WRONG, and so was my question.** Both of us read `sort_dir=desc` as implying recency. The sort key is the MESSAGE ts, not reaction time. Overturned during execution: see finding 3. |
| Does `watch_id` dedup risk dropping a distinct entry? | **No.** `watch_id` is intended unique and `add-watch.py:205` suffixes to avoid collisions. |
| Anything overclaimed? | **Yes — the rotate-logs rationale.** Corrected in place above. |
| Down-rank anything? | Yes: the dead `seen` set is cleanup, not a defect. Reflected below. |

Severity order as executed: **1 (housekeep delete) > 2 (rotate-logs) > 4 (watch-guard dedup) >
6 (NUL guard) > 9 (lease, latent) > 7, 8 (docs) > 5 (dead set, cleanup).** Finding 3 was attempted
at this point in the order and then reverted; it is not in the shipped set.

Implementation note for item 12: `dashboard-v2.html` already has a single quest-open path via
`state.selected` and `button[data-quest]` (`:52,68,72`). Reuse it; do not add a second.

---

## Execution constraints (as applied)

- `tick.py` differential goldens must stay green with **no golden edits**. Findings 1, 4 and 5
  touch code the tick exercises. **I broke this constraint myself**: the later incident scrub
  edited the `why` field of `tests/differential/scenarios/watch_ratelimited_surfaces.json`. It is
  descriptive only — `run.sh:109` prints it on failure and nothing asserts it — so the differential
  stayed 29/0, but I checked that after editing rather than before.
- Every fix needs a test that **fails before and passes after**. Verified by reverting findings 1
  and 2 in isolation and confirming their tests go red.
- No behaviour change beyond the stated fix. Finding 7 is a docstring correction, not a cap change.

## Final state

- **Suite: 46 suites, 0 failed; differential 29 passed / 0 failed.**
- Findings 1, 2, 4, 5, 6, 7, 8, 9, 10, 11 fixed. Finding 3 rejected, unresolved by design.
- Item 12 (clickable quest name in the agent trail) done and verified in a browser.
- Commit `ecd4a8e` carries findings 1-9 and item 12. The Python 3.9 fix, the deduplication, the
  incident scrub and these document corrections were still uncommitted when this was written.

## Still open

- **Finding 3 has no fix.** ~120 searches per sweep remains real cost. It needs a signal keyed by
  reaction-event time; the current search model cannot provide one.
- **Pre-existing, unrelated to this review:** `after:<60d cutoff>` means a reaction on a message
  older than 60 days is never seen at all.
