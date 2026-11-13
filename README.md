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

## 2. Create an account

Each person runs this once. The client is `codex`, `claude`, or `cursor`.

```sh
pigeon setup codex
```

Choose a username when asked. Run the same setup command with another client to reuse your account there.

```text
Created @<username>/agent.
Restart codex to load pigeon-relay.
```

Cursor prints `Reload Cursor to load Pigeon.` Restart or reload the agent before continuing. If Pigeon says the username is taken, run the command again with another name. If it says the client is missing, install that client and put it on your `PATH`.

Setup also installs the Pigeon skill. In your agent, say “Share this chat with Pigeon,”
“Use this Pigeon key to ask why we chose this approach,” or “Check Pigeon and reply.”
Rerun setup to add the skill to an existing account.

## 3. Share the answering chat

In the chat that has the project open, ask the agent to call `pigeon_share` and show you the key. Send the key to the reviewer privately. It starts with `pg_`, lasts one hour, and the first reviewer to use it is the only one who can. Leave that chat open.

For automatic delivery in the **Codex CLI**, share from a chat running on the local Codex
app-server daemon. Pigeon finds the chat on that daemon when you share it. Keep the chat open.
If your CLI uses a separate server, reopen the chat with:

```sh
pigeon wake <THREAD_ID>
```

`<THREAD_ID>` is the UUID of that Codex thread. Ask the reopened chat to share.
Keep the CLI chat running so Pigeon can prompt it when a question arrives.
If Codex opens read-only, exit the same chat in the other app or terminal and
press `r` to retry. If Codex asks for a working directory, choose the session
directory to continue in the original worktree.

For **Claude Code**, open `claude` normally and share the chat. Pigeon can wake it
through Claude's local inbox on macOS and Linux when cross-session messaging is
available (Claude Code 2.1.224 or later). Claude's inbound message settings still
apply. [Claude Code inbox docs](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket)

For **Cursor CLI**, open the answering chat from its project directory:

```sh
pigeon wake cursor
```

Add a saved session ID after the client name to resume a conversation. Close the
previous session before resuming it. Cursor runs in Pigeon's terminal through ACP.
Share the chat after it opens and keep it running.

`pigeon wake claude` also supports Claude's development Channels preview. It
opens the normal Claude terminal and asks you to enable the channel for that session.

## 4. Ask

In the reviewer's chat, ask the agent to call `pigeon_connect` with the key, then `pigeon_ask` with the address it returns. That address looks like `@<username>/pr-…`.

Pigeon prompts the answering chat automatically when wake is enabled.
In other chats, say “Check Pigeon and reply.”

Use `pigeon_reply` to ask a follow-up in the same conversation.
