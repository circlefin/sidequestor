# Migration plan — yaas-triage restructure, then v3

Living document. Tick items as they land. Each step ends in a commit, so `git checkout <sha>`
is always the rollback.

**The rule that makes this safe: one class of change per step, verified and committed before
the next.** This is not generic caution. On 2026-08-06 a suite failed and the cause turned
out to be `mutations.sh` running concurrently rather than the change under test, which cost
twenty minutes of debugging a non-bug.

**A1 gate result:** 18/18 fast suites, 18/18 goldens, no stray state tree, and a LIVE
dispatch succeeded on the patched code (`gate_dispatch_success` 13:44:32Z).

**Standard gate after every step:**

```
link check → fast suites → 18 differential goldens → mutations → dry tick
                                     → real tick (only for steps touching dispatch)
```

Triage is paused for steps that move files, and restarted after the gate passes.

---

## Phase A — structure

### [x] A1. Repo-root resolution — **DONE** (2026-08-06, commit `4600d9e`)

Done *before* any move, while the tree was still flat, so it was a verifiable no-op.

**The hazard.** 16 scripts resolved the repo root by counting up two levels
(`Path(__file__).parent.parent`, `cd "$SCRIPT_DIR/.."`). That holds only while every script
sits directly in `yaas-triage/`. The moment one moves into a subdirectory it resolves to
`yaas-triage/` itself, and writes state into a parallel `yaas-triage/state/` tree nothing
reads. No crash: watermarks advance in a file nobody consults while the real ones sit still.

**The rule now.** The repo root is the nearest ancestor directory containing `yaas-triage/`.
Fails loudly if there is none.

- Rejected `CLAUDE.md` as marker: a fresh clone of the public mirror ships only
  `CLAUDE.example.md`.
- Rejected `.git`: this working tree has two git dirs, and test fixtures have none.
- **Ambient `$REPO_ROOT` is ignored.** Codex's call, and it was right: a stale value pointing
  at another *valid* checkout passes any marker check, so it cannot be told apart from
  deliberate redirection. Fixtures copy the whole tree, so walk-up already lands in the
  fixture. Named overrides (`YAAS_NOTIFY_REPO_ROOT`, `YAAS_ROTATE_REPO_ROOT`,
  `YAAS_HEALTH_REPO_ROOT`) remain for tests.
- Shell uses `pwd -P` so symlink semantics match Python's `.resolve()`, tests `/`, and
  returns non-zero rather than printing a guess.
- The helper is **inlined identically** in each file rather than imported: a shared module
  needs `sys.path` handling whose own path is depth-dependent, which is the bug being fixed.
  `tests/behaviour/repo-root.test.sh` asserts every copy is byte-identical.

**Files touched:** `ack-watch.py` `add-watch.py` `approval-helper.py` `checker-health.py`
`watch-guard.py` `slack-send.py` `run-agent.py` `dashboard-server.py` `client.py`
`notify.py` `rotate-logs.py` `health-monitor.py` `triage.sh` `doctor.sh`
`manual-dispatch.sh` `sync-yaas-v2.sh` `dispatch-agent.sh`

**Bugs introduced and caught during A1** (kept as a record of what the gate is for):
- `health-monitor.py` and `client.py`: the helper landed at column 0 *inside* a function,
  truncating its body. Both still **parsed**, and a "helper is at module level" check
  **passed**, because it was at column 0. `health-monitor.py` silently produced no output at
  all. Lesson: every patched script is now **smoke-run**, not just parsed.
- `test-path-references.sh` flagged itself: the comment excluding `test-repo-root.sh`
  contained a literal example path, which the checker extracted as a broken reference.

**New test:** `tests/behaviour/repo-root.test.sh`, 15 assertions — every depth, fixture isolation,
nearest-marker nesting, ambient `$REPO_ROOT` ignored, loud failure when orphaned,
byte-identical copies, shell and Python agreeing.

### [x] A2. The move — **DONE** (2026-08-06)

Pause triage first.

Target layout is documented in `ARCHITECTURE.md` §2 (delete its status banner in A3).

