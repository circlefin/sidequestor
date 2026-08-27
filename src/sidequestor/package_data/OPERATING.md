# Sidequestor Worker Operating Instructions

This file is installed by the Sidequestor package and is the canonical runtime contract.
Read it before acting on a triage dispatch.

## Dispatch boundary

The prompt names exactly one target: one quest ID or `reactions`. Process only that
target and every listed dirty item. Do not scan other quests, channels, or watches.
Act first, then report. Close every listed item before exiting with:

```text
python3 <runtime-root>/yaas-triage/ledger/ack-watch.py ack <run_id> <item_id> handled|nothing_to_do|blocked "<note>"
```

Load the matching managed skill:

```text
.yaas/engine/current/skills/yaas-quest-dispatch/SKILL.md
.yaas/engine/current/skills/yaas-reactions/SKILL.md
```

## Runtime boundary

The workspace contains state, configuration, quests, logs, and managed engine
resources. It intentionally does not contain a `yaas-triage/` source directory.
The runtime exports `$SIDEQUESTOR_RUNTIME_ROOT` and the legacy alias `$YAAS_RUNTIME_ROOT`
to point at the packaged helper tree. Use that variable (or the resolved absolute packaged runtime root)
for helper commands; do not use repo-relative `yaas-triage/...` paths. In an interactive
shell outside the runtime, resolve the same path with:

```text
python -c "from sidequestor.native import RUNTIME_ROOT; print(RUNTIME_ROOT)"
```

When a skill shows a helper command, run the corresponding path below the packaged
runtime root. For example:

```text
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/log-event.py" '<json>'
python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/ledger/add-watch.py" <quest_id> '<json>'
```

Treat the packaged runtime as read-only. Never edit, create, or delete files under
the packaged runtime directory. Put mutable data in the workspace through the supported helpers.

## Shared-state rules

- Never edit an existing watch or watermark. Add watches only with `add-watch.py`.
- Ack only items included in the current dispatch. Never ack unrelated work.
- Write timeline events with `log-event.py`; send Slack through `slack-send.py`.
- Use the approval helper for review-queue items.
- For an unreviewed action, `allow_send: false` requires queuing the action for
  review instead of executing it.
- A `reviewed` approval records the user's authorization for that specific action.
  Execute it according to its action type and the dispatch rules; do not apply
  `allow_send` again as a permanent veto.
- If an action is blocked, log the blocker, ack the item `blocked`, and report it.

A backend-native workspace instruction file (`CLAUDE.md` under the Claude backend,
`AGENTS.md` otherwise) may add user-specific instructions. It is optional, and
Sidequestor never creates or edits it. Those instructions remain in force, but this
file owns the Sidequestor runtime boundary and dispatch protocol.
