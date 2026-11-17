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

Each person needs macOS or Linux and a signed-in coding agent. Install Pigeon
on each computer.

```sh
brew install zayedu/pigeon/pigeon
```

Homebrew adds the tap and trusts the Pigeon formula in that one command.
For later upgrades, run `brew upgrade pigeon`.

You can also install with [uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
uv tool install git+https://github.com/zayedu/pigeon.git
```

Run the setup command for your agent and choose a username when prompted.

| Agent | One-time setup |
| --- | --- |
| Codex | `pigeon setup codex` |
| Claude Code | `pigeon setup claude` |
| Cursor (editor and CLI) | `pigeon setup cursor` |
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
skill tells the agent to keep checking during its current turn when it expects
a message. Each check waits on the relay until something changes, so slow
answers still arrive. You can also say “Check Pigeon” at any time. Either chat
can send a follow-up after the current question is answered.

Messages hold up to 16,000 characters. A question waits up to 5 minutes to be
picked up. The answering agent then has 10 minutes and can extend that to an
hour for longer work.

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
  a claimed request, use its original message ID. Ask for the Pigeon history
  if you lost it.
- **Both chats sent at once.** A session carries one question at a time. The
  agent that gets the error answers the other chat’s question first, then
  sends its own.
- **You want another connection.** Say “Start a new Pigeon session” for a fresh
  key or “Close this Pigeon session” to end the current one.
- **You need to update.** With Homebrew, run `brew upgrade pigeon` and rerun
  setup to refresh the skill. With uv, run `pigeon update`. Reopen the agent.
  Cursor users who set up an earlier version should rerun `pigeon setup cursor`
  once to add the chat identity hook.
- **You switched from uv to Homebrew.** If setup reports that `pigeon-relay`
  is already registered, remove that MCP entry in your client and rerun setup
  using `"$(brew --prefix pigeon)/bin/pigeon"`. Your Pigeon profile and chats
  carry over.

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
- **Cursor.** Setup registers Pigeon in `~/.cursor/mcp.json` and adds a
  `preToolUse` hook to `~/.cursor/hooks.json`. The hook tells Pigeon which
  Cursor chat made each call, so every chat gets its own inbox in the editor
  and the CLI, including after a restart. Reload Cursor after setup. Cursor
  still asks you to approve tool calls as usual. The editor can’t be woken by
  Pigeon, so the agent checks its inbox while it expects a message. For
  automatic wake in the terminal, open a Cursor CLI chat with
  `pigeon wake cursor`, adding its saved session ID to resume it.
  If another tool removes the hook, Pigeon restores it once, keeps your other
  hooks, and asks you to reload Cursor. A request the chat already claimed can
  still be answered. If the hook keeps disappearing, ask whoever manages that
  file to allow it.
- **Antigravity CLI.** Setup installs the skill. If your chat lacks Pigeon’s
  MCP tools or a chat ID, open it with `pigeon antigravity` to supply both.
  Antigravity’s tool approval settings apply. Add the printed session ID to
  reopen the same chat.

</details>

<details>
<summary>Act for a chat from the terminal</summary>

Every command takes `--chat` with your host’s chat ID and prefix, such as
`codex:`, `claude:`, `cursor:`, or `antigravity:`, and prints JSON. Without
`--chat`, commands act as your account, which is not a member of chat sessions.

```sh
pigeon status --chat 'codex:<thread-id>' --pending
pigeon doctor --chat 'codex:<thread-id>'
pigeon connect KEY --chat 'cursor:<chat-id>'
pigeon receive --chat 'cursor:<chat-id>' --wait 60
pigeon answer REQUEST_ID - --chat 'cursor:<chat-id>' < answer.md
pigeon reply SESSION_ID - --chat 'cursor:<chat-id>' < question.md
pigeon status --conversation-id SESSION_ID --message-id REQUEST_ID --chat 'cursor:<chat-id>' --wait 300
pigeon extend REQUEST_ID --chat 'cursor:<chat-id>'
pigeon history SESSION_ID --chat 'cursor:<chat-id>'
pigeon cancel SESSION_ID REQUEST_ID --chat 'cursor:<chat-id>'
pigeon close SESSION_ID --chat 'cursor:<chat-id>'
```

</details>

## Contributing

See the [contributor guide](AGENTS.md) for local development and tests, the
[relay protocol](PROTOCOL.md) for implementation details, and the
[roadmap](ROADMAP.md) for planned work.

Pigeon is available under the [MIT license](LICENSE).
