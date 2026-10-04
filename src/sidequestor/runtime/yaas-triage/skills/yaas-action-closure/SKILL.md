---
name: yaas-action-closure
description: Convert findings from authorized communication, record, briefing, and quest workflows into bounded execution, review items, or tracked commitments. Load when a quest may act on findings; do not use it to broaden read-only evaluation work.
---

# Action Closure

Read the quest objective, `allow_send`, permitted actions, and scope boundaries first. The quest
decides whether action is authorized; this skill decides how an authorized finding reaches
closure.

## Closure routes

Classify each material finding into exactly one route:

1. **No action owed.** Retain the fact only when the quest needs it as durable context.
2. **Execute now.** Complete the authorized action in this run, capture evidence, then report it.
3. **Review required.** Create one bounded approval item with the target, exact proposed action,
   relevant context, and review reason.
4. **Delayed commitment.** Record the commitment and install the trigger that will bring it back.

A digest or status sentence is a surface, not closure, when the quest authorizes one of routes
2-4. For a read-only evaluation or briefing quest, the stated report is closure and no operational
route is available.

## Delayed commitments

Every delayed commitment carries:

- the action owed;
- the owner;
- the due time or observable triggering condition;
- the source surface and conversation or record;
- the next action when the trigger fires;
- the quest that owns it.

Log those fields through the supported timeline helper. Use a `schedule` watch only for a due time
or recurring cadence. For an observable condition, use the matching surface watch (for example,
Slack, GitHub, Jira, or email) that can detect it. Use an approval item when human review is the
next gate. A promise written only in prose is not tracked.

## Quest design

An action-capable quest's `context.md` defines its outcome, allowed action types, review gates,
completion evidence, explicit exclusions, and what becomes a task, commitment, approval draft, or
brief. Keep one-run tasks out of ongoing quests and preserve every read-only boundary.

## Done condition

Every in-scope finding is consciously closed as no-action, executed with evidence, queued for
review, or attached to a durable trigger with an owner and next action. Nothing remains only as a
forward-looking sentence.
