# Verification: architecture review of sidequestor @ 309aab8

Reviewed 2026-08-14 against the private working tree (branch `dashboard-submit-for-revision`)
and the public mirror `.git-yaas-v2`. Source doc: `source-review.html`.

**Verdict: accurate.** Every checkable claim reproduces. Two small caveats below; nothing
that changes a recommendation.

## The three "shipped broken" defects (candidate 1)

| Claim | Status | Evidence |
|---|---|---|
| `/api/undo/<id>` 404s | **Confirmed, still live** | `dashboard.html:921` and `:950` (reclaim reuses the undo endpoint) POST to `/api/undo/`. `dashboard-server.py:1682` routes only `review\|revise\|edit\|cancel`, `:1687` routes `prompt`, `:1691` falls through to 404. |
| `/api/edit/<id>` 404s | **Confirmed at 309aab8; fixed locally, uncommitted** | `git show HEAD:...dashboard-server.py:1535` routes only `review\|revise\|cancel`. The working tree adds `"edit"`. Not committed on any branch. |
| `item.stalled` has no producer | **Confirmed, still live** | `dashboard.html:854` gates the whole reclaim UI on `!!item.stalled`. `grep -rn stalled yaas-triage --include='*.py'` returns only `run-agent.py` (worker-stream stall detection, unrelated) and `health-monitor.py` (`triage_stalled` flag, unrelated). `_approval_card` never emits the field. |
| In-flight items render twice | **Confirmed, still live** | `build_messages` (`dashboard-server.py:1079`) filters `pending` on `status not in ("executed","cancelled")`, so `reviewed`/`executing` land in `needs_you`/`other_actions`; `:1153-1166` puts the same `reviewed`/`executing` items in `queued_items`. Client renders both: `dashboard.html:674` (carousel) and `:676` (strip). The client comment at `:850-853` asserts they "no longer reach the carousel at all", which is false. |
| 309aab8 is 1 file / 141 insertions / 0 server lines | **Confirmed exactly** | `git --git-dir=.git-yaas-v2 show --stat 309aab8` |

The root-cause framing is right. The state machine is authored on both sides of the seam, so
a client-only commit renders cleanly and half-works.

## Structural candidates

| # | Claim | Status |
|---|---|---|
| 2 | Watch types re-enumerated across many sites; `.lag` sidecar pattern already proves the fix | **Confirmed.** 5 `.lag` files against ~10 real watch types. `tick.py` genuinely never learns the type set. |
| 3 | `github_issue.py` (262 ln) and `github_pr.py` (294 ln) are near-duplicates | **Confirmed.** Line counts match to the line. 144 lines are byte-identical after sort; ~111 identical in sequence is the right order of magnitude. `tests/unit/checkers/github_issue.test.sh:18` literally says "github_issue.py was copied from github_pr.py". |
| 4 | Backoff predicate written 5 times + 2 byte-identical write sites | **Confirmed.** Read sites: `tick.py:274`, `tick.py:293`, `checker-health.py:175`, `dashboard-server.py:975`, `dashboard-server.py:1387`. Write sites: `tick.py:754` and `tick.py:1174`, byte-identical. |
| 5 | Lease-expiry decided twice with different rules | **Confirmed.** One writer (`approval-helper.py:442`), two divergent readers (`checkers/approval.py:101`, `health-monitor.py:199-201`, the latter explicitly falling back to `executing_at`), one reader with no producer (`dashboard.html`). |
| 6 | Lift only the write path out of `dashboard-server.py` | **Reasonable.** The read/write asymmetry is real; this is a judgement call, not a defect. |
| 7 | `tick.py:304-306` duplicates the first four branches of `tick_check.classify` | **Confirmed.** Same four conditions, same order, and the code comment at `:301-302` admits it. |
| 8 | `startswith("slack_")` used as an undeclared upstream grouping | **Confirmed.** 5 occurrences at `tick.py:256,257,258,353,365`. Correct today; the review already flags it as speculative. |
| 9 | Prose type tables drift with nothing keeping them in sync | **Confirmed** in principle; the exact site count was not re-counted. |

## Caveats

1. **The `edit` 404 is already fixed in the working tree**, uncommitted. The review could not
   see that. It does not weaken candidate 1: the fix is a second one-sided patch, which is the
   pattern the review is complaining about.
2. **Minor naming slip.** The review writes `cron_due.py`; the file is `checkers/cron-due.py`.
   Cosmetic.
3. The "16 watch-type enumerations" and "21 import edges / 186 files" headline numbers were
   not independently recounted. They are plausible and nothing depends on them.
