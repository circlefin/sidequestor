# Microsoft Teams Checker: Official API Feasibility

Date: 2026-08-15
Scope: Microsoft Graph support and constraints only. No runtime changes were made.

## Verdict

Feasible for Microsoft 365 work or school tenants, but not as a direct clone of the Slack polling checker.

The safest production shape is:

1. Microsoft Graph sends Teams message change notifications to a public HTTPS endpoint.
2. That endpoint verifies and stores events in a small local inbox or durable queue.
3. A normal YAAS checker reads that inbox and emits the existing checker JSON contract.
4. A separate maintenance task renews Graph subscriptions before they expire.

A free personal Teams account is not sufficient for a live end-to-end Graph test. Microsoft marks personal-account delegated access as unsupported for listing chats, listing chat messages, listing channels, and subscribing to chat or channel messages. It remains useful only for manually understanding the consumer Teams UI and, later, testing limited personal-to-organization chat behavior from the outside.

## API Options

### Specific chat or channel

Microsoft Graph can list messages from a known chat with `GET /chats/{chat-id}/messages`. Delegated work or school access requires `Chat.Read`; application access can use resource-specific `ChatMessage.Read.Chat` or tenant-wide `Chat.Read.All`. Personal Microsoft accounts are explicitly unsupported. The list API supports paging, descending ordering, and limited time filtering, but not delta tokens. [List messages in a chat](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0)

