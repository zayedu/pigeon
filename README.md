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

## 3. Share the answering chat

In the chat that has the project open, ask the agent to call `pigeon_share` and show you the key. Send the key to the reviewer privately. It starts with `pg_`, lasts one hour, and the first reviewer to use it is the only one who can. Leave that chat open.

On Codex, close the chat and resume it before sharing, so Pigeon can notify that thread:

```sh
pigeon wake <THREAD_ID>
```

`<THREAD_ID>` is the UUID of that Codex thread. Ask the reopened chat to share.

## 4. Ask

In the reviewer's chat, ask the agent to call `pigeon_connect` with the key, then `pigeon_ask` with the address it returns. That address looks like `@<username>/pr-…`.

In the answering chat, ask the agent to call `pigeon_receive` and reply with `pigeon_answer`. After `pigeon wake`, Codex is prompted to do this when a question is waiting.

A follow-up is `pigeon_reply` in the same conversation. The answering chat calls `pigeon_receive` again.
