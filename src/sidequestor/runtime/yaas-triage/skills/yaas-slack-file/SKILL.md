---
name: yaas-slack-file
description: Upload a local file to a Slack conversation or download files attached to a Slack message using Sidequestor's Slack user token. Use for screenshots, images, PDFs, and other Slack attachments, including explicit interactive requests.
---

# Slack files

Use `sq slack-file` from an initialized Sidequestor workspace. Uploads use the authenticated Slack user token; `send` posts the file immediately. The Slack connector's text-message tool cannot attach local files.

## Upload

Resolve the exact Slack conversation ID (`C…`, `D…`, or `G…`); a member ID (`U…`) is not accepted. Inspect the local file before sending, and keep secrets out of screenshots and comments.

For an explicit request in an interactive session, use `interactive: true` without a quest ID. This mode is refused during a Sidequestor dispatch and does not write a quest timeline entry. Do not set `interactive` merely because an automated worker is running in a shell. The dispatch check is advisory: it relies on `SIDEQUESTOR_DISPATCH_TARGET`, so never unset or work around it.

```bash
sq slack-file send '{"interactive":true,"channel_id":"D...","file":"/absolute/path/screenshot.png","filename":"screenshot.png","initial_comment":"Step 1: Hub sign-in"}'
```

For quest work, supply the active `quest_id` instead. The quest needs `allow_send: true` or an exact claimed approval for this file, comment, and destination. The helper checks dispatch scope, stale threads, and file bytes, then logs a successful upload to the quest timeline.

```bash
sq slack-file send '{"quest_id":"quest-...","channel_id":"C...","file":"/absolute/path/image.png","initial_comment":"Here is the image","thread_ts":"<parent timestamp>"}'
```

When review is required, run `sq slack-file approval-spec '<same send JSON>'`, merge the returned `action_type`, `target`, and `message_text` into `sq approval write`, and use the resulting approval ID only after it is claimed. For reviewed uploads, keep the file bytes, name, title, comment, and destination identical to the approved spec. A Slack file has no draft state.

On success, `send` returns a `file_id`, `response_ts`, and `permalink`. Exit 3 means Slack may already have posted the file: inspect the conversation before retrying. Exit 4 means nothing posted and retry is safe. An interactive upload to a stale thread or in force-draft mode is denied without posting.

## Download

Fetch attachments from one message or one file ID. This is available in interactive sessions and quests without a send approval.

```bash
sq slack-file fetch '{"channel_id":"C...","ts":"<message timestamp>"}'
sq slack-file fetch '{"file_id":"F..."}'
```

The command returns local paths in `files[]` and explains omissions in `skipped[]`. By default it downloads images and PDFs up to 20 MiB each into a private temporary directory. Set `any_type: true` for other file types or `out_dir` for a chosen destination outside workspace `state/`. Open the downloaded file before relying on its contents.
