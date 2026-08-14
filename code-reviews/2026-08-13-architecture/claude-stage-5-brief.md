# Stage 5 execution brief (for Codex)

Baseline: `d54c025` in `.git-yaas-v2` (**the git tree of record**), working tree clean apart from
this directory. Suite green at **41 suites, 0 failed**, differential 29/0.
Plan: `claude-plan.md`, Stage 5 section. Verified at `d54c025`.

You reviewed the Stage 5 plan and raised four findings. **All four were verified against the code
and all four are incorporated.** Implement directly; no second review round.

**Scope: 5.2 and 5.3 only. Do NOT do 5.1.** Do not touch any `startswith("slack_")` site in
`tick.py` or `tick_dispatch.py`, and do not make `tick.py`, `tick_dispatch.py` or `Config` load
manifests. That deferral is deliberate and is the conclusion you yourself argued for.

---

## Ground rules

1. This is a **test-and-docs** stage. No runtime behaviour changes at all.
2. `tick.py` differential goldens must stay green with **no golden edits**.
3. Do not commit. Leave work in the working tree.
4. **No out-of-scope changes.** This has bitten twice (`doctor.sh` in Stage 3). Report, don't fix.
5. Never touch `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` or `.env`.
6. Run `bash yaas-triage/tests/run-all.sh` at the end; `$?` is trustworthy. Report the real
   summary line.

---

## 5.2 Documentation contract tests

One new suite, `tests/behaviour/doc-contracts.test.sh`. **Assert, do not generate.** Every failure
message must name the doc file and what it expected, so the fix is obvious to whoever trips it.

### The four contracts

**a. `README.md:194-195` — all ten manifest stems.** The type names appearing in that table must
equal the set of `checkers/*.watch.json` stems.

**b. `ARCHITECTURE.md:223-226` — all ten manifest stems.** Same assertion, different table.

**c. `yaas-ops/SKILL.md` — every `.lag` file AND its value, across BOTH doc sites.**

This is the finding that matters most. The `.lag` values are documented in **two** places:

- `:112` lists `email.lag = 120`, `slack_mention.lag = 90`, `github_pr.lag = 30`, `jira.lag = 15`
- `:136` separately documents `github_issue.lag = 30`

There are five `.lag` files. A test scoped to `:112` alone would pass while ignoring
`github_issue` entirely, which is precisely the drift it exists to catch. Assert that the union of
what both sites document equals `{stem: value}` read from `checkers/*.lag`, values included.

Do not assume the two sites keep their current line numbers or phrasing; match on the
`<type>.lag = <int>` pattern across the whole file rather than on line numbers.

**d. `yaas-quest-creation/SKILL.md` — a MIXED contract, not a pure subset.**

Assert both halves:

- the set of types the page offers for creation equals `user_creatable: true` (nine types), and
- `approval` is present **and** documented as runtime-only.

`SKILL.md:45` deliberately carries `approval` as a runtime-only caveat ("Do not put one in a
creation spec"). A test asserting only the `user_creatable` subset would leave that unprotected,
and a naive test would flag it as an error when it is intentional.

### Leave alone

`yaas-quest-dispatch/SKILL.md` worker-operating guidance and the checker-authoring meta-table's
advice are judgement, not tables. Do not assert them and do not rewrite them.

### Retire the grep

`yaas-checker-authoring/SKILL.md:209-212` tells the author to
`grep -rn "<nearest_sibling_type>"` and add themselves wherever it hits. Replace that instruction
with a pointer to the new failing test, which was the entire point of candidate 9.

---

## 5.3 Manual prose cleanup — Stage 4 left instructions naming a dead symbol

These are factually wrong, not merely drifted, so "leave prose alone" does not apply. Fix by hand.

- **`yaas-quest-creation/SKILL.md:47`** says to register a new type "in `REQUIRED_FIELDS`".
  `REQUIRED_FIELDS` **no longer exists** — `new-quest.py:122,129` now imports and calls
  `load_watch_manifests`. Anyone following that instruction adds a symbol nothing reads. Rewrite
  it to say: write the checker, then add `checkers/<type>.watch.json` beside it.
- **`yaas-checker-authoring/SKILL.md:186`** lists `new-quest.py`'s "header docstring listing
  fields per type" as a registration site. That is now the manifest. Correct the row.

While you are in `yaas-checker-authoring/SKILL.md`, check the rest of its registration list
against reality at `d54c025` and correct any other row that names a site the manifest replaced.
Report what you changed; do not rewrite the skill's judgement content.

---

## Exit criteria

- Adding a `.lag` file, or changing a `.lag` value, fails a test until both doc sites agree.
- Adding a watch type fails a test until README and ARCHITECTURE list it.
- No doc instruction names `REQUIRED_FIELDS` or any other symbol the manifest replaced.
- The `grep -rn` instruction in checker-authoring is replaced by a pointer to the test.
- No `startswith("slack_")` site touched; `tick.py`, `tick_dispatch.py` and `Config` unchanged.
- Suite green (baseline 41 suites), differential 29/0, no golden edited.

## Report back

- What changed per item; every test added or modified and why.
- The real suite summary line, and confirmation no golden was edited.
- Any doc site you found that is stale but that you deliberately left alone, with the reason.
