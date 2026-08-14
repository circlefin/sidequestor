# Remediation plan: architecture review @ 309aab8

Accuracy audit: `claude-verification.md`. Source: `source-review.html`.
Reconciled with the Codex plan on 2026-08-14 per `codex-claude-diff.md`; the adjudication of the
two plans' disagreements is at the bottom under **Where the two plans disagreed**.

Sequencing principle, in the order asked for:

1. **Stage 1** quick fixes and bug fixes. Nothing structural. Ships on its own.
2. **Stage 2** the architecture changes that are easy, safe and mechanical, and that delete the
   most code per unit of risk. All pure functions, no behaviour change.
3. **Stage 3 onward** the deep changes that need design judgement.

Stages are independently shippable and in dependency order. Within a stage, items are unordered
except where noted.

---

## Stage 1 · Quick fixes and bug fixes (~1 day)

Goal: no dead UI, no double render, no silent 404. Deliberately no refactor, so this lands fast
and gives Stage 3 a green suite to move against.

**1.1 Characterization tests first.** Before changing any behaviour, pin what the approval
lifecycle does today: every `(status, action)` pair `_update_approval` currently accepts or
rejects, and what each of the four surfaces renders for each status. Half a day, and it is what
makes every later stage safe. Do this before 1.2 through 1.6.

**1.2 Commit the `edit` route.** It already exists in the working tree, uncommitted. Land it
with a test rather than leaving it as a loose local patch.

**1.3 Give `stalled` a producer.** `_approval_card` emits `stalled: bool`, computed from
`lease_expires_at`. Inline for now; Stage 3 moves the predicate into the shared module. Until
this lands, the whole reclaim UI at `dashboard.html:854-869` is unreachable code.

**1.4 Make the partition mutually exclusive.** `dashboard-server.py:1079-1089` and `:1153-1166`
both emit `reviewed` and `executing` items, so they render as a carousel card and a strip row at
once. Assign each non-terminal status to exactly one surface:

| status | surface | why |
|---|---|---|
| `pending_review` | review carousel | reviewer must act |
| `needs_reply` | review carousel | see the note below, this one is deliberate |
| `executing`, live lease | in-flight strip, read only | a worker is mid-run |
| `executing`, expired lease | recovery surface (carousel, reclaim control only) | nobody is coming |
| `reviewed` | in-flight strip, read only | approved, awaiting the worker |
| `executed`, `cancelled` | neither | terminal |

`needs_reply` stays reviewer-actionable on purpose. `_update_approval` defaults to
`from_status=("pending_review", "needs_reply")` and the comment at `dashboard-server.py:191-194`
says the reviewer must be able to act on either, because "the reviewer's explicit action still
wins". It is simultaneously dispatched to the worker (`checkers/approval.py:94`). It is not a
third bucket, it is a pending item that also has a worker coming. Moving it into the read-only
strip would remove a capability the code deliberately has.

**1.5 Split `undo` and `reclaim` into two endpoints.** Not one, and `undo` must not reach into
`executing`. This replaces the original 0.1 of this plan, which was wrong:

- `/api/undo/<id>`: legal from `reviewed` and `cancelled` only. Returns `409` once a worker has
  advanced the item. This is the 6 second toast, and it is a genuine undo.
- `/api/reclaim/<id>`: legal from `executing` with an expired lease. Puts the item back for
  review flagged `needs_reconcile`, so the worker reads the thread and looks for the message
  before doing anything.

`checkers/approval.py:96-99` states the rule outright: an expired claim means "the send may or
may not have landed... the worker's job then is to reconcile (read the thread, look for the
message) and NOT to blindly resend." A generic undo from `executing` to `pending_review` sets up
exactly the blind resend that comment forbids. The client already routes reclaim through the
undo endpoint at `dashboard.html:950`, so this is a client change too.

**1.6 Route/action parity contract test.** Assert that the set of actions the server declares
legal equals the set of paths `do_POST` routes, and add one endpoint behaviour test per action.

Do **not** implement this as a grep for `fetch('/api/...')` literals, which was this plan's
original proposal. It would have missed the single most important case:
`dashboard.html:950` builds its path as `` `/api/${endpoint}/${id}` `` from a ternary, so a
literal scrape sees nothing. Test the contract, not the JavaScript source text.

