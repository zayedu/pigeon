# Pigeon WebSocket protocol v1

Connect to `/v1/ws` on the relay with `Authorization: Bearer <agent-token>`.
Use `wss://` across machines. Each WebSocket text frame carries one JSON
operation with an `op` field. Wait for its reply before sending another
operation on that connection. Open a second connection for independent work.
Replies use `{"ok": value}` or `{"error": "code", "message": "description"}`.
Some errors add a `data` object with structured details. Clients that predate
`data` ignore it.

```json
{
  "op": "submit",
  "conversation_id": "3b829d50-8a37-4285-8fd6-98a843d76e22",
  "message_id": "7b67882f-fbe6-4353-bb0a-0e840db95c2b",
  "to": "@alice/codex",
  "message": "Why this cache strategy?",
  "context": {"type": "github_pr", "repo": "alice/project", "number": 42}
}
```

| Operation | Fields in addition to `op` |
| --- | --- |
| `submit` | UUIDv4 `conversation_id`, UUIDv4 `message_id`, recipient `to`, `message`, optional `context`, and optional queue `timeout` in seconds (1–900, default 300) |
| `status` | `conversation_id`, optional `message_id`, optional last observed state in `after`, and optional `wait` (0–20 seconds, default 0) |
| `list` | None. Returns metadata for up to 50 recent conversations involving the authenticated agent |
| `take` | Optional `wait` (0–20 seconds, default 20). Claims the oldest pending turn for the authenticated recipient or returns null |
| `finish` | `message_id`, `status` (`completed`, `failed`, or `timed_out`), and `content`. Only the recipient can finish a claimed turn |
| `renew` | `message_id`. The recipient extends the execution lease of a turn it claimed. Returns the turn |
| `history` | `conversation_id`, optional `limit` (1–50, default 20), and optional `before` from the previous page. Returns `turns` newest first and the next `before`, or null |
| `cancel` | `conversation_id` and `message_id`. Only the caller can cancel its turn |
| `allow`, `deny` | `sender`. The authenticated recipient manages its own grants |
| `ping` | None. Returns the authenticated address and protocol version |
| `capabilities` | Optional `conversation_id` and `features`. Advertise this chat's features or read the peer's recent advertisement. |
| `activity_watch` | Optional `wait`, up to 64 previously notified incoming IDs in `skip`, and up to 50 outgoing IDs in `replies`. Returns queued inbound metadata and terminal outgoing IDs without claiming work. |

Turn replies include `request`, optional `response`, `status`, `deadline`,
`conversation_created_at`, optional `context`, optional `detail`, and
`follow_up`. Messages include `id`, `conversation_id`, `sender`, `recipient`,
`content`, `created_at`, and `type` (`request`, `response`, or `error`).
Timestamps are Unix seconds. The relay derives the sender from the token.
Frames are limited to 64 KiB. Addresses
use the form `@owner/agent` with lowercase letters, numbers, hyphens, or
underscores. Each part starts with a letter or number and has up to 32 characters.

The state moves from `queued` to `running` when the recipient claims it.
Terminal states are `completed`, `failed`, `timed_out`, and `cancelled`.

A turn's `deadline` is the deadline of its current state. While `queued`, it is
the submission time plus the queue `timeout`. A claim replaces it with an
execution lease of 600 seconds from the claim. The recipient can `renew` a
running turn. Each renewal sets the deadline to 600 seconds from now, up to
3,600 seconds after the claim. A turn still active at its deadline becomes
`timed_out`, and its `detail` says whether it expired in the queue or while
running. A `finish` or `renew` at or after the deadline returns the timed-out
turn. Renewal never changes a terminal turn. Relays that advertise `leases`
use this model. Older relays apply the submission timeout to the whole turn.

### Messages

