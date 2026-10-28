---
name: yaas-slack-channel
description: Create private Slack channels, invite members, or set channel topics and descriptions through Sidequestor's Slack user token. Use when asked to create a private channel or manage its membership or details.
---

# Slack channels

Use `sq slack-channel` from an initialized workspace. The helper uses the rotating
Slack user token. Creation always sends `is_private: true`; public creation is not
supported. Private-channel operations use `groups:write`, `groups:write.invites`, and
`groups:write.topic`. The generated manifest and OAuth flow also request `channels:write`
and `channels:write.invites` for public-channel management/invitations. These are not
needed to create the private onboarding interface and do not enable public creation
through this helper.
On `missing_scope`, update the app's User Token Scopes, reinstall, and rerun `sq setup`.
For optional onboarding channel creation, read the `scopes` field from
`python3 "$SIDEQUESTOR_RUNTIME_ROOT/yaas-triage/surfaces/client.py" slack-identity`.
Offer creation only when the current Sidequestor user token has `groups:write`.
Otherwise continue onboarding in the owner's existing DM or terminal; do not make
reauthorization a prerequisite for onboarding.

For an explicit manual request outside dispatch, use `interactive: true`:

```bash
sq slack-channel create '{"interactive":true,"name":"project-planning"}'
sq slack-channel invite '{"interactive":true,"channel_id":"C...","users":["U...","U..."]}'
sq slack-channel topic '{"interactive":true,"channel_id":"C...","topic":"Project planning"}'
sq slack-channel purpose '{"interactive":true,"channel_id":"C...","purpose":"Coordinate the project"}'
```

Resolve real channel and user IDs first; the placeholders above are not valid IDs.
Resolve the intended audience before inviting anyone. Names must be 1–80 lowercase
letters, numbers, hyphens or underscores, with at least one letter or number. The
creator joins automatically. Creation returns `channel_id`, `name`, and `is_private`.
Use that ID for subsequent operations. Each command performs one mutation; a failed
invite or topic update must never cause another creation call. Invites, topics and
descriptions can also operate on existing public channels if the token has the
corresponding public-channel scopes; the helper never converts a channel's privacy.

For quest work, use `quest_id` instead of `interactive`. The quest must be active,
match the dispatch, and have `allow_send: true` or an exact claimed approval. Reaction
dispatches cannot change channels directly; create a quest or leave it for review.
The dispatch check relies on `SIDEQUESTOR_DISPATCH_TARGET`, like `slack-file`; it is
a guard against mistakes, not a security boundary. Never unset it or set `interactive`
to bypass quest policy. Force-draft mode denies all changes.

```bash
sq slack-channel approval-spec '{"action":"create","name":"project-planning"}'
```

Merge the returned `action_type`, `target`, and `message_text` into `sq approval write`
for the quest. Claim the reviewed item before executing the same operation with its
`approval_id` and `quest_id`. All fields are bound to the approval: names, privacy,
channel ID, invitees, and topic/description text. A supplied approval is checked even
when `allow_send` is true. Queue a separate approval for each operation. Execute via
this helper and finish the approval through the normal approval lifecycle. Successful
quest mutations log `executed` automatically, including `approval_id`; do not duplicate
that event with `log-event.py`. No Slack message is sent by these operations, so no
`slack_thread` watch or message `response_ts` is created. Close an approval using the
returned `channel_id` as its response identifier and a clear per-action report.

The approval target uses Slack API names. When reconstructing a command from it,
map `channel` to `channel_id`, split the comma-separated `users` string into a JSON
list, and remove `surface` and the `channel_` prefix on `action`. For creation, pass
`name` only; the helper sets the approved `is_private: true` itself. Preserve the exact
topic or purpose text. `message_text` is a generated action description, not a message
to post; do not copy it into a Slack send.

Exit 1 is invalid input; exit 2 is denied or a Slack API rejection. Exit 4 is a confirmed
rate limit with no mutation. Exit 3 means the remote outcome is uncertain, or Slack
succeeded but local logging failed (`remote_succeeded: true`). Inspect Slack before
retrying: creation and invitations are not safe to retry blindly. `name_taken` does
not prove that an existing channel is the one you meant to create; verify it and its
privacy before using it. Never fall back to creating a public channel after a failure.