---

## Stage 2 · Easy, safe, mechanical simplification (~1 day)

Every item here is a pure function extraction with no behaviour change and an obvious test. High
deletion-to-risk ratio. Each ships independently and none depends on Stage 1.

**2.1 `structural_verdict(...)` in `tick_check.py`** (candidate 7). One pure function returning a
verdict or `None`. `tick.py:304-306` becomes "if `None`, exec the checker"; `classify()` calls it
as its own prologue. The four conditions and their order are currently written twice, and the
comment at `tick.py:301-302` already documents the duplication as intentional. Smallest, safest,
most obviously correct change in the whole plan. Start here.

**2.2 One retry-time predicate, `is_due(rec, now_ts)`, in `tick_check.py`** (candidate 4,
narrowed). Replaces five hand-rolled `next_retry_ts` float-parse comparisons at `tick.py:274`,
`tick.py:293`, `checker-health.py:175`, `dashboard-server.py:975` and `dashboard-server.py:1387`.
One unit test covers the parse tolerance for `0`, `""` and garbage.

**Do not** also add a generic `apply_backoff(rec, now_ts, promote)`, which this plan originally
proposed. There are two deliberately different ladders and merging them hides that:

| ladder | base | cap | promote threshold |
|---|---|---|---|
| `checker-health._backoff_for` | 60s | 3600s (`MAX_BACKOFF`) | none |
| `tick.unacked_backoff_for` | 300s (`UNACKED_BASE_BACKOFF`) | 86400s (`UNACKED_MAX_BACKOFF`) | yes |

Share the time predicate, keep two named policy functions.

**2.3 Collapse the two byte-identical unacked write blocks.** `tick.py:754` and `tick.py:1174`
are the same write. One helper, called twice. Independent of 2.2 and just as safe.

**2.4 GitHub checker collapse** (candidate 3). `checkers/github.py` (non-executable, so the
executable-bit discriminator keeps working) holds the doctrine: tie safety, prefix-not-suffix
bounding, boundary re-filter, `gh` exit taxonomy. `github_issue.py` and `github_pr.py` shrink to
executable adapters supplying `(noun, json_fields, preview_fn)`. Use the adapter shape the
`chore/tidy` branch gives the four Slack checkers so both land on one convention.

Keep the type-specific safety tests where they are. `tests/unit/checkers/github_issue.test.sh:18`
says the file was copied from `github_pr.py` and therefore inherited the 2026-08-05 stall fix
(unbounded DESCENDING query, 14 hours parked). That fix stops living in two copies here. Add a
shared adapter-contract test on top.

This is ~150 lines deleted for half a day and effectively zero design risk, which is why it is in
Stage 2 rather than last.

---

## Stage 3 · One definition of the approval state machine (2 to 3 days)

The architectural fix behind Stage 1. Needs design judgement, so it goes after the mechanical
work, not before.

**3.1 `yaas-triage/approval_state.py`, pure and importable.** Holds `TRANSITIONS:
dict[(status, action), updates]`, `is_stalled(item, now)`, `LEASE_MINUTES`, and
`available_actions(item, now)`.

It must be a new importable module, **not** `ledger/approval-helper.py` as this plan originally
said. That file is hyphenated and is invoked purely as a subprocess by every consumer
(`tick.py:496`, `dashboard-server.py:1718`, `surfaces/slack-send.py:213`). It cannot be imported
by the dashboard, the checker and the health monitor without an `importlib` hack. The helper
stays as the CLI adapter over the new module; this also stops HTTP routing concerns leaking into
the ledger CLI.

**3.2 A separate store module** owning the flock plus temp/fsync/replace discipline for
`pending-approvals.json`. There are three independent implementations today. Pure rules and
locked storage are different jobs and should not share a file.

**3.3 Derive the route list from the table.** `do_POST` stops hardcoding action names. Adding a
transition can no longer produce a dead client button, which retires 1.6's contract test from
"catches a bug" to "pins an invariant".

**3.4 Server emits `available_actions`.** The client renders the buttons the server says are
legal instead of recreating the status-to-action table. `switch(item.status)` still selects
visual treatment. This is a stronger version of the source review's proposal: a status switch
alone still leaves the client deciding which actions a status permits, which is the exact split
brain that produced the reclaim and undo defects.

