# Pigeon next phase

Interactive clients register a local MCP server through `pigeon setup`.
`create_mcp` resolves the host's chat ID to a relay inbox. The WebSocket
client submits and inspects turns, and the relay enforces membership and
persists them in SQLite or libSQL. The listener in `pigeon/wake.py` asks a
supported host to start a turn. The headless daemon is a separate workflow.

1. **Skill updates and behavior.** The previous `install_skill` wrote only
   missing files, and `update` only reinstalled the executable. Track the
   installed digest so stock copies refresh and local edits survive.
   Describe claim before answer, the text limit, bounded status checks,
   and recovery using existing IDs. Test setup and update against both
   stock and edited copies.
2. **Wake reliability.** `open_inbox` can replace a live listener on every
   share or return a cached inbox without checking its listener. Report
   listener availability and notification attempts per chat. Keep relay
   state authoritative for claims and responses. Verify reconnect and
   duplicate suppression locally, then verify idle wake in real clients.
3. **Architecture.** Remove shared mutable inbox state where the caller
   already returns a chat-specific config. Check migrations against both
   stores and preserve protocol v1 for existing clients. Use regression
   tests for two concurrent chats on one profile and for restarted chats.
4. **Antigravity.** Verify its MCP registration, skill discovery, and
   source of stable chat identity. Add one-command setup only when two
   chats on one account can demonstrate isolation. Treat manual receive
   as the initial target and gate native wake on a host-supported API.
   Google documents stdio MCP registration and exposes a conversation ID
   in hook payloads. Verify whether the MCP process itself sees that same
   ID across restarts. If it does not, test an ACP wrapper that owns the
   session ID, as Pigeon does for Cursor, before offering normal setup.
5. **Performance.** Measure submit-to-claim, notification-to-claim, and
   submit-to-answer p50/p95 separately on an isolated relay. Count socket
   opens and idle RPCs. Compare the existing WebSocket path with reusing
   connections or a scoped watch before changing transport or streaming.
6. **Capabilities and attachments.** Negotiate capabilities before
   sending new payload types. Prefer opaque attachment IDs in messages
   backed by short-lived storage with session authorization, a hard byte
   limit, digest verification, and expiry. Compare authenticated relay
   downloads with short-lived signed URLs before choosing storage. Test
   cross-session access, oversized files, tampering, and cleanup first.
7. **Context and CLI.** Extend existing structured PR context with
   optional commit and file references via capability negotiation. Extend
   status and doctor before adding commands. Keep machine-readable output
   and verify that CLI inspection preserves chat boundaries.

## Local performance baseline

Twelve sequential requests through a local SQLite relay on macOS, with a
scripted receiver and reused request sockets. Values are p50 / p95.

| Measure | Time |
| --- | --- |
| Open a new WebSocket | 1.6 / 1.8 ms |
| Submit to claim | 6.2 / 6.8 ms |
| Watch result to claim | 2.4 / 2.6 ms |
| Submit to finish | 8.0 / 8.9 ms |

An unpaced 30-request run hit the per-agent rate limiter in
`server.create_app`. The previous listener issued a list request after
each two-second watch. Capable relays now use one scoped activity watch
per five idle seconds, plus a capability heartbeat every 30 seconds for
attached listeners. An 11-second local run counted 12 calls on the legacy
path and four on the scoped path, including startup calls. At steady state
the configured intervals give about 60 versus 14 calls per minute. Hosted
storage still checks for cross-instance changes while waiting.

A second 12-request run exercised the new listener with a scripted wake
callback. Submit to callback took 3.2 / 3.9 ms. Callback to claim took
5.3 / 6.1 ms. Submit to finish took 11.3 / 13.2 ms. Provider and WAN
latency require separate live measurements.

## Implementation and release checks

- Managed skill refresh, chat-scoped wake state, scoped activity watch,
  and the baseline relay fallback are implemented.
- Antigravity setup installs its CLI skill. The Pigeon launcher registers
  MCP through ACP and uses the ACP session ID for isolation and resume.
  The automated tests use an ACP stub. Verify two live `agy` chats before
  claiming real-host compatibility. The launcher uses manual delivery.
- Peer advertisements and file transfers are additive. Attachment bytes
  use bounded SQLite or libSQL storage behind authenticated HTTP. This
  avoids a separate storage service and uses the relay's session checks.
  The local tests exercise libSQL bindings without a hosted database.
- Structured GitHub references produce links inside the normal message.
  This keeps existing clients compatible without storing or transferring
  repository content. `status --chat` and `doctor --chat` use existing
  local chat credentials to inspect a specific inbox.
- The live Codex CLI check passed. Run `uv run --locked python tests/smoke_wake.py`
  with a signed-in Codex client to repeat it against a disposable local
  relay and chat. Before deployment, run the live Claude and Antigravity
  checks and smoke test attachments against a nonproduction hosted database.

## Reliability and security audit

- **Wake.** `wake.listen` used a successful host call to suppress all further
  attempts. It now makes up to three attempts for work that remains queued.
  A relay reconnect or listener replacement in the same MCP process preserves
  the attempt history. Restarting the process allows a fresh set of attempts.
  `pigeon_share` retains the key in `result` and adds structured wake status.
  `pigeon_status` reports the most recent local claim separately.
- **File access.** MCP transfer handlers previously opened caller-provided
  paths. Host permission settings vary, so Pigeon now uses the terminal
  transfer commands for local file access. The user confirms the exact path
  and session. Existing attachment IDs and the HTTP protocol remain valid.
- **Storage.** Per-session counts did not stop one account filling the relay
  across many chats. The upload transaction now checks account and relay
  byte limits. A separate uploader table preserves the prior attachment row
  format and lets old rows count against both accounts until expiry. The
  relay's cap applies to live payload bytes. Database pages, WAL files,
  backups, and provider storage usage follow their own retention rules.
- **Antigravity.** A checksum-verified `agy` 1.5.9 binary initialized ACP
  protocol 1. Creating a chat stopped at Google authentication. The signed-in
  registration, resume, and manual receive paths remain a live release check.
  The ACP stub covers registration and the local relay tests cover identity
  isolation and resume. Live checks since the audit verified both manual
  receive and automatic wake through `pigeon antigravity` on `agy` 1.5.9.
  The idle wake test reached a completed reply with ACP tool approvals.
- **Relay.** The previous watch opened separate authentication and lookup
  connections and ordinary turn updates woke all listeners. Activity watch
  now uses one database connection per poll, and turn updates signal only their
  participants. SQLite's query plan now uses `queued_by_recipient` rather
  than scanning the turns table. The legacy branch covers older rows without
  per-turn recipients. Cross-instance notices still rely on the one-second
  shared-store poll. Each shared-store transaction still opens a connection,
  and the 16-socket limit is per credential per relay process.

Run `uv run --locked python tests/bench_relay.py 100 1000 --seconds 6` to
repeat the local WebSocket load test. It exercises the hosted polling code
with SQLite and measures 12 sequential submit-to-claim samples under load.

| Idle clients | Idle store connections/listener/s, before → after | Submit-to-claim p50 / p95, before → after |
| --- | --- | --- |
| 100 | 2.33 → 1.00 | 284 / 437 ms → 6.4 / 24.7 ms |
| 1,000 | 1.29 → 1.00 | 3,291 / 3,603 ms → 6.5 / 7.5 ms |

An intermediate 10,000-client run opened all idle sockets, but a new
sender's WebSocket handshake timed out under load. It produced no valid
message latency sample. These tests establish neither a hosted Turso
capacity limit nor a production latency estimate.
