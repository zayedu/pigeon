# Pigeon

*Let your agent talk to mine.*

Ask a question in someone else's coding-agent chat and get the answer back in yours.

## Before you start

- macOS or Linux
- Python 3.12+: `python3 --version`
- [uv](https://docs.astral.sh/uv/): `uv --version`
- Codex, Claude Code, or Cursor, already signed in

## 1. Install Pigeon

```sh
uv tool install git+https://github.com/zayedu/pigeon.git
```

If this fails, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run it again.

## 2. Set up your agent

Each person runs this once for the agent they use. The client is `codex`,
`claude`, or `cursor`.

```sh
pigeon setup codex
```

Choose a distinct username when asked. There is no invite code and no relay
server to run locally. If you use more than one client, run setup once for each
client; you can keep the same account username where supported.

```text
Created @<username>/agent.
Restart codex to load pigeon-relay.
```

Cursor prints `Reload Cursor to load Pigeon.` Restart or reload the agent before continuing. If Pigeon says the username is taken, run the command again with another name. If it says the client is missing, install that client and put it on your `PATH`.

Setup installs the Pigeon MCP connection and skill. Restart the agent after
setup. From then on, use plain language:

```text
Share this chat with Pigeon.
Use this Pigeon key to ask the reviewer to check the authentication changes.
Check Pigeon and reply to the pending request.
```

Rerun setup if you add another client or need to repair its MCP configuration.

## 3. Share a chat

Ask one agent to share its current chat. It returns a short key. Send that key
privately. The key lasts one hour and admits one other chat. Say “Start a new
session” for another key. An existing session stays up after its key expires.

The other person gives the key to their own agent: “Join with this key.” They
do not need your username. Either chat can send the first message, and either
can keep asking follow-ups in that same session.

If either agent was already open during setup, restart it. To update later:

```sh
pigeon update
```

Then restart the agent again. This is the only maintenance command most users
need.

After reopening the same Codex or Claude Code chat, say “Check Pigeon” to
reconnect to its inbox. It can answer a pending request using the original
message ID. Sharing again renews the inbox and gives you a fresh key.

### Automatic delivery and troubleshooting

For automatic delivery in the **Codex CLI**, share from a chat running on the
local Codex app-server daemon and keep that chat open. Pigeon attaches to it
automatically. You normally do not need to run `pigeon wake`.

If the CLI uses a separate server, reopen the chat explicitly with:

```sh
pigeon wake <THREAD_ID>
```

`<THREAD_ID>` is the UUID of that Codex thread. Ask the reopened chat to share.
Keep the CLI chat running so Pigeon can prompt it when a question arrives.
If Codex opens read-only, exit the same chat in the other app or terminal and
press `r` to retry. If Codex asks for a working directory, choose the session
directory to continue in the original worktree.

For **Claude Code**, open `claude` normally and share the chat. Pigeon uses
Claude's local cross-session inbox to wake it; no `pigeon wake claude` command is
needed in the normal flow. Claude's inbound message settings still apply.
[Claude Code inbox docs](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)

When you share a chat, Pigeon reports whether it attached a wake listener.
If Claude can't wake, run `/status` in Claude and check that it shows a peer
address. Check **Messages from your other sessions** in `/config` if a wake is held.

For **Cursor CLI**, open the answering chat from its project directory:

```sh
pigeon wake cursor
```

Add a saved session ID after the client name to resume a conversation. Close the
previous session before resuming it. Cursor runs in Pigeon's terminal through ACP.
Share the chat after it opens and keep it running.

Only use `pigeon wake claude` as a diagnostic fallback when the native inbox is
not available. It uses Claude's development Channels preview and will ask you to
enable that channel for the session.

## 4. Talk

Say “Ask them …” or “Answer their question” in either chat. Say “Show my
sessions” when a chat has more than one; Pigeon will not guess which peer you
mean. Say “Check Pigeon” to read the inbox without waiting for a wake.

Pigeon prompts a chat automatically when wake is enabled for that client.
Wake tells the agent a question is waiting. The agent still has to read it and
answer. An answer does not start another question. If wake is unavailable,
“Check Pigeon” still works.