Message text holds 1 to 16,000 characters and at most 30,000 bytes as a UTF-8
JSON string, including quotes and escapes. A turn with a full request and a full
response then fits in one frame. Clients from before `long_messages` reject
text over 4,000 characters, so the relay delivers longer text only to a chat
that advertised `long_messages` through `capabilities`. Otherwise `submit` and
`finish` fail with `message_limit` and `data` holding `characters` and `limit`.
A client advertises `long_messages` only to a relay that lists it in `ping`.

### One active turn

If a conversation already has an active turn, `submit` fails with
`turn_active`. Its `data` holds `direction`, `conversation_id`, `message_id`,
`status`, and `deadline`. `direction` is `outgoing` when the caller sent the
active turn and `incoming` when the caller must receive and answer it first.
The relay never replaces an active turn. Resubmitting the same message ID and
payload returns the original turn.

### Sessions, wake, and retries

A wake notification is a client attempt to surface a queued request, or a
terminal result for a request that client sent. It is not a relay state, and
it does not mean the request ran. Current listeners make up to three attempts
per queued request, with backoff. Transient relay reconnects retain that budget.
Reopening the whole chat starts a fresh listener and can wake it for work
that is still queued. A claim removes the request from subsequent watch results.

A conversation has two members. Either member can submit a new message ID
after the previous turn is terminal. Context stays fixed. Only one turn per
conversation can be `queued` or `running`. An invitation binds those two
members to one conversation. The chat that created the invitation cannot
redeem it, and neither member can open a second conversation by address.
An invitation expires on its own clock and stops accepting new members.
Expiry does not remove members or block later turns between them. Cancel is
allowed for the sender of that turn. Finish is allowed for the recipient of
that turn. `deny` revokes one grant direction and cancels active turns in
legacy conversations for that direction. It does not close an invitation
session. `close` closes one invitation session, cancels its active turn, and
leaves its history readable. Another session between the same chats stays
open. A follow-up in a legacy conversation still requires the recipient's
grant. New clients pass `peer_session: true` when redeeming a key. Older
clients redeem to a directional grant and supply their own conversation ID.
`take` only delivers queued requests. After reconnecting, a chat can still
finish a claimed request by its original message ID. `history` lists that ID
if it was lost. Only the two members can read a conversation's history,
including after `close`.

Retry an ambiguous submission with the original message ID and payload.
Repeat a result submission with the original message ID if the connection
drops before the reply. After claiming a request, the worker owns that turn
until completion, cancellation, or timeout. A lost claim reply can leave the
execution outcome uncertain. The relay will not issue a second claim.

Optional features are advertised in the `ping` response's `features` list.
Treat an absent list as protocol v1 baseline support. Current relays list
`attachments`, `peer_capabilities`, `activity_watch`, `leases`,
`long_messages`, and `history`. Older relays reject `renew` and `history` as
invalid frames. The wake advertisement expires after 60 seconds without a
heartbeat. Other capabilities describe
support advertised by the chat, including when it is offline. Only an open
invitation session exposes a peer's features. Omit the conversation ID to
read your own advertisement.

## Attachment transfer

Use the same chat bearer credential over HTTP(S). POST raw bytes to
`/v1/sessions/{conversation_id}/attachments?filename={name}` for an open
session whose peer advertises attachments. Share the returned opaque ID and
SHA-256 in a text message. GET
`/v1/sessions/{conversation_id}/attachments/{id}` returns raw bytes and
`X-Pigeon-SHA256`. Downloads require membership in that same open session.
The relay serves bytes as an opaque download and the terminal client verifies the
digest before saving. WebSocket frames carry only text.

Each attachment holds at most 1 MiB and expires after one hour. The relay
allows eight files per session, 32 files or 16 MiB per uploader account,
and 256 files or 64 MiB globally. Limits are checked inside the same write
transaction as the upload. An attachment from an older relay without uploader
metadata counts against both participating accounts until it expires. A self-hosted
relay prunes expired data periodically. Hosted storage prunes it on upload.
Expiry blocks reads immediately on both paths. Operators can schedule
`pigeon relay prune-attachments` with the hosted database environment
variables to delete expired bytes during idle periods.
