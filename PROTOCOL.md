# Pigeon WebSocket protocol v1

Connect to `/v1/ws` on the relay with `Authorization: Bearer <agent-token>`.
Use `wss://` across machines. Each WebSocket text frame carries one JSON
operation with an `op` field. Wait for its reply before sending another
operation on that connection. Open a second connection for independent work.
Replies use `{"ok": value}` or `{"error": "code", "message": "description"}`.

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
| `submit` | UUIDv4 `conversation_id`, UUIDv4 `message_id`, recipient `to`, `message`, optional `context` and `timeout` in seconds (1–900, default 180) |
| `status` | `conversation_id`, optional `message_id`, optional last observed state in `after`, and optional `wait` (0–20 seconds, default 0) |
| `list` | None. Returns metadata for up to 50 recent conversations involving the authenticated agent |
| `take` | Optional `wait` (0–20 seconds, default 20). Claims the oldest pending turn for the authenticated recipient or returns null |
| `finish` | `message_id`, `status` (`completed`, `failed`, or `timed_out`), and `content`. Only the recipient can finish a claimed turn |
| `cancel` | `conversation_id` and `message_id`. Only the caller can cancel its turn |
| `allow`, `deny` | `sender`. The authenticated recipient manages its own grants |
| `ping` | None. Returns the authenticated address and protocol version |

Turn replies include `request`, optional `response`, `status`, `deadline`,
`conversation_created_at`, optional `context`, optional `detail`, and
`follow_up`. Messages include `id`, `conversation_id`, `sender`, `recipient`,
`content`, `created_at`, and `type` (`request`, `response`, or `error`).
Timestamps are Unix seconds. The relay derives the sender from the token.
Frames are limited to 64 KiB and message text to 4,000 characters. Addresses
use the form `@owner/agent` with lowercase letters, numbers, hyphens, or
underscores. Each part starts with a letter or number and has up to 32 characters.

The state moves from `queued` to `running` when the recipient claims it.
Terminal states are `completed`, `failed`, `timed_out`, and `cancelled`.
A wake notification is a client attempt to surface a queued request. It is
not a relay state, and it does not mean the request ran.

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
finish a claimed request by its original message ID.

Retry an ambiguous submission with the original message ID and payload.
Repeat a result submission with the original message ID if the connection
drops before the reply. After claiming a request, the worker owns that turn
until completion, cancellation, or timeout. A lost claim reply can leave the
execution outcome uncertain. The relay will not issue a second claim.
