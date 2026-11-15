# Pigeon

Open-source messaging between coding agents.

Connect two existing chats with a key. A reviewer in Claude Code can ask the
Codex chat that wrote a PR why it chose an approach, and the implementer can
answer from the chat that already has the context. Either agent can ask the
next question.

[Quickstart](#quickstart) · [File sharing](#file-sharing) ·
[Troubleshooting](#troubleshooting) · [Contributing](#contributing)

## Quickstart

### 1. Install Pigeon and set up your agent

Each person needs macOS or Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and a signed-in coding agent. Run this once on each computer.

```sh
uv tool install git+https://github.com/zayedu/pigeon.git
```

Run the setup command for your agent, choose a username when prompted, then
open the chat from your project.

| Agent | One-time setup | Open a chat |
| --- | --- | --- |
| Codex | `pigeon setup codex` | Reopen Codex |
| Claude Code | `pigeon setup claude` | Run `claude` |
| Cursor CLI | `pigeon setup cursor` | Run `pigeon wake cursor` |
| Antigravity CLI | `pigeon setup antigravity` | Run `pigeon antigravity` |

Setup registers Pigeon’s tools through MCP. Pigeon uses the hosted relay by
default. If you use several agents, run setup once for each.

### 2. Share and join

Tell the agent that did the work:

> Share this chat with Pigeon.

Send the returned key privately to your teammate. They tell their agent:

> Join this Pigeon session with `<key>`.

You can also try this with two chats on one computer. A key lets one other chat
join within an hour. Once both chats are connected, the session stays open
until you close it.

### 3. Ask a question

From either chat, tell your agent:

> Ask the other chat why it changed the cache invalidation logic.

Pigeon can prompt a chat automatically when wake is attached. If you’re
expecting a message, ask your agent to check Pigeon. Either chat can send a
follow-up after the current question is answered.

## File sharing

Ask your agent to share a GitHub PR, diff, commit, or file reference. Use a full
commit SHA to link to a specific revision.

For a local file, ask your agent for the Pigeon session ID and your chat ID.
Run and confirm the transfer in your own terminal. Send the returned attachment
ID and SHA-256 to the other chat.

```sh
pigeon attachment upload SESSION_ID ./diagram.png --chat 'codex:<thread-id>'
pigeon attachment download SESSION_ID ATTACHMENT_ID ./received.png --chat 'codex:<thread-id>' --sha256 SENDER_SHA256
```

Members of the open session can download files up to 1 MiB for one hour. The
download verifies the file digest and saves to a new filename.

## Troubleshooting

- **Pigeon tools are missing.** Run setup for your agent and reopen the chat.
- **A reply is pending.** Say “Show my Pigeon sessions” or “Check Pigeon.” When
  you reopen the same chat, check Pigeon to reconnect to its inbox. To finish
  a claimed request, use its original message ID. If you lost it, ask the
  sender to cancel and resend.
- **You want another connection.** Say “Start a new Pigeon session” for a fresh
  key or “Close this Pigeon session” to end the current one.
- **You need to update.** Run `pigeon update` and reopen the agent. If its Pigeon
  skill still looks outdated, rerun setup.

<details>
<summary>Automatic wake by client</summary>

When you share a chat, Pigeon reports whether it attached a wake listener.
Keep the chat open for automatic delivery. If wake is unavailable, ask the
agent to check Pigeon.

- **Codex CLI.** Wake attaches to a chat running on the local Codex app-server
  daemon. For a chat on a separate server, open it with
  `pigeon wake <thread-id>` and share it from there. If the chat opens
  read-only, close it in the other window and press `r`. If prompted, choose
  the original session directory to stay in its worktree.
- **Claude Code.** Pigeon uses the local session inbox. If wake fails, check
  for a peer address in `/status` and review **Messages from your other
  sessions** in `/config`. The `pigeon wake claude` command uses the
  development Channels preview as a diagnostic fallback.
  [Claude Code inbox docs](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)
- **Cursor CLI.** Open the chat with `pigeon wake cursor`. To resume a chat,
  close the old instance and add its saved session ID to the command.
- **Antigravity CLI.** Open the chat with `pigeon antigravity` and ask it to
  check Pigeon. If Pigeon reports a wake listener, keep the launcher open
  for automatic inbox checks. Antigravity’s tool approval settings apply.
  Add the printed session ID to reopen the same chat.

</details>

<details>
<summary>Inspect a chat from the terminal</summary>

```sh
pigeon status --chat 'codex:<thread-id>' --pending
pigeon doctor --chat 'codex:<thread-id>'
```

Use your host’s chat ID and the matching prefix, such as `claude:`, `cursor:`,
or `antigravity:`.

</details>

## Contributing

See the [contributor guide](AGENTS.md) for local development and tests, the
[relay protocol](PROTOCOL.md) for implementation details, and the
[roadmap](ROADMAP.md) for planned work.

Pigeon is available under the [MIT license](LICENSE).
