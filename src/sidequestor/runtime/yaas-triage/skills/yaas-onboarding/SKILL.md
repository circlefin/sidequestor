---
name: yaas-onboarding
description: Run the owner's guided Sidequestor onboarding (Stage 0 inventory, Stage A configuration checks, Stage B AGENTS.md and skills, Stage C quests, Stage D check-ins to day 30). Load on every dispatch of the quest whose folder holds onboarding.json, and in a terminal session started by `sq onboard --terminal`. Talks to the owner through the channel recorded in onboarding-channel.json.
---

# yaas-onboarding

Guide the owner, who may have nothing set up, from a fresh install to a workspace they
understand and trust. "The owner" is the person this Sidequestor works for. Speak to them
in the first person ("I found…", "Shall I…?").

## State

The onboarding quest folder's `onboarding.json` stores progress:

- `stage`: `0`, `A`, `B`, `C`, `D` or `done`.
- `started_at`, `stage_c_completed_at`: UTC timestamps. Stage D counts days from the latter.
- `skipped_stage_a`: set to `true` if the owner chose a quick start.
- `interface_offer`: `offered`, `accepted` or `declined`, plus the approved name.
- `interface_channel_id`, `interface_quest_id`: verified created channel and ongoing quest.

The CLI-owned `onboarding-channel.json` stores `channel` (`slack` or `terminal`).
Read it on every dispatch and immediately before a message; it overrides any legacy
`channel` in `onboarding.json`. For a legacy quest without the channel file, use
that legacy value until `sq onboard` migrates it. Never write the channel file from
a worker or session. Only `sq onboard` changes it, so stale progress writes cannot
undo a channel switch. Its optional `slack_channel_id` selects the owner-approved
private interface channel; without it, Slack mode uses the verified owner DM.

Update `stage` (and `stage_c_completed_at`, `skipped_stage_a`) whenever a stage is
approved, by rewriting the progress file. Do not copy channel preferences into it. Keep `context.md` "Current state" to a short
summary of where things stand. Approvals and findings go in the timeline via `sq log`.

## Channels

**Slack.** Start in the owner's own Slack DM unless `onboarding-channel.json` selects
an approved private interface channel. Use that selected conversation for onboarding,
check-ins and replies. Old watches may still fire after a switch: acknowledge messages
from other conversations `nothing_to_do` without responding there. An explicit owner
request in the verified recovery DM to move back may instead invoke `sq onboard
--slack-dm`; reread the preference before continuing. Before the first DM message:

1. Resolve the owner's Slack user ID: the account Sidequestor's Slack credentials belong to.
   Then open or find the one-to-one DM conversation (`D…`) between the owner and that account.
   Verify it with a live read of that conversation. Never guess.
2. Add that `D…` ID to `allow_send_channels` in this quest's `meta.json`. Leave
   `allow_send` false. Post only through
   `python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/slack-send.py"`
   with this quest's `quest_id`.
3. Install one `slack_channel` watch for that verified DM with `sq watch`, so the
   owner's replies wake you. This also works for a self-DM under the owner's user
   credentials. Messages beginning `:sidequestor-yaas: ` are your own output: never
   treat them as owner answers or send another message merely to acknowledge them.
   A tick containing only your own messages must acknowledge `nothing_to_do`.

Start every Slack message with `:sidequestor-yaas: ` (the emoji, then one space). Keep messages
short and use Slack formatting. After the owner approves a step, add a one-shot `schedule`
watch (`next_fire_ts` about a minute out) with `sq watch`, so the next step starts
without waiting for the owner to write again.

**Terminal.** The owner is in an interactive session with you. Ask questions and wait for
answers there. Background dispatches of this quest must not advance Stages 0–C while the
channel is `terminal`. They handle only Stage D check-ins and a nudge when a stage has
stalled for more than two days. Those nudges are possible only if a verified owner DM
or approved private interface channel is already in `allow_send_channels`. Otherwise log an informational check-in for the
next terminal session, acknowledge `nothing_to_do`, and wait; missing optional Slack
must not mark terminal onboarding blocked. Surface pending check-ins when the owner
next runs `sq onboard --terminal`. Make sure
the quest keeps one daily `schedule` watch for this while in terminal mode. If a
Slack bootstrap was already pending when the owner switched to terminal, leave
Stages 0–C to the interactive session, verify the daily watch, and acknowledge the
bootstrap as `nothing_to_do`; triage can then clear it without advancing a stage.
Exact claimed channel-setup approvals may still execute through the normal approval
handler while terminal mode is selected. Record their results for the terminal
session; do not switch channel or advance stages on that background run.

**Switching.** The owner may run `sq onboard --slack` or `--terminal` at any time. On
the next dispatch or session, read `onboarding-channel.json` and `onboarding.json`, say in one line where things stand,
and carry on from the same stage. The agent performs local file operations and harness
commands for the owner after their approval. Do not send non-technical owners to the
terminal to finish a stage. Use Slack for questions and decisions; explain any required
browser login or operating-system permission in plain language.

