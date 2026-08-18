# Meet Sidequestor (YaaS)

Hi! I am Sidequestor. **YaaS** means *Yourself as a Service*, and the name is
literal: your local agent, files, tools, skills, and access, wrapped in a tiny
service that can keep pursuing your quests while you are away.

I do not roam around Slack looking for adventures. I wake up when a watched
conversation changes, or when you give a message one of these reactions:

| Reaction | The tiny spell it casts |
|---|---|
| `:claude-intensifies:` | **Handle this.** Read the thread, do the work, and reply in-thread. |
| `:writing_hand:` | **Draft this.** Research and prepare a reply, but leave the sending to you. |
| `:floppy_disk:` | **Remember this.** Quietly save useful context for later. No reply. |
| `:incoming_envelope:` | **Adopt this.** Add the message to an existing quest so it is not forgotten. |

## Why did the emoji change?

That is the progress bar. For actions that take work, I keep exactly one status
reaction on the message:

```text
your reaction  ->  :claudeloading:  ->  :updatedone:
please do this     working on it        finished
```

- **Loading** means I picked it up. If it stays there, I may be blocked or waiting.
- **Done** means the requested action and its immediate follow-ups are complete.
- **Floppy disk** is the shy one: it saves silently and does not do the emoji dance.

## What is a quest?

A **quest** is an open-ended mission for your local agent, not just a watcher. It
has an objective, context, boundaries, send permission, and durable memory. The
agent can research, draft, ask questions, wait for people, notice their replies,
continue the work, and coordinate follow-ups across conversations and time.

The watches are simply tripwires that wake the mission when its next step is
ready:

```text
objective -> watch -> wake agent -> act -> remember + adjust watches -> repeat
```

That makes quests useful for work such as shepherding a partner question to an
answer, running a review cycle, preparing a recurring brief, or following an
issue from discovery through resolution. Each time it wakes, the agent can adapt
the watch plan for what comes next: another thread, a person's reply, an approval,
or a future time. Then cheap local code watches those new tripwires while the full
agent sleeps. A long-running quest stays attentive without staying expensive.

You can create one from the dashboard or simply tell an agent:

```text
Create a quest to shepherd this partner question to resolution. Watch this
thread, research each new question, prepare replies, track promises, and follow
up until every open item is closed. Draft first; do not send.
```

Every quest is still just four readable files: its context, settings, tripwires,
and timeline. No mystery database. If something looks odd, you can see its
mission, why it acted, what is waiting for review, and what happens next.

## Who is actually doing the work?

Sidequestor does not wake a cut-down chatbot. It wakes your real local agent in
your real working folder. Subject to the permissions you configured, that agent
can use your local files, command-line tools, test kits, MCP connectors, personal
skills, and authenticated services. A quest can therefore inspect documents,
run tests, update files, research connected systems, and orchestrate real work,
not merely send reminders.

Claude Code, Codex, and Cursor can all use this same Sidequestor folder. They can
search and edit the same quest files, code, and history, then hand work to one
another through ordinary files and Git diffs. They share the workshop, not their
private chat history.

That access is powerful, so each dispatch stays inside one quest, external sends
follow that quest's permission and review rules, and the result is written to an
inspectable trail.

Behind the curtain, a tiny local Python loop checks for changes about once a
minute. When nothing changed, no AI runs. When something did, it wakes one agent
for one target, records the result, and goes back to sleep.

Free Slack checking requires a workspace Slack app. If that is unavailable, set
`YAAS_SLACK_CHECKERS_ENABLED=0` in `.env`. Slack watch entries stay recorded but dormant,
the reaction sweep is disabled, and schedule, email, Jira, and GitHub watches continue.
Scheduled agent runs can still access Slack through the agent's own MCP connector.

That is the whole rhythm: **react for now, quest for later, review before send.**

Want the technical tour? See the full [Sidequestor README](README.md).
