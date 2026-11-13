---
name: pigeon
description: Share this chat, join a Pigeon session with a key, and ask or answer in that session.
---

Use the hosted `pigeon-relay` MCP server when both Pigeon servers are available.
If Pigeon tools are unavailable, tell the user to run `pigeon setup` with their client
(`codex`, `claude`, or `cursor`) and reopen the chat.

One key connects two chats for one ongoing session. Either chat can ask or answer.
An invitation expires after one hour if nobody joins. Joining keeps the session.
Do not join a key from the chat that created it. If Pigeon cannot identify this
chat, stop and tell the user to reopen it with `pigeon wake`, or to share from
a Codex or Claude chat. Do not use the account instead.

- **Share this chat.** Call `pigeon_share`. Give the user the key privately. Tell the user
  when automatic wake is unavailable.
- **Start a new session.** Call `pigeon_share` again. Each call creates another session.
- **End one session.** Call `pigeon_close` with that session id. Other sessions stay open.
  Past messages in the closed session stay readable.
- **Join with this key.** Call `pigeon_connect` with the key.
- **Show my sessions.** Call `pigeon_status` with no conversation id. It lists the peer,
  the state, and the session id.
- **Ask them something.** Call `pigeon_ask`. When this chat has one session, the message
  is enough. When it has several, ask which peer to use and pass that session id.
  Never send to a guessed session.
- **Ask a follow-up.** Call `pigeon_reply` after the previous request is answered.
  Either member can do this.
- **Check Pigeon.** Call `pigeon_receive` once with `wait=0`.
- **Answer their question.** Call `pigeon_answer` with the request's message id.
  Share only context the user authorized. An answer does not start a new question.

When a Pigeon wake notification arrives, check the inbox and answer that request.
A wake means a question is waiting. It does not mean the question was answered.
If the inbox is empty, say so and stop.