**3.5 Move the `"?"`-in-note heuristic into the table.** `dashboard-server.py:1775-1783` silently
rewrites a `review` into a `needs_reply`. That is a transition rule living in an HTTP handler.

**3.6 `is_stalled` gets its single call site set.** `checkers/approval.py:101`,
`ops/health-monitor.py:199` (drop the `executing_at` / `APPROVAL_STUCK_MIN` fallback), and
`_approval_card`, which closes 1.3 properly. Candidate 5 falls out of this for free.

**Exit criterion:** no status literal in `dashboard.html` outside a render switch, and no
transition rule anywhere in `dashboard-server.py`.

---

## Stage 4 · Watch-type manifest (2 days)

Candidate 2, the highest-leverage structural change, but it pays off on the *next* watch type
rather than today, which is why it is last. Extends the pattern `tick_state.load_lag_map` already
proves.

**Revised 2026-08-14 after Stages 1 to 3.** Line numbers re-verified at `5684086`. The earlier
version of this section had three wrong paths and missed the fixture cost entirely.

### 4.0 Pre-flight, in this order, before writing anything

1. **Have Codex review this brief before executing it.** Stage 3 cost a full revert because I
   briefed a design that violated a documented invariant. Its review of that brief raised seven
   findings, all of which verified correct. Make this a standard step now.
2. **Enumerate the fixture cost.** Any consumer that gains a new `import tick_state` breaks every
   minimal fixture that copies it. Only `tick.py` imports `tick_state` today, so **every other
   consumer in 4.3 is a new dependency**. Grep for both `$HERE` and `$SCRIPT_DIR` copy patterns;
   the Stage 3 list was 14 files because I originally grepped only one spelling.
3. **Check for isolation-sensitive consumers.** `ops/health-monitor.py` must never import a triage
   module (see finding 3 below and the Stage 3 record). It does not enumerate watch types today, so
   it should not appear in 4.3 at all. If a manifest consumer turns out to be a dead-man component,
   pin parity with a test rather than sharing code.

### 4.1 The sidecar

Add `checkers/<type>.watch.json` beside the existing `<type>.lag`, carrying `schema_version`,
`required`, `identity`, `example_entry`, `open_loop`, `user_creatable`, `upstream`.

`required` cannot be a flat list. `ledger/add-watch.py:89` declares `schedule` as `()` with the
comment "needs cron+tz OR next_fire_ts; checked below", so the schema needs an any-of form.

`approval` needs `user_creatable: false` — it is in `REQUIRED` but is runtime-only and quest
creation should not offer it.

**Model the subset fields on Stage 3's `http` flag.** `dashboard-server.py:563` holds
`_OPEN_WATCH_TYPES = {"slack_thread", "slack_channel", "slack_dm", "slack_mention", "email"}`,
which is a *semantic subset*, not the full type set. Stage 3 solved exactly this shape with a
per-entry `{"http": True|False}` flag plus a named tuple of the subset, and that worked. Do the
same: `open_loop` and `user_creatable` are per-type booleans, and each consumer reads the subset it
needs. Do not let a consumer re-derive a subset by listing type names.

### 4.2 The loader

`load_watch_manifests()` in `tick_state.py`: one glob, mirroring `load_lag_map` at
`tick_state.py:143`. No import from the checkers, so the standalone-CLI doctrine survives. Validate
on load, and assert a checker-to-manifest bijection.

Consumers outside `yaas-triage/` reach it the way Stages 2 and 3 established: the existing
byte-identical `_repo_root()` walk-up, then `sys.path.insert`. **Do not share `_repo_root` itself**
(`add-watch.py:66` carries the comment explaining why; `repo-root.test.sh` pins it byte-identical).

### 4.3 Repoint the executable consumers

Corrected paths and line numbers at `5684086`. There are eight sites, not six:

