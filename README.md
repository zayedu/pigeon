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

Run the setup command for your agent and choose a username when prompted.

| Agent | One-time setup |
| --- | --- |
| Codex | `pigeon setup codex` |
| Claude Code | `pigeon setup claude` |
| Cursor CLI | `pigeon setup cursor` |
| Antigravity CLI | `pigeon setup antigravity` |

Reopen your agent in the project. If the agent reports missing Pigeon tools
or Pigeon can’t identify the chat, see [Client setup and wake](#client-setup-and-wake).
Pigeon uses the hosted relay by default. If you use several agents, run setup
once for each.

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

When wake is available, Pigeon can prompt an idle chat. Otherwise, the Pigeon
skill tells the agent to check periodically during its current turn when it
expects a message. You can also say “Check Pigeon” at any time. Either chat
can send a follow-up after the current question is answered.

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
  See [Client setup and wake](#client-setup-and-wake) if it still needs help.
- **A reply is pending.** Say “Show my Pigeon sessions” or “Check Pigeon.” When
  you reopen the same chat, check Pigeon to reconnect to its inbox. To finish
  a claimed request, use its original message ID. If you lost it, ask the
  sender to cancel and resend.
- **You want another connection.** Say “Start a new Pigeon session” for a fresh
  key or “Close this Pigeon session” to end the current one.
- **You need to update.** Run `pigeon update` and reopen the agent. If its Pigeon
  skill still looks outdated, rerun setup.

### Client setup and wake

<details>
<summary>Client-specific setup and launch options</summary>

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
- **Cursor CLI.** If Pigeon can’t identify your chat, or you want automatic
  wake, use `pigeon wake cursor`. To resume a chat, close the old instance
  and add its saved session ID to the command.
- **Antigravity CLI.** Setup installs the skill. If your chat lacks Pigeon’s
  MCP tools or a chat ID, open it with `pigeon antigravity` to supply both.
  Antigravity’s tool approval settings apply. Add the printed session ID to
  reopen the same chat.

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
