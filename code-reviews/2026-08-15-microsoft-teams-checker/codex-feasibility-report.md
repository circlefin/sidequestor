# Microsoft Teams Checker Feasibility Report

Date: 2026-08-15

## Decision

**Conditionally feasible.** YAAS can listen to Microsoft Teams alongside Slack, but it cannot be implemented as another 60-second upstream poller. Microsoft permits change polling of a Teams resource only once per day and directs apps that need prompt updates to Graph change-notification subscriptions. A compliant integration therefore needs a small public HTTPS webhook receiver that writes events to a durable inbox; the ordinary YAAS checker can continue polling that local inbox every minute.

Proceed with a read-only proof of concept only after obtaining an administrator-controlled Microsoft 365 work or school test tenant. Do not present it as production-ready until it has passed a real-source test in that tenant.

## Is A Personal Teams Free Account Useful?

Not for the integration test. Microsoft marks personal-account delegated access as unsupported for listing chats, reading chat or channel messages, and subscribing to their changes. Configuring an Entra app to accept personal Microsoft accounts does not override each Graph endpoint's account restrictions.

The personal account has limited secondary value:

- It can help inspect the consumer Teams interface.
- It can later act as an external participant in a federation test, if the test organization permits personal users.
- It cannot validate OAuth consent, Graph message reads, subscriptions, renewals, channels, or tenant policy.

A meaningful test needs a Microsoft 365 work or school tenant, two test users, Teams licenses, and control of its Entra app registrations. An eligible Microsoft 365 Developer Program E5 sandbox is suitable, but sandbox eligibility is restricted; a disposable Microsoft 365 trial tenant is the practical fallback.

## Recommended Architecture

```text
Microsoft Teams
  -> Microsoft Graph change notification
  -> hosted public HTTPS relay and durable queue
       - validates subscription challenge and clientState
       - deduplicates notifications
       - durably appends normalized events
  -> authenticated local pull into Teams inbox
  -> teams_chat / teams_channel YAAS checker
  -> existing dirty-watch dispatch and approval flow

subscription maintainer
  -> creates and renews Graph subscriptions
  -> records expiry, auth, lifecycle, and delivery health
```

The relay is an adapter, not a second orchestration system. It should only authenticate notifications, normalize them, enqueue them, and expose an authenticated drain endpoint. It must retain notifications while the laptop is asleep. Quest selection, watermarks, dispatch, review, and actions remain owned by YAAS. A temporary HTTPS tunnel to localhost is adequate for the Phase 0 spike, but not for production delivery.

Use two checker types rather than a generic `teams` type:

| Type | Identity | Initial scope |
|---|---|---|
| `teams_chat` | `chat_id` | One known one-to-one or group chat |
| `teams_channel` | `team_id` + `channel_id` | One known channel, including replies |

Their discovery, consent, message shapes, and reply semantics differ enough that combining them would create branching throughout one checker. Both manifests should declare `upstream: "microsoft_teams"`. They must not use the `slack_` prefix because the current tick deliberately reserves that prefix for Slack's infrastructure gate.

## Authentication And Permissions

Start with delegated work or school authentication through an Entra public-client app and Microsoft Authentication Library (MSAL) device-code flow. Store the serialized token cache in macOS Keychain; do not introduce a client secret into `.env` or print tokens through checker output.

The least-invasive first target is a known chat using delegated `Chat.Read`. Microsoft lists that delegated permission as not requiring administrator consent, although a tenant can still disable user consent. Reading channel messages uses delegated `ChannelMessage.Read.All`, which does require administrator consent. Application-wide delta or tenant capture uses broad application permissions and administrator consent, so it is inappropriate for the first version.

No Azure billing configuration is required for these Teams APIs as of 25 August 2025. Microsoft's old payment-model page is retained only as deprecated reference.

## Delivery And Watermark Safety

The webhook must acknowledge quickly and persist before returning success. Graph notifications can be duplicated, delayed, retried, or delivered out of order. The inbox therefore needs a stable event key, append-before-ack behavior, and a processed cursor separate from Graph subscription state.

The checker should emit the existing `clean`, `dirty`, `hold`, `ratelimited`, `error`, and `misconfig` contract. It may advance only across a fully persisted local prefix. If an event references a message that must be fetched from Graph and that fetch fails, it must `hold` or report a transient outcome, never advance past the event.