| site | what it declares |
|---|---|
| `ledger/add-watch.py:81` | `REQUIRED` |
| `ledger/add-watch.py:94` | `IDENTITY` |
| `skills/yaas-quest-creation/new-quest.py:109` | required fields (**note: under `skills/`, not `ledger/`**) |
| `ops/dashboard-server.py:563` | `_OPEN_WATCH_TYPES` (the open-loop subset) |
| `tests/lib/scenario.py:294` | the type set for fixtures |
| `tests/behaviour/checker-contract.test.sh:170` | the type set for the contract loop |
| `tests/behaviour/checker-contract.test.sh:70-73` | a sample entry per type (feeds `example_entry`) |
| `tick_state.py:143` | `load_lag_map`, the glob to mirror (leave; extend beside it) |

`new-quest.py` lives at `skills/yaas-quest-creation/new-quest.py`, which is one level deeper than
the earlier draft of this plan assumed. Its `sys.path` bootstrap needs the walk-up, not a relative
hop.

The manifest must be a declared file, not inferred from a glob of executables: `checkers/cron-due.py`
and `checkers/reactions.py` are executable but are not watch types. Note the filename is
`cron-due.py` with a hyphen, not `cron_due.py` as the source review wrote it.

Current counts to sanity-check the bijection against: 5 `.lag` files, 12 checker `.py` files of
which `result.py`, `slack_utils.py` and `github.py` (new in Stage 2.4) are non-executable helpers,
leaving 9 real watch types plus `cron-due.py` and `reactions.py` as executable non-types.

---

## Stage 5 · Optional, only when they earn it

**Revised 2026-08-14 after Stage 4, verified at `d54c025`.** The two items have moved in opposite
directions. 5.2 got easier and is now worth doing. **5.1 got harder and should probably never be
done in the form originally proposed.**

### 5.1 Replace `startswith("slack_")` with `upstream` — recommend NOT doing this

**Six** sites, not five: `tick.py:256`, `:257`, `:258`, `:340`, `:352`, **and
`tick_dispatch.py:66`**, which uses the same prefix rule to decide "does this tick need Slack".
Earlier drafts of this plan and the source review both counted only the `tick.py` five. The
`upstream` field now exists and is populated (`slack` ×4, `gmail`, `github` ×2, `jira`, `null` ×2),
so the data is there. The problem is what consuming it would cost.

**It directly contradicts Stage 4's finding 3.** Stage 4 deliberately kept `load_watch_manifests`
out of `Config.__init__` and out of module import, and left `tick.py` completely untouched,
because `tick.py:71` builds `Config` on every tick: loading manifests there makes **every tick**
hard-depend on ten manifest files being present and valid, and breaks the partial-`checkers/`
fixtures. Those five `startswith` sites are all inside the tick's hot path. Consuming `upstream`
there means reversing that decision.

The trade is bad in both directions:

- Reversing it buys nothing today. The prefix is correct for all four Slack types, and no second
  rate-limited upstream exists.
- It costs the tick a hard dependency on the manifest layer, for a grouping that is currently a
  five-character string comparison that cannot fail.

**If a second rate-limited upstream ever arrives**, do it as: load the manifests once at the top of
the tick, pass the `{type: upstream}` map down as a parameter, and **fall back to the prefix when
no manifest is present** so a partial tree still runs. Pin the current grouping with a
characterization test first. Do not make `Config` load manifests.

**Do not conflate this with the `"slack"` source name.** `tick.py:369`, `:1236`, `:1323` use
`"slack"` to mean the *dispatch source* for recovery evidence, not a watch-type group. Different
concept, same word. Leave those alone.

### 5.2 Documentation contract tests — now worth doing

Stage 4 materially improved the case for this: `checkers/*.watch.json` is now a machine-readable
source of truth to assert against, which did not exist when this item was written.

Assert, do not generate. **Scope each assertion to the projection that table actually claims to
list**, because the tables are deliberately not all the same set:

| doc site | asserts against |
|---|---|
| `README.md:194-195` | all ten manifest stems |
| `ARCHITECTURE.md:223-226` | all ten manifest stems |
| `yaas-ops/SKILL.md` **:112 AND :136** | **every `.lag` file and its value**, not one line's subset |
| `yaas-quest-creation/SKILL.md` | **mixed contract**, see below |

**The `.lag` documentation lives in two places, not one.** `:112` lists `email=120`,
`slack_mention=90`, `github_pr=30`, `jira=15`; `:136` separately documents
`github_issue.lag = 30`. There are five `.lag` files. A test scoped to `:112` alone would pass
while ignoring `github_issue` entirely, which is exactly the drift it is supposed to catch.
Assert **all five files and their values across both sites**.

