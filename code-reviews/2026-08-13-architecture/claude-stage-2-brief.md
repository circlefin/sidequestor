# Stage 2 execution brief (for Codex)

Architecture is decided. Execution only. Do not redesign, do not widen scope.
Baseline commit: `bc953ab` (in `.git-yaas-v2`, the git tree of record for this repo).
Full plan and rationale: `claude-plan.md` in this directory.

**Scope is Stage 2 only** (items 2.1 to 2.4). Do not start Stage 3, 4 or 5. In particular do not
build `approval_state.py`, a transition table, or a watch-type manifest. If you spot something
from a later stage, write it in the report and leave the code alone.

**Every item in this stage is a pure-function extraction with NO behaviour change.** That is the
whole point of the stage. If you find yourself changing what the system does, stop and report it
instead. Line numbers below were verified at `bc953ab`.

---

## Ground rules

1. **The differential goldens are your safety net.** `yaas-triage/tests/differential/` replays
   `tick.py` against recorded goldens. Items 2.1, 2.2 and 2.3 all touch `tick.py`, so those
   goldens must stay green with **no golden edits**. If a golden changes, you have altered
   behaviour and the change is wrong. Do not update a golden to make a test pass.
2. **Do not commit.** The sandbox blocks `.git` writes and this repo has two git dirs, so commits
   will be made for you. Leave everything in the working tree. Ignore any "one commit per item"
   habit.
3. Run the full suite before you finish: `bash yaas-triage/tests/run-all.sh`. Note that this
   runner **exits 0 even when a suite fails**, so read the summary line, do not trust `$?`.
   Report the real numbers. If it is red, say so plainly.
4. Each of the four items is independent. Do them in order; if one gets stuck, move on and report.
5. Never touch `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` or `.env`.

---

## 2.1 `structural_verdict()` in `tick_check.py` (do this first, smallest and safest)

`tick.py:304-306` computes `structurally_held` from four conditions. `tick_check.classify()`
(from `tick_check.py:67`) opens with the *same four conditions in the same order*. The duplication
is deliberate today: `tick.py` must know whether to exec the checker at all, and the comment at
`tick.py:301-302` says so out loud.

Extract one pure function in `tick_check.py`:

    structural_verdict(watch, health, unacked, unacked_due, unacked_promote, checker_exists)
        -> verdict | None

It returns the verdict when a structural gate fires, and `None` when none does. Then:

- `tick.py` becomes: call it; if `None`, exec the checker; otherwise use the returned verdict.
- `classify()` calls it as its own prologue instead of re-listing the four branches.

The four conditions, their order, and their exact `reason` strings must be preserved byte for
byte. The verdict strings are asserted by tests and replayed by the goldens.

---

## 2.2 One retry-time predicate `is_due(rec, now_ts)` in `tick_check.py`

Five sites hand-roll the same "has `next_retry_ts` passed?" comparison with five different parse
fallbacks:

- `tick.py:274`
- `tick.py:293`
- `yaas-triage/ledger/checker-health.py:175`
- `yaas-triage/ops/dashboard-server.py:987`
- `yaas-triage/ops/dashboard-server.py:1397`

Write one pure `is_due(rec, now_ts) -> bool` and route all five through it. One unit test covers
the parse tolerance for `0`, `""`, `None` and garbage. Preserve each site's current behaviour for
malformed input exactly; if two sites genuinely differ today, report it rather than silently
picking one.

**Do NOT write a generic `apply_backoff(rec, now_ts, promote)`.** There are two deliberately
different ladders and merging them would hide the difference:

| ladder | base | cap | promote threshold |
|---|---|---|---|
| `checker-health._backoff_for` (`checker-health.py:110`) | 60s | 3600s | none |
| `tick.unacked_backoff_for` (`tick.py:1147`) | 300s | 86400s | yes |

Share only the time predicate. Leave both ladder functions where they are, separately named.

---

## 2.3 Collapse the two byte-identical unacked write blocks

`tick.py:753-754` and `tick.py:1173-1174` are the same computation and write:

    wait = unacked_backoff_for(rec["count"], t.unacked_promote)
    rec["next_retry_ts"] = f"{t.now_ts + wait:.6f}" if wait else "0"

Plus the surrounding `backoff_sec` assignment. Factor into one small helper called from both
sites. Check the surrounding lines: if the two blocks are wider than they look, collapse the
whole common region, but do not merge the two call sites' differing context.

This is independent of 2.2 and just as safe.

---

## 2.4 GitHub checker collapse

`checkers/github_issue.py` (262 lines) and `checkers/github_pr.py` (294 lines) encode the same
watermark doctrine twice. 144 lines are byte-identical.

- New `checkers/github.py` holds the shared doctrine: tie safety, prefix-not-suffix bounding,
  boundary re-filter, and the `gh` exit taxonomy.
- `github_issue.py` and `github_pr.py` shrink to adapters supplying `(noun, json_fields,
  preview_fn)` or whatever minimal shape the code actually needs.

**`checkers/github.py` MUST NOT be executable.** `tests/contract/checker-contract.test.sh:161-168`
asserts that non-executable files in `checkers/` are helpers and can never be dispatched as a
watch type. `result.py` and `slack_utils.py` are the existing precedent for a shared helper there;
follow them. The two adapters stay executable.

Keep both type-specific test suites. `tests/unit/checkers/github_issue.test.sh:18` explains that
the file was copied from `github_pr.py` and so inherited the fix for the 2026-08-05 stall
(unbounded DESCENDING query, watermark could never cross the gap, 14 hours parked). That fix must
end up in exactly one place, and the query-shape assertions must still hold for both types. Add a
shared adapter-contract test if it helps, but do not delete the per-type safety assertions.

Update the copied-from comment at `github_issue.test.sh:18` once the duplication is gone, since it
will no longer be true.

---

## Report back

- What changed, per numbered item.
- The exact test command and the real summary line, pass or fail, including the differential
  section.
- Explicit confirmation that **no golden file was edited**.
- Anything in this brief that turned out to be wrong when you got into the code. The line numbers
  and claims were verified at `bc953ab`; if one is off, report it rather than working around it.
- Anything you deliberately left alone.