## Ground rules

- `sq setup` and `sq upgrade` already handle upgrading Sidequestor and choosing the model.
  If `sq --version` reports an available update, or `.env` pins an older model than the
  `sq setup` default, mention it once and move on.
- If the owner wants a quick start, they may skip Stage A and come back to it. Set
  `skipped_stage_a`, and remind them before Stage C and again at the first Stage D check-in.
- Only one onboarding quest exists per workspace. Never create another one; resume this one.
- Build on what exists. Never delete, overwrite or reset an existing quest, skill,
  setting, connection or file without the owner's explicit OK. Suggest the change and wait.
- Skip any step that is already done. Tell the owner in one line what you found.
- Finish each stage and get the owner's OK before starting the next.
- Your only text-send authority is the verified owner DM and any explicitly approved
  private interface channel recorded in `allow_send_channels`. Keep `allow_send: false`.
  Anything addressed to other people (for
  example asking a colleague about a skill) is a draft the owner sends.
- Third-party skills, connectors and installs need the owner's OK first.

## Offer a Slack interface early

The purpose of onboarding is to let a non-technical owner use Sidequestor without
navigating a terminal. As soon as Slack identity/authentication works, before the
long inventory and configuration discussion, ask:

> Would you like me to create a private Slack channel, such as
> #alex-sidequestor-interface, where you can talk to Sidequestor, manage your quests,
> and ask me to configure things for you? You can keep using it after onboarding.
> Or would you prefer to stay in this DM or terminal?

Use the owner's verified name to propose a valid lowercase channel name; let them
choose another. Explain that it is private and initially for them alone. Record the
choice in `onboarding.json` as `interface_offer` (offered/accepted/declined), the
approved name, and later the actual `interface_channel_id` and `interface_quest_id`.
Do not repeat an offer they declined. Offer it in a terminal session too; if Slack
is not connected, explain the browser connection step and offer to help set it up.
Never require the owner to copy commands just to choose or use this interface.

If accepted:

1. Read `yaas-slack-channel/SKILL.md`. Confirm the exact name and private audience.
   In a dispatch, queue the exact channel-create action with `sq slack-channel
   approval-spec` and the normal review helpers. Give the owner the clickable review
   link and explain what to approve; a conversational yes does not bypass the helper's
   claimed-approval requirement. In an interactive session, use `interactive: true`
   only for their explicit manual request, never during a dispatch. Keep `allow_send`
   false; never widen it to create a channel. If scopes/authentication need repair,
   guide the owner through the browser steps and continue in the DM until it works.
2. Create the private channel through `sq slack-channel`. The token's owner joins
   automatically. Live-verify its returned ID, privacy and the owner's membership.
   Do not invite others or guess an ID. On an uncertain outcome, inspect before any
   retry. Resume from the recorded ID; never create a second channel just because a
   later setup step failed. If using an existing channel, get the owner's explicit
   choice and verify the same privacy/membership facts.
3. Add only that verified, approved ID to this onboarding quest's `allow_send_channels`.
   Install a `slack_channel` watch for it with `sq watch`. Finish the ongoing interface
   setup below before selecting the new conversation, so a background dispatch cannot
   start onboarding there while the terminal session is still configuring it. The
   interactive terminal agent executes the explicitly requested creation/setup itself;
   it does not leave that work to a terminal-mode daily nudge.
4. Create or reuse **one ongoing interface quest** for this workspace/channel through
   the normal quest helper. It is separate from the 30-day onboarding quest. Give it
   `allow_send: false`, `allow_send_channels: [<verified-ID>]`, and a `slack_channel`
   watch for that ID. Its context must name the verified owner user ID, actual channel
   ID and onboarding quest ID, and require loading `yaas-slack-interface/SKILL.md`.
   Save its quest ID in onboarding progress. Check active quests/progress before
   creating so a retry reuses it. Never attach a 30-day completion deadline to it.
5. After technical setup, and only if the owner agreed to move the conversation, run
   `sq onboard --slack-channel <verified-ID>` for them. It checks the existing grant
   and watch before saving the preference. Reread it before each message. Welcome
   the owner in the channel with its Slack link and short examples: “What's
   running?”, “Create a quest to track this”, “Pause my weekly report”, and “Help me
   connect Notion”. Explain that Sidequestor will do the technical steps and ask for
   approval when needed. Continue onboarding there from the same stage. If this was
   an interactive terminal session, record the current stage, tell the owner to
   continue in the channel, and stop driving stages in this session; background
   Slack dispatches now own them. Never keep both conversations moving in parallel.
   Keep the verified DM as recovery: if the channel is unavailable or its grant/watch
   was removed, run `sq onboard --slack-dm` for the owner to clear the selection,
   without widening permissions or editing the channel file. Declining or skipping
   this optional offer must not block the rest of onboarding.

While the onboarding quest is active in this channel, it owns the conversation;
load `yaas-slack-interface/SKILL.md` for owner requests about quests or the harness
between stages. The separate interface quest defers to it to avoid duplicate replies.
Completing onboarding must leave the ongoing interface quest active and its watch
intact. The interface skill describes the hand-off after day 30.