Asserting values rather than names is right here. They are operational constants already checked
into the repo; if changing one forces a doc update, that is the point. The brittle version would
be scraping prose paragraphs, not pinning an explicit value list.

**Quest creation is a mixed contract, not a pure `user_creatable` subset.**
`yaas-quest-creation/SKILL.md:45` deliberately documents `approval` as runtime-only with a caveat
("Do not put one in a creation spec"). A test asserting only the `user_creatable: true` set would
leave that entry unprotected. Assert two things: the offer set equals `user_creatable: true`, and
`approval` is present and marked runtime-only.

Leave genuinely judgement-heavy prose alone: `yaas-quest-dispatch/SKILL.md` worker-operating
guidance, and the checker-authoring meta-table's advice. A generator cannot write those.

### 5.3 Manual prose cleanup — Stage 4 left factually stale instructions

Not a contract test; a correction list. Stage 4's brief said "leave prose alone, Stage 5 covers
documentation contracts", and that was my call. The consequence is that some prose now names
symbols that no longer exist, so it misinstructs rather than merely drifting:

- `yaas-quest-creation/SKILL.md:47` says to register a new type "in `REQUIRED_FIELDS`".
  **`REQUIRED_FIELDS` no longer exists**; `new-quest.py:122,129` now imports and calls
  `load_watch_manifests`. Anyone following that instruction adds a symbol nothing reads.
- `yaas-checker-authoring/SKILL.md:186` points at "the header docstring listing fields per type"
  in `new-quest.py` as a registration site. That is now the manifest.

Fix these by hand as part of Stage 5. They are wrong in a factual way, not a judgement way, so
"leave prose alone" does not apply.

This replaces the `grep -rn "<nearest_sibling_type>"` instruction at
`yaas-checker-authoring/SKILL.md:209-212` with a failing test, which was the whole point of
candidate 9.

**Sequencing note.** 5.2 is a test-only change with no runtime risk, so it does not need the
brief-review round trip that Stages 3 and 4 needed. It is a good candidate for a small, direct
change rather than a full delegated stage.

---

## Final holistic review (2026-08-14, HEAD `da3390c`)

Codex reviewed the whole body of work adversarially. Its candidate scoring was more honest than
ours and is worth keeping:

- **Addressed:** 1, 2, 3, 4, 7.
- **Partially addressed:** 5 (`is_stalled` is shared by triage, dashboard and checker, but the
  dead-man monitor deliberately re-implements it, so "one definition" is not literally true);
  6 (the state machine and store moved out, but the HTTP mutation handler still lives in
  `dashboard-server.py`).
- **Narrowly addressed:** 9 (the four selected doc tables are under contract; not all
  maintainer-facing prose is).
- **Deferred in code:** 8, deliberately.

**One live defect it found, and it was larger than reported.** The checker-authoring skill still
told authors to register a type in `add-watch.py`'s `REQUIRED` and `IDENTITY` tables, which Stage 4
deleted. Checking the rest of that table, **four of its seven rows were stale**:
`add-watch.py` ×2, `tests/lib/scenario.py`'s type list, and `checker-contract.test.sh`'s
`entry_for`, all now manifest-derived. The table opened with "these are separate lists, none of
which is derived from the others" — which *was* candidate 2's complaint. The document describing
the shotgun surgery outlived the surgery. Fixed in `675cfce`.

**Its sharpest line, and a fair one:** "the codebase claims 'one declaration per type' more
strongly than it actually delivers", because `tick.py` and `tick_dispatch.py` still group Slack
types by name prefix. The deferral stands, but the exception is now documented in the skill rather
than implied.

**Risks it flagged that tests will not catch:**

- Manifest corruption now takes down the dashboard's open-items and quest-detail paths, which call
  `load_watch_manifests()` at request time and fail closed by design.
- Drift between `approval_state.is_stalled` and `health-monitor` is prevented by a parity test, not
  by structure.
- The fixture tax is real and recurring: standalone CLIs now depend on more shared modules, and
  Stage 4 already showed how easily one fixture gets missed.