Avoid rich resource notifications initially. Receive the lightweight notification, then fetch the named message. This avoids certificate-based payload decryption and allows longer-lived subscriptions. Chat-message subscriptions still expire quickly, so renewal and lifecycle handling are mandatory, monitored components rather than setup scripts run once.

## Implementation Sequence

### Phase 0: Tenant And Spike

1. Obtain an admin-controlled work or school test tenant and two test users.
2. Register a single-tenant Entra app and prove delegated `Chat.Read` against one known chat.
3. Expose a temporary HTTPS callback and prove validation, notification delivery, renewal, and message retrieval.
4. Record representative sanitized payloads as test fixtures.

Stop here if any permission or tenant-policy assumption fails. Fakes alone are not sufficient to authorize rollout.

### Phase 1: Read-Only Chat MVP

1. Add a small Graph auth/client surface using MSAL and Keychain-backed cache storage.
2. Add the hosted HTTPS relay, durable queue, authenticated local drain, deduplication, and subscription-maintenance command.
3. Add executable `checkers/teams_chat.py` and `checkers/teams_chat.watch.json` with `chat_id` identity.
4. Normalize message body HTML, mentions, edits, and deletions before dispatch.
5. Update checker-authoring registration consumers, quest creation/dispatch guidance, README, architecture, operations documentation, and `.env.example` only for non-secret relay settings.
6. Keep all outbound Teams sending disabled. A fired watch should produce the existing local draft/review behavior.

### Phase 2: Channel Listening

1. Obtain administrator consent for `ChannelMessage.Read.All`.
2. Add `teams_channel` with `team_id` and `channel_id` identity.
3. Cover root posts and replies explicitly; do not assume a root-message list represents the complete thread.
4. Validate private/shared channel behavior and membership loss in the real tenant.

### Phase 3: Optional Replies

Add a separate `teams-send.py` surface only if there is a real need. It must preserve YAAS draft-first approval, log sends, classify Teams infrastructure health, and use delegated send permissions. Do not add sending implicitly as part of listening.

## Required Tests

- Subscription validation, renewal, expiry, lifecycle notification, and authorization loss.
- Duplicate, delayed, reordered, edited, deleted, root, reply, and mention events.
- Durable append before webhook acknowledgement and restart recovery.
- Inbox pagination, full-page tie safety, monotonic cursor advance, and no advance after a failed message fetch.
- `401`/`403` as actionable misconfiguration; `429`, timeout, and `5xx` as transient outcomes honoring `Retry-After`.
- Malformed payloads, forged `clientState`, wrong tenant/resource IDs, and hostile message HTML.
- Token-cache secrecy and assurance that credentials never enter logs or checker stdout.
- Existing checker contract, dirty dispatch, documentation contract, and full YAAS test suite.
- One final real-source run, as required by the checker-authoring doctrine.

## Main Risks

| Risk | Severity | Mitigation |
|---|---|---|
| No work/school tenant for real validation | High | Block production rollout until one is available |
| Public webhook expands the local-only trust boundary | High | Minimal relay, HTTPS, `clientState`, allowlisted tenant/resources, durable queue |
| Subscription expiry silently stops listening | High | Automatic renewal, lifecycle handling, expiry dashboard alert |
| Channel replies or out-of-order events are skipped | High | Separate channel adapter, durable cursor, explicit reply tests |
| Tenant consent policy blocks permissions | Medium | Start with delegated `Chat.Read`; treat channel support as a separate admin-approved phase |
| Authentication cache leaks | Medium | MSAL plus Keychain, redaction tests, no client secret |
| Graph throttling or outage | Medium | Honor `Retry-After`, retry without cursor advance, surface health visibly |

## Go / No-Go

**Go** for a tenant-backed, read-only `teams_chat` spike and then the webhook-to-local-inbox MVP.

**No-go** if any required constraint is personal Teams Free support, direct one-minute Graph polling, localhost-only operation with no reachable relay, or production rollout without a real Microsoft 365 tenant test.

## Official Sources

- [Teams API polling requirements](https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview?view=graph-rest-beta)
- [Teams message change notifications](https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage)
- [Webhook delivery requirements](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)
- [List messages in a chat](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0)
- [List channel messages](https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0)
- [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
- [Teams API licensing update](https://learn.microsoft.com/en-us/graph/teams-licenses)
- [Microsoft 365 Developer Program FAQ](https://learn.microsoft.com/en-us/office/developer-program/microsoft-365-developer-program-faq)
- [MSAL Python token-cache guidance](https://learn.microsoft.com/en-us/entra/msal/python/advanced/msal-python-token-cache-serialization)
