# Sidequestor — worker + interactive addendum

*Append this block to your existing `CLAUDE.md`. It layers a background quest system (Sidequestor)
over your own agent. Your own rules above still apply; this only governs how you touch the quest
system, in both the autonomous loop and your interactive sessions.*

---

## Two modes

Infer which one you're in from context:

- **Autonomous (Mode A):** the triage loop invokes you headless (`claude -p`) with **one** dispatch
  target and a list of items to close. Act on exactly that target, then exit. This is the
  $0-when-idle background worker.
- **Interactive (Mode B):** a human is talking to you in the REPL. You may inspect or steer quests
  on their behalf. The loop may still be ticking in the background while you do.

**On an autonomous dispatch** you're given `dirty target: <quest_id | reactions>` plus an item
list. Do only this:

1. Load the matching skill and follow it:
   - target `reactions` → `yaas-triage/skills/yaas-reactions/SKILL.md`
   - a quest id → `yaas-triage/skills/yaas-quest-dispatch/SKILL.md`
2. Close **every** listed item in the ack ledger before exiting (the prompt gives the exact
   `ack-watch.py ack` command). Unacked items re-surface next tick; a false ack buries real work.
3. **Act first, then report** — never send a promise you haven't already executed.
4. Don't scan anywhere else; the loop already found what's new.

## Coexistence: interactive and autonomous must not collide

Both modes share the same quest folders and state files, and the loop can dispatch a quest while
you're mid-conversation about it. These rules keep the two coherent and apply in **both** modes:

1. **The loop owns watermarks.** Never edit an existing `watch.json` entry or any `last_checked_ts`
   — a hook blocks raw edits and `yaas-triage/ledger/watch-guard.py` reverts them. Add a watch only via
   `yaas-triage/ledger/add-watch.py` (append-only, safe mid-tick).
2. **Touch shared state only through the helpers.** Each locks and writes atomically, so it is safe
   to run concurrently with a tick. Never hand-edit the underlying JSON:
   - watches → `yaas-triage/ledger/add-watch.py`
   - ack ledger → `yaas-triage/ledger/ack-watch.py` — **Mode A only**; never ack items you weren't dispatched
   - review queue → `yaas-triage/ledger/approval-helper.py`
   - reaction emojis → `yaas-triage/surfaces/react-lifecycle.py advance`
   - Slack sends (auto-logged) → `yaas-triage/surfaces/slack-send.py`
3. **Don't act on a quest the loop is mid-handling.** A tick holds a flock and writes a
   `state/triage/dispatch-*.json` manifest while a worker runs. In Mode B, before sending into a
   watched thread or editing a quest's `context.md` / `meta.json`, confirm there's no in-flight
   manifest and no just-written timeline entry for it. When unsure, route through the review queue
   instead of sending directly.
4. **Draft-first.** Never send outbound without `allow_send: true` (Mode A) or an explicit human
   go-ahead (Mode B). Sends are **not** idempotent — the loop will not dedupe a message you sent by
   hand, so a manual send during an active dispatch can double-post.

## Judgment rules (not mechanized — you must hold these)

- Internal escalation / expert threads are read-only: post one crisp ask, then only monitor —
  never a second bot reply.
- Execute every in-tick commitment ("I'll raise X with the team") before reporting; a forward
  promise with no action and no `schedule` watch or approval backing it is a bug.
- Privacy: never relay one person's information to another unless explicitly asked to.

## More

Per-watch-type querying, logging, approvals, and quest completion live in the dispatch skill above.
Slack access (native MCP tool, or the `yaas-triage/surfaces/mcp-call.sh` shell fallback) and the wider state
layout live in `yaas-triage/skills/yaas-ops/SKILL.md`. Adding a NEW watch type requires an
executable `checkers/<type>.py`, a `checkers/<type>.watch.json` manifest, its behavior tests, and
the relevant docs. Follow `yaas-triage/skills/yaas-checker-authoring/SKILL.md`.

---
## ▲ YOUR OWN AGENT RULES GO ABOVE THIS BLOCK ▲
This Sidequestor block is purely additive — keep your existing `CLAUDE.md` content above it.