```
triage.sh  triage-loop.sh    the two entrypoints, nothing else at top level
checkers/                    unchanged — approval.py STAYS (see below)
dispatch/                    run-agent, dispatch-agent, manual-dispatch, format-stream,
                             translate-stream, extract-tokens, source-evidence,
                             spend-window, worker.mcp.json
ledger/                      ack-watch, add-watch, watch-guard, ensure-watch-ids,
                             approval-helper, checker-health
surfaces/                    client.py, mcp-call.sh, jira-call.sh, slack-react.sh,
                             slack-send.py
ops/                         health-monitor, heartbeat-loop, doctor, notify, rotate-logs,
                             dashboard-server, dashboard-start, sync-yaas-v2
setup/ skills/ tests/        unchanged
```

**Two placements that look wrong and are not:**
- `checkers/approval.py` **must not move.** `approval` is a live watch type and triage
  resolves checkers dynamically as `checkers/$type.py`. Moving it breaks every approval
  watch, and because the path is assembled at runtime there is **no literal string to
  substitute** — no find-and-replace would catch it. (Codex caught this; my original plan
  had it in `ledger/`.)
- `dispatch/spend-window.py` is not in `ledger/` despite dealing with cost. It only reads
  `run-log.ndjson` to decide whether to dispatch. It owns no file, so it fails the
  `ledger/` rule.

