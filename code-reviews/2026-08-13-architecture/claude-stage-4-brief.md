# Stage 4 execution brief (for Codex)

Baseline: `5684086` in `.git-yaas-v2` (**the git tree of record**), working tree clean.
Suite at baseline: **40 suites passed, 0 failed**, `run-all.sh` exit 0, differential 29/0.
Plan and rationale: `claude-plan.md`, Stage 4 section. Line numbers verified at `5684086`.

**Scope is Stage 4 only** (items 4.0 to 4.4). Stage 5 stays out: do **not** replace
`startswith("slack_")` with the `upstream` field, and do **not** write the documentation contract
tests. Add the `upstream` field to the manifest, then stop.

---

## 0. Review already happened — this is revision 2

You reviewed revision 1 and raised seven findings. **All seven were verified against the code and
all seven are incorporated below.** Your two structural recommendations are adopted: per-type
sidecars stay (adding or removing a type stays local), and the checker example stays in the sidecar
now that its semantics are pinned.

You may now implement. If something here is still wrong, report it and stop rather than working
around it.

What changed from revision 1, so you do not re-litigate it:

| your finding | resolution |
|---|---|
| 1 · schedule validation contradictory | **Canonical rule chosen: `cron` + `tz`, OR `next_fire_ts`.** See 4.0. |
| 2 · `example_entry` ambiguous | Renamed **`checker_example`**, defined as checker input. See 4.2. |
| 3 · loader must be lazy | `load_watch_manifests(triage_dir)` is a pure function. **`Config` is not touched.** See 4.3. |
| 4 · inventory wrong | Corrected: 15 `.py`, 12 executable, **10 watch types**, 2 executable non-types, 3 helpers. |
| 5 · bijection under-specified | Explicit `NON_WATCH_EXECUTABLES` exclusion, both directions. See 4.3. |
| 6 · schema needs a concrete form | AND-of-OR list form plus a full per-type value matrix. See 4.2. |
| 7 · failure behaviour missing | Fail closed with the offending path, six named cases. See 4.5. |

---

## 4.0 First: settle the `schedule` contradiction, with characterization tests

Do this before touching the manifest, because centralizing `required` while the two paths disagree
would silently change behaviour on one of them.

The contradiction, verified:

- `ledger/add-watch.py:142` accepts `cron` with **no** `tz`.
- `checkers/schedule.py:69` then defaults `tz` to `"UTC"`, so such a watch silently runs in UTC.
- `skills/yaas-quest-creation/new-quest.py:207` **rejects** `cron` without `tz`, with the comment
  that "a bare cron is ambiguous".

**Canonical rule going forward: a schedule needs `cron` AND `tz`, or `next_fire_ts`.** This adopts
the stricter `new-quest.py` behaviour, because a bare cron really is ambiguous and silently
resolving it to UTC is the kind of quiet wrong answer this whole review exists to remove.

This is a **behaviour change for `add-watch.py`**, so:

1. Write characterization tests first, pinning what both paths do **today**, including that
   `add-watch.py` currently accepts bare `cron`.
2. Then change `add-watch.py` to require `tz` with `cron`, and update those tests in the same commit
   with the reason.
3. Check whether any existing `state/quests/active/*/watch.json` has a bare-`cron` schedule watch.
   If any does, **report it and stop** rather than making live state invalid.

---

## Ground rules

1. **`tick.py` differential goldens must stay green with NO golden edits.** Stage 4 touches
   `tick_state.py`, which `tick.py` imports, so this is a live risk. If a golden changes, stop.
2. **Do not commit.** Leave work in the working tree; commits are made for you.
3. Run `bash yaas-triage/tests/run-all.sh` at the end. It exits non-zero correctly, so `$?` is
   trustworthy, but still report the `N suite(s) passed, M failed` line.
4. **Do not make out-of-scope changes.** On Stage 3 you also "fixed" `ops/doctor.sh` in a way that
   regressed it (the new `.env` parse ignored the environment variable entirely), and it was
   reverted. If you spot an unrelated bug, report it; do not fix it here.
5. Never touch `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` or `.env`.

---

## 4.1 Fixture pre-flight, before the loader exists

Stage 2's only failure, and one of Stage 3's, was that giving a standalone CLI a new import breaks
every minimal test fixture that copies it. **Only `tick.py` imports `tick_state` today**, so every
other consumer in 4.3 is a brand-new dependency.