**Highest-value work remaining, per the review:** finish the watch-type registry boundary. Either
load a single `{type: upstream}` map once at the tick entrypoint and inject it, or codify
prefix-based Slack handling as permanent doctrine with a test for the exemption.

---

## Explicitly not doing

- **Splitting the read path out of `dashboard-server.py`** (candidate 6). The four zero-argument
  projections are genuinely deep and share `_link_fields` / `_norm_channel` / `_approval_card`.
  Only the write path moves, and Stage 3 already moves it.
- **Splitting `tick.py`.** 1,496 lines is not the problem. `have_network`, `dispatch_loop` and
  `commit_quest` are impure sequencing and correctly live there.
- **Touching `tick_state.load_lag_map`, the executable-bit discriminator, `reaction_config.py`,
  or `dispatch/slack-read-health.py`.** All correctly deep.
- **A doc generator for the prose type tables.** Trades prose drift for template drift plus a
  build step, and loses the judgement content.
- **A single generic `apply_backoff`.** See 2.2.

---

## Sequencing summary

| Stage | Content | Effort | Risk | Status |
|---|---|---|---|---|
| 1 · quick + bug fixes | dead endpoints, double render, unreachable UI, parity test | ~1 day | low, but 1.5 needs care | **DONE** 2026-08-14, `bc953ab` |
| 2 · easy safe simplification | 4 pure extractions, ~172 lines deleted | ~1 day | very low | **DONE** 2026-08-14, `d48e07c`..`e9bcc22` |
| 3 · approval state machine | importable pure rules + store + server-authored actions | 2 to 3 days | medium, design work | **DONE** 2026-08-14, `7ea4818`..`b51565d` |
| 4 · watch-type manifest | sidecar + glob + repoint 8 consumers | ~2 days | low, wide | **DONE** 2026-08-14, `a8a7ff2`..`76aa938` |
| 5.1 · consume `upstream` | reverses Stage 4's tick isolation | deferred | **recommend NOT doing** |
| 5.2 · doc contract tests | assert docs against the manifests | ~0.5 day | very low | ready, worth doing |

Stage 3 is the one that still pays for itself. Stage 4 pays off on the next watch type.

---

## Execution record

Stages 1 and 2 were architected here and executed by Codex against
`claude-stage-1-brief.md` and `claude-stage-2-brief.md`. **`.git-yaas-v2` is the git tree of
record for this repo**, not the private `.git`.

| Commit (`.git-yaas-v2`) | Contents |
|---|---|
| `bc953ab` | All of Stage 1, squashed, plus Codex's follow-up fixes (below) |
| `d48e07c` | Stage 2.1 `structural_verdict`, 2.2 `is_due`, 2.3 collapsed unacked write |
| `9f8d21a` | Stage 2.4 GitHub checker collapse, 556 lines to 384 |
| `e9bcc22` | Fixture fix, see finding 2 |
| `474fd5e` | Codex: the seventh fixture, `tests/contract/dashboard-routes.test.sh` |
| `7ea4818` | Stage 3.0, route-parity test moved into `behaviour/` so it actually runs |
| `b51565d` | Stage 3.1 to 3.7, the approval state machine |
| `5684086` | Codex: reconciliation close path + corrupt-store handling (below) |
| `a8a7ff2` | Stage 4.0, one canonical schedule rule |
| `76aa938` | Stage 4.1 to 4.5, the watch-type manifest |
| `02d0c52` | this review record |
| `d54c025` | two consumers Stage 4 left behind (below) |

Final state after Stage 3: **39 of 39 suites pass** (39 not 38 because the route-parity test now
runs), `run-all.sh` exits 0, differential 29 passed / 0 failed, no golden file edited.

### Stage 3 was attempted twice

The first attempt was **reverted to `474fd5e` and restarted**. Codex reviewed my Stage 3 brief
before executing and raised seven findings; I verified all seven against the code and all seven were
correct. Two would have caused real damage:

- **My 3.7 told it to make `ops/health-monitor.py` import `approval_state` and drop its independent
  fallback.** That file is a dead-man switch (`health-monitor.py:20-36`): its own launchd job,
  "shares no code path with the triage loop", "deliberately dependency-free", because "a health
  check living inside triage cannot detect triage being dead". Two multi-hour silent outages
  motivated it, one a stray `.pth` that crashed every tick for 6.5 hours. The import was already in
  the working tree when I paused. Now excluded, with parity pinned by a test.