**What a text substitution of paths will MISS** (from Codex's review):
- `triage.sh` has many `$SCRIPT_DIR/<flat-name>` references needing a subdirectory.
- `dashboard-server.py` (→ `ops/`) locates `manual-dispatch.sh` as a **sibling**; it moves
  to `dispatch/`.
- `run-agent.py`, `dispatch-agent.sh`, `format-stream.py` and `worker.mcp.json` stay
  mutually valid only if moved **together**.
- The differential harness injects stubs at **root-level** `run-agent.py`, `mcp-call.sh`,
  `notify.py` (`tests/lib/scenario.py:269-271`), and its stub agent calls root-level
  `ack-watch.py` / `add-watch.py`. Unfixed, it would stub nothing and hit the **real** Slack
  seam.
- Several standalone tests flatten explicit `cp` lists into a fixture root.
- Editing a plist **template** does not touch what is already installed in
  `~/Library/LaunchAgents`. Reinstall and bootstrap; keep `WorkingDirectory` at repo root.
- `checkers/` imports work because `result.py` and `slack_utils.py` sit beside the checkers.

**A2 gate result:** link check 7/7 (94 references against new paths), 18/18 fast suites, no
stray state tree, 18/18 goldens, 4/4 mutations caught, and a LIVE tick dispatched a worker
and committed a watermark on the moved tree (`DISPATCH DONE ... exit 0` 14:04:21Z, fresh
`logs/worker-latest.*`).

**The goldens caught a harness break the suites could not.** First run after the move: 6
passed, 12 failed. The stubs landed in the right directories, but the stub AGENT's own
internal calls still used flat paths for `ack-watch.py` and `add-watch.py`, so it acked
nothing and every watermark held. Worth noting the mutation run in that state reported
"4 caught, 0 survived" — technically true and completely meaningless, because every scenario
was failing anyway. A mutation result is only informative when the baseline is green.

**What actually broke, and it was all one thing.** Eight suites failed, every one because its
fixture FLATTENED the tree into a single directory. That was a harmless little lie before the
move; afterwards it is a broken one, because scripts look for collaborators in sibling
directories (`surfaces/slack-send.py` -> `ledger/approval-helper.py`). Fixtures now mirror
the real layout, which is more faithful anyway: it exercises those cross-directory lookups.

**Three of Codex's warnings bit exactly as predicted:**
- The INSTALLED heartbeat plist still pointed at flat `heartbeat-loop.sh`. Editing the
  template does not touch `~/Library/LaunchAgents`. Fixed and re-bootstrapped.
- Fixture stubs at the wrong path meant `mcp-call.sh` was not found, so the health ping
  failed and the tick reported `SLACK DOWN` — every dispatch assertion then failed for a
  reason unrelated to what was under test.
- `checkers/*.py` had `MCP_CALL` fallbacks pointing at `checkers/mcp-call.sh`, which **never
  existed**; they only ever worked because `triage.sh` exports the variable. Now they point
  at `surfaces/`, so a standalone checker run works too.

**My own mistakes, for the record:** a blanket `$SCRIPT_DIR/<name>` rewrite also hit
fixture-internal invocation paths while the `cp` still landed flat, so the two disagreed; and
a `until grep <(tail -40 ...)` wait loop re-read a fixed window and spun for ten minutes
while the dispatch had already finished in 94 seconds.

Then: commit.

### [x] A3. Delete the `ARCHITECTURE.md` §2 status banner — **DONE** (2026-08-06)

Removed: §2 now describes the tree as it actually is. Also corrected the one forward-looking
reference inside it (it named `tests/behaviour/`, which is A4 and does not exist yet).

### [x] A4. Test reorganisation — **DONE** (2026-08-06)

```
tests/
├── unit/          mirrors the source tree 1:1, <name>.test.sh
├── behaviour/     named by FAILURE CLASS, may span files
├── differential/  18 goldens + mutations.sh
├── lib/           scenario.py, snapshot.py
├── coverage.sh    lists source files with NO unit test  ← the payoff
└── run-all.sh     must recurse (it currently globs test-*.sh in one directory)
```

Strict 1:1 is deliberately **not** the rule. The worst bugs so far were interaction bugs:
the rate-limit skip that starved approvals was about checker *ordering*; the approval lease
spanned three files; the commit predicate spans `triage.sh` plus `ack-watch.py` plus the
checker's verdict. A test named after one file has nowhere to live when it is about three.

`coverage.sh` needs a short **justified** exemption list (`worker.mcp.json` and `*.lag` are
config; `dashboard-start.sh` is fifteen lines that open a browser). A bare allowlist becomes
a place to hide gaps.

**Result:** 18 suites relocated (12 into `unit/` mirroring the source tree, 6 into
`behaviour/`), 18/18 pass, coverage reports 13 tested + 34 exempt-with-reasons + **0 with
none**.

**The depth problem again.** Every suite computed `SCRIPT_DIR` as `dirname $0/..`, which is
correct only at one depth. After the move they sit 2-3 levels down, so they now walk up to
the directory containing `yaas-triage/` — the same rule as A1. Counting `..` was the bug A1
removed from the scripts; re-introducing it in the tests would have been silly.

**The link check earned its keep.** It failed immediately after the move: 18 renamed suites
were referenced by name in `ARCHITECTURE.md`, `MIGRATION-PLAN.md` and 13 source-file
docstrings. All updated. Without that check those would have sat stale indefinitely, since
nothing else reads a docstring.

### [x] A5. Read the coverage report — **DONE** (2026-08-06)

It said exactly what was predicted. `coverage.sh` lists `triage.sh` as exempt with the
reason *"NOT unit testable inside 1,601 lines of shell — this is the argument for the port"*.
No shell unit tests were written for code that is about to be replaced.

Everything else is either unit-tested or exempt with a stated reason. The honest reading:
13 files have a direct unit test, and most exemptions are real coverage via a behaviour
suite or the goldens rather than an absence — but `triage.sh` itself, the single most
important file, is covered only end-to-end. That is the gap Phase C closes.

**Phase A is complete.**

---

## Phase B — close the live gaps before rewriting anything

Rewriting on top of known-broken behaviour bakes it in.

### [x] B1. Bounded ascending queries for `github_pr` and `jira` — **DONE** (2026-08-06)

**`drain()` turned out to be the wrong tool, and the right fix is much smaller.** `drain()`
parses Slack prose to find timestamps, so it is not reusable for row-based sources. But these
checkers get structured rows with timestamps, and that allows a far simpler move:

**Query ascending, bounded at the watermark.** Then the rows returned ARE the oldest rows in
the gap — a contiguous PREFIX by construction — with no slicing machinery at all. `gh search
prs --order asc` plus `updated:>=<iso>`; JQL `updated >= "..." ORDER BY updated ASC`.

That also rehabilitates the predicate: `len(rows) < limit` was never wrong as a test, it was
meaningless against an UNBOUNDED DESCENDING query where a full page told you nothing. Bounded
plus ascending makes a short page a real proof of coverage.

**The subtle part, which I got wrong first.** `complete` means "everything up to advance_to
has been seen", NOT "the whole gap is done" — `advance_to` bounds the claim, exactly as
`slack_utils.drain()` does for a covered forward slice (`slack_utils.py:160`). My first
version reported `complete: false` on a full page, which HOLDS the watermark and reproduces
the 14-hour stall with entirely plausible-looking code. Verified live: a 5-day-old watermark
with `limit=10` now commits 10 rows and advances, and the next tick takes the next 9. The
backlog drains monotonically.

**Deliberate asymmetry:** if a watch supplies its own `ORDER BY`, we no longer own the sort
and cannot claim a prefix, so the conservative page-cap rule still applies there.

**One case fails loudly instead of spinning:** a full page whose rows all share a timestamp
cannot advance past itself, so it errors and names the knob to raise.

New tests: `unit/checkers/github_pr.test.sh` (16) and `unit/checkers/jira.test.sh` (11).
Gate: 19/19 suites, 18/18 goldens, coverage 15 tested + 32 exempt + 0 unaccounted.

### [x] B2. Catch-up mode — **DONE** (2026-08-06)

**Two complementary defences, both now in place.**

1. *Per-message, always on* (`surfaces/slack-send.py`): any reply to a thread quiet longer
   than `YAAS_STALE_REPLY_HOURS` (24) goes to the approval queue. Makes a backlog **safe**.
2. *Per-gap* (`ops/catchup.py` + `triage.sh`): a gap over `YAAS_CATCHUP_AFTER_HOURS` (6) puts
   triage into a **hold**. It still checks every watch each tick, writes a digest of what
   accumulated, and sends/commits nothing until `catchup.py release`. Makes it **visible**.

Defence 1 alone would still produce dozens of drafts about resolved threads; defence 2 alone
would leave a trickle of stale sends after release.

**It holds clean watermarks too.** "Nothing sent, nothing committed" is the stronger promise:
advancing clean watches while holding dirty ones leaves a half-applied tick whose end state
depends on how far it got. Holding all of it makes release a clean resume from the pause.

**Three traps found while building it:**
- *Detection has to run before the tick writes anything.* The gap is measured from the newest
  run-log entry, and a tick appends to that log in six places. Detecting where the hold acts
  measured a gap of zero and could never fire.
- *Release could not clear the hold.* Immediately after release the gap is still a week, so
  the next `detect` re-armed it. Fixed with a `resume_after_epoch` marker that suppresses
  re-arming until a normal tick has logged something.
- *The digest was silently empty.* It read `DIRTY_WATCHES_NDJSON`, whose temp file is deleted
  right after the checks. It now reads column 7 of `TMP_RESULTS`, which also avoids changing
  a structure the goldens compare.

**A hold must never look healthy.** While held, ticks complete and nothing is dirty, so every
other condition reads fine and the agent would sit idle for days looking well.
`health-monitor.py` gained a `catchup_awaiting_release` condition that escalates with age and
names the remedy.

New suite: `behaviour/catchup-hold.test.sh` (25 assertions), in `behaviour/` because the hold
is only correct if `triage.sh`, `catchup.py` and `health-monitor.py` agree.
Gate: 20/20 suites, 18/18 goldens (verified stable across three runs — a hold can never arm
inside a fixture, since a fresh fixture has no prior activity to measure a gap against).

### [x] B3. The globally-applied Slack gate — **DONE** (2026-08-06)

Fixed: the gate now drops only the targets that need Slack, and dispatches the rest. Their
watermarks are untouched, so they retry next tick. A tick where EVERY dirty target needs
Slack still exits early, and a MIXED quest is still gated whole, since dispatch granularity
is the target.

The frozen golden was re-recorded — **the one intentional golden change so far**, and exactly
what the harness is for. The diff is the evidence: `planned` and `dispatches` went from `[]`
to `["q-mail"]`, and `q-mail/we` from `held` to `advanced_to_now` while `q-slack/ws` stayed
`held`. The scenario's `known_defect` marker is gone and its `why` now describes intended
behaviour.

### [~] B4. Small ones — **2 of 4 done** (2026-08-06)

**Done — `approval-helper.py` writes atomically.** It did `seek(0)` + `truncate()` + `dump`,
so a crash between the truncate and the write left an empty file and every pending approval
was gone, including drafts already reviewed. Now temp + fsync + `os.replace`.

That forced the lock onto a **sidecar** file: `os.replace` swaps the inode, so a lock held on
the data file becomes a lock on a deleted inode — a second writer wakes up, reads stale
contents through its old descriptor, and writes them back over the first writer's change.
Same lost-update trap already fixed once in `add-watch.py`. The five critical sections kept
their existing `try`/`finally` and `flock` calls untouched; only what they lock and how they
persist changed.

**Done — env knobs are validated.** `${VAR:-default}` only falls back on an EMPTY value, so
`YAAS_MAX_SPEND_6H=twenty` passed straight through and the numeric comparison it feeds
evaluated as *no cap*. A ceiling that silently switches itself off is worse than no ceiling,
because it looks present. Thirteen named knobs plus the `YAAS_MAX_SPEND_*` family are now
checked; a malformed one fails the tick with `gate_bad_env_knob` and exit 2.

**Dropped — `reactions.py` onto the result contract.** Recommended against after looking at
it. `reactions.py` never used the pipe contract: it reports through grep markers
(`REACTIONS_TRUNCATED=1`) and has no watermark, so `result.py`'s `advance_to` and `complete`
have nothing to bind to. The part that actually mattered — reporting truncation so reacted
messages cannot be silently missed — is already implemented. Converting it would be churn for
consistency, with regression risk on the reactions path and no safety gain.

### [~] B4d. Correlate acks against observed source reads — **SHIPPED OBSERVE-ONLY** (2026-08-07)

**What it does.** `dispatch/source-evidence.py sources` now returns the SET of Slack channels a
worker successfully read (it previously only answered a boolean "did any read succeed").
`ledger/ack-watch.py acked-as` returns items by exact status. `commit_quest` compares the two
and logs `gate_ack_unverified` when an item acked `nothing_to_do` has no observed read of its
channel.

**It does NOT hold anything** unless `YAAS_ACK_EVIDENCE_ENFORCE=1`. That default is a decision
after Codex review, and it overturned my original reasoning:

1. **My justification was wrong.** I argued a false negative costs "only one tick". It does
   not: three no-progress dispatches promote the watch to `misconfig`, which stops automatic
   retries entirely. Enforcing on imperfect evidence trades silent loss for eventual
   *manual-recovery* loss, which is a different failure, not a strictly smaller one.
2. **It is bypassable.** A worker can ack `handled` instead and skip the check completely.
   "handled leaves another trace" only holds if the commit code verifies that trace and ties
   it to this watch and run, which it does not.
3. **The false-negative surface is wide:** a `slack_search` whose channel appears only in
   results, a read by permalink or thread ts with no channel argument, a DM read by user id,
   any tool returning the channel only in its response, and Codex/Cursor event schemas this
   parser does not model.

So it observes and reports. Once live data shows how often evidence is genuinely absent,
enforcement is an informed switch rather than a guess — and the switch is already proven:
`ack_evidence_enforced_holds` records what enforcement does.

**Coverage is SLACK, CHANNEL-ATTRIBUTABLE ONLY.** Absence of `gate_ack_unverified` does not
mean every ack was verified. Email, Jira and GitHub acks are unchecked.

**Five new goldens** pin the behaviour and the limitations: evidence absent logs but advances;
evidence present is silent; a FAILED read is not evidence; `handled` is deliberately unchecked;
and enforcement holds.

**Still open** (Codex's remaining points, none of them blocking observation):
- Evidence should bind `{tool, channel, window}`, not just channel. Reading an unrelated old
  thread in the same channel currently counts as a false positive.
- If enforcement is ever switched on, the terminal state should be its own
  `evidence_unverifiable` rather than generic `misconfig`, which obscures that possibly-unread
  Slack content is still pending.
- `handled` needs its own action-receipt check before this gate means much.

## Phase C — v3

### [x] C1. Scope — **DECIDED** (2026-08-06)

**Orchestrator only.** That means `triage.sh` and nothing else: 1,711 lines, 1,119 of code,
29 sections. `checkers/`, `ledger/`, `surfaces/`, `ops/` and `dispatch/` are NOT touched. They
are the best-tested code in the repo and their bugs are already paid for.

#### Rules that protect what already works

Binding, not aspirational.

1. **Nothing is deleted.** v3 is a NEW file (`tick.py`) beside `triage.sh`. Both stay on disk
   through the port and well past cutover.
2. **`triage.sh` is frozen to bug-fixes only** while the port runs. It is the reference the
   goldens were recorded from, so any change to it means re-recording them AND re-running v3
   against the new ones. A casual edit is not free.
3. **Cutover is one line** in `triage-loop.sh`; rollback is reverting that line. No file
   moves, no deletions, no state migration — v3 reads and writes the same state files.
4. **Shadow first.** v3 runs `DRY_RUN=1` beside the live shell for several days and its
   decisions are diffed against what the shell actually did. Cut over only when they agree.
5. **`triage.sh` is deleted only when** v3 has run live without incident for an agreed period
   AND you say so explicitly. Nothing is scheduled for deletion.
6. Every step is committed, so `git checkout <sha>` returns to a known-good tree.

### [x] C1a. Close the golden coverage gap — **DONE** (2026-08-07)

**The 18 goldens are necessary but NOT sufficient as a port gate.** Measured, not assumed:

| triage.sh behaviour | golden coverage |
|---|---|
| dispatch, ack, commit, watermark movement | thorough |
| Slack health gate | yes |
| retire stale `slack_thread` watches | **none** |
| retire completed `approval` watches | **none** |
| retire fired one-shot `schedule` watches | **none** |
| prune reaction state / worker logs / manifests | **none** |
| fairness rotation (`dispatch_cursor`) | **none** |

Only 8 distinct event names appear across all 18 goldens, so `gate_catchup_hold`,
`gate_bad_env_knob`, `gate_skip_locked`, `gate_watch_misconfigured` and
`gate_budget_exceeded` are unrepresented too.

The retire rules are the dangerous omission because they **delete watch entries**. A port that
drops one grows `watch.json` without bound; one that over-applies it silently stops tracking a
live customer thread. Neither would fail a single existing golden.

**Closed.** Six scenarios added (24 goldens total) and five matching mutations, so the gap is
covered by tests that provably detect a regression rather than merely existing:

| new scenario | protects |
|---|---|
| `retire_stale_thread` | 30-day default retires an old thread, keeps a fresh one |
| `retire_respects_never` | `never` keeps an ancient thread (partner conversations) |
| `retire_respects_custom_window` | a per-quest window is honoured both ways |
| `retire_completed_approval` | terminal approval retired, pending one kept |
| `retire_fired_one_shot_schedule` | fired one-shot retired, recurring cron survives |
| `fairness_rotation` | dispatch order rotates from the persisted cursor |

All 9 mutations caught, 0 survived.

Prune rules (worker logs, manifests, reaction state) remain uncovered. They are lower risk:
they delete LOGS rather than watches, so a mistake costs disk or history, not tracking.

### [~] C2/C3. The port — in-place pure cores live, `tick.py` modules building (2026-08-07)

**Phase 1 — pure cores extracted in place and wired into the live `triage.sh`** (each a pure,
unit-tested, Codex-reviewed-where-risky module; behaviour-verified by the goldens, committed):

| Module | Owns | Live in triage.sh |
|---|---|---|
| `ledger/commit.py` | the commit predicate (which watermarks move) | ✓ |
| `dispatch/plan.py` | fairness rotation + per-target breaker | ✓ |
| `ledger/housekeep.py` | the three retire rules | ✓ |

This tightened `triage.sh` but did NOT shrink it much (~1,800 → ~1,700): in-place extraction
moves logic out but leaves the call site. A small `triage.sh` needs the full rewrite below.

**Phase 2 — the genuine `tick.py` rewrite: new orchestrator modules, one per phase.** These are
NOT wired into the live shell; they are the replacement being built and held to the SAME 30
goldens as `triage.sh` (`run.sh check tick.py`), so the port is behaviour-preserving by
construction. Each is pure where the logic lives and thin where it only sequences:

| Module | Phase it replaces | Status | Commit |
|---|---|---|---|
| `tick_state.py` | config/loading: repo root, knobs (refuse-on-garbage), lag map, sorted quests | done, 12 tests | `a16f3ac` |
| `tick_check.py` | analyze: the six-way `classify()` verdict (misconfig/backoff/skip/hold/dirty/clean) + parallel fan-out | done, 21 tests | `e02d74c` |
| `tick_dispatch.py` | dispatch gates: `slack_gate` (per-target Slack-need) + `slice_plan` (budget/fanout/MIN_SLICE) | done, 14 tests | `571c723` |
| `tick.py` | the main flow that sequences all of the above + commit | **done: 30/30 goldens, 9/9 mutations** | `cf73ebf` |

Porting the analyze phase into `tick_check.py` surfaced a **latent live bug** (`e02d74c`): the
tie-safety fix (B1) made `github_pr`/`jira` emit `outcome=hold`, but the shell's analyze case
recognized only clean/dirty/ratelimited/error/misconfig, so a real busy-repo hold was mismapped
to error → backoff → misconfig. Fixed in both `classify()` and the live shell; a new
`checker_emits_hold` golden pins it end to end. This is the payoff of porting carefully rather
than mechanically.

**Step 4 done (`cf73ebf`).** `tick.py` is the assembled orchestrator: 30/30 goldens, 9/9
mutations, byte-identical decisions to `triage.sh`. It is NOT wired live — `triage-loop.sh`
still runs `triage.sh`.

**Remaining: the cutover — the one step that needs a human present (rules 3/4).** Sequence:
1. **Shadow.** Confirm `tick.py` agrees with `triage.sh` on LIVE state, not just fixtures. The
   safe way is a read-only comparison (a `--plan-only`/dry mode that prints the decision without
   writing watch.json / dispatching), run beside a real tick and diffed. A plain `tick.py` run
   against the live repo is NOT safe to shadow with — its check phase advances clean watermarks,
   calls checker-health, and housekeeps, all mutating live state.
2. **Flip.** Point `triage-loop.sh` line 48 at `tick.py`, kickstart the loop (the KeepAlive
   loop must be re-kickstarted after any loop edit — see [[project_triage_keepalive_loop]]), and
   watch the first several ticks live.
3. **Keep the shell.** Do not delete `triage.sh`; leave it as the instant rollback until
   `tick.py` has run clean for a stretch. Only then reduce `triage.sh` to a thin driver or
   retire it.
The cutover is the user's call and a supervised action, not an overnight/unattended one.

## Phase R — reaction path hardening (2026-08-07, unplanned, from live incidents)

Three live issues surfaced and were fixed with the same discipline (unit test + Codex on the
risky one + full gate):

- **R1. Stale-reply guard read the wrong param.** It called `slack_read_thread` with
  `thread_ts` instead of `message_ts`, so the read raised and the guard failed CLOSED — silently
  holding EVERY threaded reply as "unreadable". Fixed + static regression assertion. (`2de5e37`)
- **R2. Reaction lifecycle was unreliable prose.** `trigger -> claudeloading -> updatedone` was
  hand-composed remove+add pairs, unlogged, decoupled from the state file, so it drifted. Now
  one atomic logged verb `surfaces/react-lifecycle.py advance`; CLAUDE(.example).md call it.
  (`b5374db`, `421c3f6`)
- **R3. Reaction approvals could never self-execute.** quest_id="reactions" has no quest folder,
  so the approval watch never armed and reviewed drafts stranded. Routed to a durable
  executor-only host quest. (`ec3db9e`)


## Do not

- Run Phase A and Phase B in parallel. B changes behaviour, A changes only structure; mixing
  them produces a golden diff you cannot attribute.
- Start v3 before Phase B. You would port the bugs.
- Re-record goldens to make a failure go away. That converts the harness into decoration.
  `record` is for **intended** changes, and the golden diff is the reviewable evidence.
- Commit `CLAUDE.md`. Public-facing changes go to `CLAUDE.example.md`.

## Outstanding, not blocking

- The public mirror commit (`.git-yaas-v2`, `a219c7e`) is **local**. Pushing publishes it.