Derive the list yourself. Grep for fixture copies of each consumer using **both** `$HERE` and
`$SCRIPT_DIR` (my Stage 3 list was wrong because I grepped one spelling), and any other variable
name in use. Then provision each fixture in the same change that adds the import, not at the end.

Consumers to check fixtures for: `ledger/add-watch.py`,
`skills/yaas-quest-creation/new-quest.py`, `ops/dashboard-server.py`, `tests/lib/scenario.py`,
`tests/behaviour/checker-contract.test.sh`.

---

## 4.2 The manifest

`checkers/<type>.watch.json` beside the existing `<type>.lag`:

    schema_version   int, currently 1
    required         AND-of-OR list, see below
    identity         flat list: the fields that make two watches the same watch
    checker_example  a valid CHECKER INPUT entry, see below
    open_loop        bool
    user_creatable   bool
    upstream         string or null (declared now, NOT consumed until Stage 5)

### `required` is an AND-of-OR list

Outer list is OR, each inner list is AND. So `schedule` becomes:

    "required": [["cron", "tz"], ["next_fire_ts"]]

and an ordinary type is a single alternative:

    "required": [["channel_id", "thread_ts"]]

That expresses 4.0's canonical rule without inventing a general schema language.

### `checker_example` is checker input, not creation input

Renamed from `example_entry` because the semantics were ambiguous. The existing samples at
`tests/behaviour/checker-contract.test.sh:65-73` carry `last_checked_ts` and omit `reason`, so they
are **valid checker inputs and invalid creation inputs**. Define the field as: a minimal entry
sufficient to invoke the checker, including `last_checked_ts`, not required to satisfy
`add-watch.py`. Say so in a comment in each file so nobody re-reads it as a creation template.

### Value matrix, all ten types

Take `required` and `identity` from `add-watch.py:81` and `:94`; `open_loop` from
`dashboard-server.py:563`. Do not re-derive these from prose.

| type | required | identity | open_loop | user_creatable | upstream |
|---|---|---|---|---|---|
| `slack_thread` | `[[channel_id, thread_ts]]` | `channel_id, thread_ts` | true | true | slack |
| `slack_channel` | `[[channel_id]]` | `channel_id` | true | true | slack |
| `slack_dm` | `[[channel_id, user_id]]` | `channel_id, user_id` | true | true | slack |
| `slack_mention` | `[[user_id]]` | `user_id` | true | true | slack |
| `email` | `[[query]]` | `query` | true | true | gmail |
| `jira` | `[[jql]]` | `jql` | false | true | jira |
| `github_pr` | `[[repo]]` | `repo` | false | true | github |
| `github_issue` | `[[repo]]` | `repo, search` | false | true | github |
| `schedule` | `[[cron, tz], [next_fire_ts]]` | `id, cron, next_fire_ts` | false | true | null |
| `approval` | `[[approval_id]]` | `approval_id` | false | **false** | null |

Two details not to lose: `github_issue`'s identity includes `search` while `github_pr`'s does not
(the comment at `add-watch.py:101` explains that two issue watches on one repo with different
qualifiers are genuinely different watches); and `approval` is `user_creatable: false` because it is
runtime-only, appended by the worker, per the comment at `new-quest.py:116`.

`upstream` is **declared only**. Nothing reads it in Stage 4. Do not touch the five
`startswith("slack_")` sites in `tick.py`.

**Model the boolean subsets on Stage 3's `http` flag.** `_OPEN_WATCH_TYPES` at
`dashboard-server.py:563` is a semantic subset, the same shape Stage 3 solved with a per-entry flag
plus a named subset. No consumer should re-derive a subset by listing type names.

---

## 4.3 The loader

`load_watch_manifests(triage_dir)` in `tick_state.py`, mirroring `load_lag_map` at
`tick_state.py:143`: one glob, no import from the checkers, so the standalone-CLI doctrine survives.

**It must be a pure, explicitly-called function, and `Config` must not change.** Do not call it from
`Config.__init__` (`tick_state.py:167`) and do not call it at module import. `tick.py:71` constructs
`Config` on every tick, so loading manifests there would make every tick hard-depend on the
manifests being present and valid, and would break the minimal and custom-checker fixtures that
build a partial `checkers/` tree. **Stage 4 should leave `tick.py` and `Config` behaviour
unchanged**, which is also what keeps the differential goldens green.

### Bijection, stated precisely