## Stage 0: What's already here

Summarise briefly:

- how long Sidequestor has been running
- active and closed quests
- existing skills and where they live
- AGENTS.md / CLAUDE.md
- which connections work: `SIDEQUESTOR_CHECKER_CONNECTORS`, plus a live probe of each

Then say which stages you will do in full, which only partly, and which you will skip, so
the owner can adjust that before you start. Someone who has used Sidequestor for a while
gets a shorter onboarding that builds on what they have.

## Stage A: Configuration checks

1. Confirm the Sidequestor loop is running and picking up changes (`sq doctor`, dashboard
   heartbeat). If it isn't, give the exact fix.
2. Audit the MCP and CLI connections the owner should be using, in two groups:
   (a) connectors the watch system uses for free update checks
   (`SIDEQUESTOR_CHECKER_CONNECTORS`: Slack, email, GitHub, Jira, Notion, Telegram, X);
   (b) tools dispatches use for research and actions (MCP servers, `gws`, `gh`, and others).
3. Report each configuration or authentication gap with short, specific fix steps. Wait
   until the owner has fixed each one or said to skip it.

## Stage B: AGENTS.md and skills

1. Scan the owner's activity over the past month across every working connection
   (Slack, email, calendar, docs, trackers). Work out:
   - the nature of their work
   - key tasks and projects
   - key documents, trackers and resources
   - the key people they work with, inside and outside the company

   Start from existing quests, context files and notes, and fill only the gaps.
2. AGENTS.md tells the agent who it is, how it writes and what it must never do.
   - If it exists, read it and suggest only what is missing. Never overwrite it, and never
     touch Sidequestor protocol sections.
   - If it does not exist, draft a short one from the scan and show it. Write it only
     after the owner's OK.
   - Keep it short. Make a skill only when AGENTS.md cannot carry the rule.
3. Inventory existing agent skills: the workspace `skills/`, `~/skills/`, and the
   Claude, Codex, Cursor and `.agents` skill folders, plus other folders on this machine
   that plausibly hold skills. Point out duplicates, stale copies and conflicts.
4. Suggest only missing skills, useful both interactively and in long-running quests.
   Skills can cover:
   - processes
   - quality bars (what "done" looks like)
   - technical procedures (for example CRUD operations on a data source, updating slides)
   - output formats

   Where an existing skill nearly fits, suggest additions to it instead of a new skill.
   Search Slack and public sources for colleagues' reusable skills. Ask before installing
   any third-party skill, and say who to ask when one isn't shared.
5. For writing style, ask whether the plain-english and simple-english skills from
   https://github.com/b1rdmania/claude-plain-english-skill are enough, or whether they want
   others, including any they already use.
6. Suggest a few skills unique to the owner's role. Once they agree, write a first
   draft of each, ask one round of questions, apply the answers, and stop.
7. Make `~/skills/` the single home. Symlink skills that live elsewhere and are in use
   rather than moving them. Link `~/skills/` into the agent tools the owner actually uses.
   Ask which ones; don't assume. Never break an existing path.

## Stage C: Quests

1. Review existing quests first: for each, say whether it is working, stuck,
   overlapping or no longer needed, and suggest changes. Change nothing until the owner agrees.
2. Propose new quests only where there is a gap. Include quests that act, not only ones
   that report. Use state and briefs so the owner can see what was done and what is left.
   Make sure the owner has:
   - at least one quest that sweeps every channel (Slack, email, calendar and others), so
     nothing is dropped
   - at least one quest that sends on the owner's behalf from the start, with a scope
     and quality bar the owner is confident in

   If these already exist, say so and skip. Start in draft-only mode wherever a wrong
   automated action would do more harm than good.
3. Present each new or changed quest, making sure the owner understands what it does and
   how to control it. Create only the approved ones, with `sq new-quest`
   (see the yaas-quest-creation skill).
4. Show how to control and monitor quests in plain language from their chosen Slack
   conversation: ask what is running, inspect progress, create/change a quest, and
   pause or resume one. Link to the dashboard for reviews and status. Technical CLI
   equivalents are optional for owners who ask for them; operate the harness for
   everyone else. Keep it shorter for experienced owners. Record
   `stage_c_completed_at`.

## Stage D: Check-ins

1. Check in on days 1, 2, 3, 5, 8, 14, 21 and 30 after `stage_c_completed_at`. At each:
   - confirm the quests are running
   - flag things that may have slipped the owner's mind
   - ask whether any quest should change

   Schedule each check-in as a one-shot `schedule` watch.
2. Space check-ins out further for owners who clearly understand Sidequestor. Don't
   over-explain.
3. After day 30, set `stage` to `done`, post a short wrap-up, and complete only the
   onboarding quest. Point to the ongoing Slack interface, verify its quest/watch is
   active, and leave it running. Do not archive its quest or revoke its channel grant.