- **Calling `TRANSITIONS` "the state machine" while listing only six dashboard actions.** The worker
  also drives `start`, `answer`, `done`, `abandon`, `auto_cancel`. Combined with 3.5 deriving routes
  from the table, internal worker transitions would have become HTTP-reachable.

The other five: a static dict cannot express payload-dependent transitions (now
`apply_transition(item, action, payload, now)`); validation must be inside the lock callback, not
check-then-write; my fixture survey was 9 files when it was 14 (I grepped `$HERE` and missed every
`$SCRIPT_DIR` fixture); the "no status literal in dashboard.html" exit criterion was too broad and
now prohibits client-side action-eligibility rules instead; and finding 7 below.

**Lesson: have the executor review the brief before executing it.** This cost one revert and saved
a wrong foundation. Worth doing for Stage 4.

### Correction to finding 1 above

**`run-all.sh` does NOT swallow failures.** It ends with `[ "$FAIL" -eq 0 ]` and exits non-zero
correctly; verified empirically. My earlier claim came from reading a *backgrounded wrapper's* exit
code, where the 0 belonged to a trailing `grep`, not to the suite. I repeated the wrong claim in
three briefs and a memory file; all are corrected. Codex's "reports done on a red suite" behaviour
is still real and still means verify independently.

### Codex's post-Stage-3 pass (`5684086`) closed two more gaps

Both worth recording because both were holes in *my* design, not sloppiness in the execution:

- **The reconciliation protocol was unexecutable.** Stage 1.5 sets `needs_reconcile` on reclaim and
  returns the item to `pending_review`; the `yaas-quest-dispatch` skill then tells the worker to
  close it with `done` if the send already landed. But `done` was legal only from `executing`, so
  that path could not actually be taken. `("reviewed", "done")` and `("reviewed", "abandon")` are
  now legal **and gated on `needs_reconcile`**, so the shortcut exists for reconciliation only and
  cannot be used to skip execution normally.
- **A corrupt queue file silently became an empty queue.** `_read_queue_unlocked` caught every
  exception and returned `{"items": []}`, so a malformed `pending-approvals.json` would make every
  pending approval vanish with no error. It now validates and raises. This is the repo's own
  fail-loud rule, and silent data loss is exactly the failure mode the whole review exists to fix.

### Out-of-scope change reverted in Stage 3

Codex also modified `ops/doctor.sh` to resolve `YAAS_AGENT` by parsing `.env` instead of reading the
environment. Not in the brief, and a regression: the new helper never consults the environment, so
`YAAS_AGENT=codex bash doctor.sh` would silently report `claude`. Reverted.

The underlying observation is probably valid, since `triage.sh` sources `.env` and `doctor.sh`
reading only the environment can disagree with what actually runs. The right fix is
environment-wins-with-`.env`-fallback. Worth doing as its own change.

### What changed against the plan as written

**Stage 1.2 was a no-op.** The `edit` route already existed uncommitted in the working tree.

**Codex's follow-up pass closed three gaps this plan left open**, all folded into `bc953ab`:

- `needs_reconcile` was write-only. `cmd_done` now pops it, so an item does not stay flagged
  after its outcome is resolved.
- **The flag had no consumer, which was the real miss.** 1.5 invented `needs_reconcile` as the
  safety mechanism separating reclaim from undo, but never said what a worker does on seeing it,
  which made it decorative. `yaas-quest-dispatch/SKILL.md` now carries the protocol: check the
  target before acting, `done` if it already landed, execute normally if not, `blocked` if
  undeterminable, and `abandon` for a `manual_instruction` whose arbitrary effects cannot be
  proven from one target. Without this, the undo/reclaim split existed only at the HTTP layer.
- The Undo toast fired after any successful action, but `revise` lands in `needs_reply`, which the
  new undo endpoint deliberately rejects. It offered an Undo that would 409.

It also strengthened 1.1: the original characterization test called `_update_approval` with a
hardcoded `allowed` map, so it partly asserted its own copy of the rules. It now drives
`_handle_review` end to end and checks the HTTP code plus the persisted status.

### Findings to carry into Stage 3

