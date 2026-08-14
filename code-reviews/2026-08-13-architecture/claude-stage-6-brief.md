# Stage 6 execution brief (for Codex)

Baseline: `aa1853f` in `.git-yaas-v2` (**the git tree of record**), tracked tree clean.
Suite green at **42 suites, 0 failed**, differential 29/0.

Small, contained task. This closes the last open item from the holistic review: the watch-type
manifest is authoritative for everything except Slack grouping, where `tick.py` and
`tick_dispatch.py` still read the type **name** instead of the manifest's `upstream` field.

**We are deliberately NOT making the tick read manifests.** That was considered and rejected: the
tick runs every 60s and is the liveness path, and giving it a hard dependency on ten manifest files
means one malformed manifest can stop the loop. Instead we make the naming rule **official and
enforced**, so the prefix cannot silently disagree with the manifest.

---

## Scope

Two things only. Do not touch `tick.py` or `tick_dispatch.py` logic.

### 6.1 A test pinning the prefix rule, in BOTH directions

Add to `tests/behaviour/checker-contract.test.sh` (this is a manifest/checker invariant, not a doc
contract, so it does not belong in `doc-contracts.test.sh`).

Assert, over `checkers/*.watch.json`:

- **Every type with `"upstream": "slack"` has a name starting with `slack_`.**
  This is the dangerous direction. `tick.py:256-258,340,352` and `tick_dispatch.py:66` decide
  "does this share the Slack rate limit?" by prefix. A Slack-backed type named anything else is
  silently excluded from that pacing, and Slack throttling has already caused a costly runaway
  loop in this repo's history.
- **Every type named `slack_*` declares `"upstream": "slack"`.**
  The cheaper direction to get wrong, but it means the manifest is lying about what the type talks
  to.

Failure messages must name the offending type and which half of the rule it broke, and say that
the fix is either renaming the type or correcting `upstream`.

This passes today: `slack_channel`, `slack_dm`, `slack_mention`, `slack_thread` are the only
`upstream: slack` types and all four are named correctly. **Verify it actually bites** before you
finish: temporarily set some non-Slack manifest's `upstream` to `slack`, confirm the suite fails,
then revert. Report that you did this.

### 6.2 Write the rule down where it will be read

Three places, all small:

- **`checkers/*.watch.json`** already carry a `_comment` about `checker_example`. Do not add a
  second comment to all ten. Skip this.
- **`yaas-checker-authoring/SKILL.md`** currently has a section I added noting this as an
  exception, phrased as "this is deliberate but the name still carries meaning". Upgrade it from a
  caveat to a **rule**: a Slack-backed type MUST be named `slack_*`, and `checker-contract.test.sh`
  enforces it. Keep the explanation of why the tick does not read manifests.
- **`tick.py`**, at the first prefix site (around `:256`), add a short comment: this groups by name
  on purpose, the manifest's `upstream` field is the declaration, and `checker-contract.test.sh`
  asserts the two agree. One or two lines, no logic change. Do the same at `tick_dispatch.py:66`
  if it reads naturally.

---

## Ground rules

1. **No logic changes.** `tick.py` and `tick_dispatch.py` get comments only. Their goldens must
   stay green with **no golden edits**.
2. Do not make anything in the tick path load manifests.
3. Do not commit. Leave work in the working tree.
4. No out-of-scope changes. If you spot something, report it.
5. Never touch `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` or `.env`.
6. Run `bash yaas-triage/tests/run-all.sh` at the end; `$?` is trustworthy. Report the real
   summary line.

## Exit criteria

- A manifest declaring `upstream: slack` on a type not named `slack_*` fails the suite, and you
  have demonstrated that by mutation.
- The reverse mismatch also fails.
- The rule reads as a rule in the checker-authoring skill, not as a quirk.
- `tick.py` and `tick_dispatch.py` say why they group by name and where the invariant is enforced.
- Suite green at 42 suites, differential 29/0, no golden edited.