A glob alone cannot tell a watch checker from an executable non-type. So:

    NON_WATCH_EXECUTABLES = {"cron-due", "reactions"}

Then assert **both** directions:

- every executable checker stem, minus `NON_WATCH_EXECUTABLES`, has a manifest;
- every manifest has a matching **executable** checker (catches an orphan manifest, and a manifest
  whose checker lost its executable bit).

Non-executable helpers are excluded by the executable-bit rule that
`checker-contract.test.sh` already pins, so `result.py`, `slack_utils.py` and `github.py` need no
special-casing.

### Verified inventory at `5684086`

15 `.py` files in `checkers/`: **12 executable**, of which **10 are watch types**
(`approval`, `email`, `github_issue`, `github_pr`, `jira`, `schedule`, `slack_channel`, `slack_dm`,
`slack_mention`, `slack_thread`) plus 2 executable non-types (`cron-due`, `reactions`); and
3 non-executable helpers (`github.py`, `result.py`, `slack_utils.py`). Also 5 `.lag` files.

So there will be **10 manifests**. Revision 1 of this brief said nine, which was a double-subtraction
on my part. `cron-due.py` is hyphenated, not `cron_due.py`.

Consumers outside `yaas-triage/` reach the loader the way Stages 2 and 3 established: the existing
byte-identical `_repo_root()` walk-up, then `sys.path.insert`. **Do not share `_repo_root`** — see
the comment at `add-watch.py:66`; `repo-root.test.sh` pins it byte-identical across files.

`skills/yaas-quest-creation/new-quest.py` is one level deeper than the other consumers, so it needs
the walk-up rather than a relative hop.

---

## 4.4 Repoint the consumers

Eight sites, verified at `5684086`:

| site | what it declares |
|---|---|
| `ledger/add-watch.py:81` | `REQUIRED` |
| `ledger/add-watch.py:94` | `IDENTITY` |
| `skills/yaas-quest-creation/new-quest.py:109` | required fields |
| `ops/dashboard-server.py:563` | `_OPEN_WATCH_TYPES` |
| `tests/lib/scenario.py:294` | the type set used to build fixtures |
| `tests/behaviour/checker-contract.test.sh:170` | the type set for the contract loop |
| `tests/behaviour/checker-contract.test.sh:70-73` | one sample entry per type (feeds `example_entry`) |
| `tick_state.py:143` | `load_lag_map` — leave it, extend beside it |

`new-quest.py` also carries a prose type table in its module docstring (around `:40`). Leave prose
alone in this stage; Stage 5 covers documentation contracts.

---

---

## 4.5 Fail closed, with the offending path in the error

The loader and the contract test must reject these six cases loudly. A manifest system that
silently tolerates a broken manifest is worse than no manifest, because every consumer then
disagrees quietly. Every message must name the offending file path.

| case | expected |
|---|---|
| invalid JSON in a manifest | raise, naming the path |
| unsupported `schema_version` | raise, naming the path and the version |
| wrong field type (e.g. `required` not a list of lists) | raise, naming the path and the field |
| `checker_example` does not satisfy that type's own `required` | raise |
| manifest with no matching executable checker | raise |
| executable checker (not in `NON_WATCH_EXECUTABLES`) with no manifest | raise |

Put loader-level cases in `tests/unit/tick_state.test.sh` and the inventory/bijection cases in
`tests/behaviour/checker-contract.test.sh`.

---

## Exit criteria

- A new watch type requires one directory entry plus two files, and no edit to any of the eight
  sites in 4.4.
- Bijection asserted in both directions, with `NON_WATCH_EXECUTABLES` explicit.
- All six failure cases in 4.5 covered by tests.
- `schedule` has one canonical rule (`cron`+`tz`, or `next_fire_ts`) enforced identically by
  `add-watch.py` and `new-quest.py`, with characterization tests recording the change.
- `checker_example` is documented as checker input, not a creation template.
- No consumer re-derives a type subset by listing names.
- `Config` and `tick.py` behaviour unchanged; nothing reads `upstream` yet.
- Full suite green (baseline is 40 suites), differential 29/0 with no golden edited.

## Report back

- Your review findings first, before implementing. Then wait for the corrected brief.
- After implementing: what changed per item, every test you modified and why, the real suite summary
  line, and confirmation that no golden was edited.
- Anything you deliberately left alone, and anything out-of-scope you noticed but did not touch.