For channel messages, Graph supports `GET /teams/{team-id}/channels/{channel-id}/messages`; replies are either expanded or fetched separately. The companion channel discovery API also explicitly excludes personal Microsoft accounts. [List channel messages](https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0), [List channels](https://learn.microsoft.com/en-us/graph/api/channel-list?view=graph-rest-1.0)

Graph change notifications are the better fit for near-real-time monitoring. A specific channel subscription uses `/teams/{team-id}/channels/{channel-id}/messages`; a specific chat uses `/chats/{chat-id}/messages`. Work or school delegated permissions are `ChannelMessage.Read.All` and `Chat.Read` respectively. Application permissions are also supported, including narrower resource-specific consent options, though Microsoft currently notes that the chat-specific option is beta-only. Personal Microsoft accounts are unsupported for both. [Teams message change notifications](https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage)

### All chats for one user or the tenant

Graph provides an incremental delta feed at `/users/{user-id}/chats/getAllMessages/delta`. It returns opaque next and delta links and covers new or updated chat messages from the last eight months. This is application-only and requires `Chat.Read.All` or `Chat.ReadWrite.All`; neither work/school delegated access nor personal delegated access is supported. [Chat message delta](https://learn.microsoft.com/en-us/graph/api/chatmessage-delta?view=graph-rest-1.0)

Tenant-wide message subscriptions exist at `/teams/getAllMessages` and `/chats/getAllMessages`, but require broad application permissions (`ChannelMessage.Read.All` or `Chat.Read.All`). A user-level subscription, `/users/{user-id}/chats/getAllMessages`, is also available, with work/school delegated or application access, but no personal-account support. [Teams message change notifications](https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage), [Teams API licensing](https://learn.microsoft.com/en-us/graph/teams-licenses)

## Authentication And Consent

Delegated permissions operate as a signed-in work or school user and are the least invasive choice for monitoring only that user's known chats or channels. They require an interactive OAuth setup and renewable token storage. Some delegated Teams scopes can still require tenant administrator consent depending on tenant policy and the selected permission.

Application permissions operate without a signed-in user and are suitable for a launchd service, delta feed, or tenant-wide subscription. The relevant broad message-read application permissions require administrator consent. Resource-specific consent can narrow an installed Teams app to one chat or team, but introduces Teams app packaging and installation; it is not simply an OAuth scope toggle. Microsoft's permissions reference marks `ChatMessage.Read.All` as requiring admin consent. [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)

An app registration may be configured to accept personal Microsoft accounts, but that does not make an individual Graph API support those accounts. The Teams message endpoints above explicitly reject personal delegated accounts. [Microsoft identity supported account types](https://learn.microsoft.com/en-us/entra/identity-platform/howto-modify-supported-accounts)

## Polling, Webhooks, And Runtime Fit

Microsoft says an app polling a Teams resource for changes may do so only once per day. For more frequent updates it should use change-notification subscriptions; when polling new messages, Microsoft directs developers toward date ranges or the chat-message delta API. Failure to follow the polling rules can lead to throttling or suspension. Therefore a normal YAAS checker issuing a Graph list request every 60 seconds is not acceptable. [Teams Graph polling requirements](https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview?view=graph-rest-beta)

Webhooks require a publicly accessible HTTPS endpoint. Graph validates the callback during subscription creation, expects prompt responses, and may drop notifications when the endpoint is persistently slow or unavailable. A localhost dashboard cannot receive these callbacks without a secure tunnel or a small hosted relay. [Receive Graph notifications through webhooks](https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks)

Teams `chatMessage` subscriptions last at most three days, or under one day when rich resource data is included. Requests lasting over one hour also require lifecycle notifications. Renewal and lifecycle handling are therefore required runtime responsibilities, not optional setup work. [Graph subscription lifetime](https://learn.microsoft.com/en-us/graph/api/resources/subscription?view=graph-rest-1.0), [Teams message change notifications](https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage)

Microsoft documents service throttles including one request per second per app, tenant, and individual chat or channel for Teams APIs. Clients must handle `429` responses and back off. This is manageable at YAAS scale, but must be part of the adapter rather than left to the generic checker loop. [Microsoft Graph throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits)

## Licensing And Cost

Starting August 25, 2025, Microsoft removed metering and billing-configuration requirements from the Teams APIs listed in its former payment-model document. The `model` query parameter is now ignored. Meeting AI insights and Data Loss Prevention operations remain explicit exceptions, but neither is part of this checker. A specific chat or channel subscription is still preferable because it minimizes permission scope, not because of billing. [Teams API licensing](https://learn.microsoft.com/en-us/graph/teams-licenses)

## Value Of A Personal Teams Free Account

Teams Free is a consumer product built around personal chats and Communities. Microsoft distinguishes this from business "Teams and channels." [Teams Free subscription comparison](https://support.microsoft.com/en-us/office/learn-more-about-subscriptions-for-microsoft-teams-free-1061bbd0-6d97-46a6-8ca0-21059be3eee3)

Your personal account cannot provide a meaningful authentication or message-ingestion test because the relevant Graph endpoints all say delegated personal Microsoft accounts are unsupported. It can help only with:

- Manual UI and payload expectation discussions.
- A later federation test where an organization tenant explicitly permits chats with personal users.
- Acting as an external message sender after the integration has already been proven inside a real tenant.

It cannot validate app registration, organizational consent, channel discovery, message reads, subscription creation, renewal, lifecycle notifications, or admin controls.

For a genuine isolated test, use a work/school tenant you administer. Qualifying Microsoft 365 Developer Program members can obtain an E5 development sandbox with Teams and test users, but Microsoft now limits sandbox eligibility. [Microsoft 365 developer sandbox](https://learn.microsoft.com/en-us/office/developer-program/microsoft-365-developer-program-get-started), [Developer Program eligibility](https://learn.microsoft.com/en-us/office/developer-program/microsoft-365-developer-program-faq)

## Recommended Proof Of Concept

1. Obtain a disposable Microsoft 365 tenant with two test users and administrator access.
2. Start with one known chat or channel, not tenant-wide capture.
3. Register a single-tenant Entra application and request only the least-privileged permission for that resource.
4. Implement a public HTTPS webhook relay with validation, `clientState` verification, deduplication, and durable storage.
5. Exclude rich resource data initially. On notification, fetch only the referenced message. This avoids encryption-certificate handling and permits longer subscriptions.
6. Add a renewal task and explicit monitoring for subscription expiry, authorization failure, throttling, and callback backlog.
7. Feed normalized events into a local Teams checker that obeys the existing YAAS checker manifest/result contract.
8. Test create, edit, delete, reply, mention, duplicate notification, expired subscription, invalid token, `429`, and relay outage behavior before considering broader scope.

## Feasibility Decision

**Go, conditional on obtaining an administrator-controlled Microsoft 365 test tenant and accepting a public webhook component.**

Do not proceed if the requirement is personal Teams Free support, localhost-only operation with no public relay, or direct one-minute message-list polling. Those constraints are incompatible with Microsoft's documented Teams Graph support and polling policy.