**1. Codex signals completion on a red suite.** It did this on both stages. Its code was accurate
and on-spec each time, including subtle constraints, but its self-report is not a gate. Always run
the suite independently and read the summary line: `run-all.sh` **exits 0 even when a suite
fails**, so `$?` is not usable as a gate either. Harness details are in the
`reference_codex_headless_invocation` memory.

**2. Sharing a module into a standalone CLI breaks its minimal test fixtures. Neither review
priced this in.** Routing `dashboard-server.py` through `is_due` gave it a hard import dependency
on `tick_check`, and six behaviour tests that build a fixture tree containing only
`ops/dashboard-server.py` failed on `ModuleNotFoundError`. Fixed by provisioning the fixtures
(`e9bcc22`), not by backing out the extraction.

**This will bite Stage 3 harder.** `approval_state.py` plus a store module is a much wider
dependency, and its three consumers (`ops/dashboard-server.py`, `checkers/approval.py`,
`ops/health-monitor.py`) all have minimal fixtures. Budget for a fixture-provisioning step in the
Stage 3 brief up front rather than discovering it at the end. Grep for
`cp "$HERE/ops/...` patterns in `tests/behaviour/` before starting.

**4. `tests/contract/` is never executed by the runner, so the Stage 1.6 guardrail has never run.**
`run-all.sh:32` globs only `unit/` and `behaviour/`. My Stage 1 brief invented the
`tests/contract/` path, which did not exist before Stage 1, so `dashboard-routes.test.sh` sits
outside the runner. It fails hard without its fixture fix and every "38 of 38 green" report above
was therefore incomplete: it never ran the parity test at all. My error, not Codex's. Fixed as
Stage 3 item 3.0 (move it into `behaviour/` rather than add a second directory for the same idea).
Codex's follow-up patch added the required `cp "$HERE/tick_check.py"` to that fixture, which is the
seventh fixture the Stage 2 finding applies to; I had only found six because I grepped
`behaviour/`.

**3. `_repo_root` must stay duplicated.** The comment at `checker-health.py:87` and
`dashboard-server.py:65` explains why: a shared version would need `sys.path` handling that itself
depends on knowing the repo root. `repo-root.test.sh` pins it byte-identical across files. Stage 2
correctly used it to bootstrap the `tick_check` import rather than trying to share it. Stage 3 must
do the same.

---

## Where the two plans disagreed

Adjudicated by reading the code, not by preferring a reviewer. Codex wins four, this plan wins
one, and the rest were complementary.

| Topic | Outcome |
|---|---|
| Undo from `executing` | **Codex.** `checkers/approval.py:96-99` forbids blind resend after an expired lease. Original 0.1 was unsafe. Now 1.5, two endpoints. |
| Where shared approval logic lives | **Codex.** `ledger/approval-helper.py` is hyphenated and subprocess-only, so it cannot be imported. New `approval_state.py`. Now 3.1. |
| One generic `apply_backoff` | **Codex.** Two real ladders, 60s/1h versus 300s/24h with a promote threshold. Share the predicate only. Now 2.2. |
| Route guardrail by grepping `fetch()` literals | **Codex.** Would have missed the dynamic path at `dashboard.html:950`, the single most important case. Now a contract test, 1.6. |
| `needs_reply` belongs in the in-flight strip | **This plan.** Codex's one-status-one-surface rule is too rigid here. `_update_approval`'s default `from_status` and the comment at `dashboard-server.py:191-194` make `needs_reply` deliberately reviewer-actionable while also dispatched to the worker. Moving it to the read-only strip removes a designed capability. It stays in the carousel; see 1.4. |
| Characterization tests before fixes | **Codex**, folded in as 1.1, ahead of the bug fixes rather than replacing them. |
| Server emits `available_actions` | **Codex**, adopted as 3.4. Stronger than the source review's client-side status switch. |
| Manifest schema depth | **Codex**, adopted in 4.1. `schedule`'s any-of and `approval`'s runtime-only status genuinely do not fit a flat list. |
| Snapshot verification of `309aab8` | **This plan**, via the `.git-yaas-v2` mirror, which Codex could not see. |
| GitHub checker collapse | Both agreed. Promoted to Stage 2 as the best risk-adjusted deletion in the plan. |
