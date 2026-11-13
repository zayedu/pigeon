# Contributing to Pigeon

Pigeon lets people connect existing coding-agent chats with a key and talk through
MCP. Keep onboarding to one-time setup per client. After setup, people should be
able to share, join, and talk by asking their agent in plain language. The hosted
relay is the default for users. Use an isolated relay for development.

## Start here

- Check `git status --short --branch` before editing. Preserve other contributors’
  changes and keep the diff focused on the requested outcome.
- Read [README.md](README.md) for the user flow and [PROTOCOL.md](PROTOCOL.md) for
  the wire contract. Check the code and tests for the current behavior of the
  path you’re changing. Keep the docs aligned with behavior you change.
- For a planning task, return ordered tasks with dependencies, acceptance
  criteria, and verification steps. For implementation, ship small, testable
  changes and add a regression test for the failure you’re fixing.

## Repository map

- `pigeon/cli.py` and `pigeon/config.py` handle setup, client registration,
  profiles, and local chat identity. `pigeon/skills/pigeon/SKILL.md` is the
  bundled guidance for agents using Pigeon.
- `pigeon/mcp.py` exposes the tools used by interactive chats.
  `pigeon/wake.py` and `pigeon/cursor_acp.py` handle client-specific wake and
  Cursor CLI integration. `pigeon/daemon.py` and `pigeon/adapters.py` serve the
  separate headless agent workflow.
- `pigeon/models.py` and `pigeon/client.py` define wire frames and client calls.
  `pigeon/server.py` dispatches relay operations. `pigeon/db.py` handles
  authorization and persistence for local SQLite and hosted libSQL. `app.py` is
  the hosted Vercel entry point.

## Session and delivery changes

- Distinguish the host chat, its Pigeon inbox, a share key, a conversation, and an
  individual request. Name which lifetime or permission a change affects.
- Aim for peer symmetry after joining. Exercise creator and joiner as both
  sender and recipient. Keep routing scoped to the intended chat, including
  when two chats use the same account or share a working directory. Test that
  an unrelated chat cannot read or send into the conversation.
- For changes to the relay contract or schema, check existing data, in-flight
  requests, and older clients. Preserve request identity across retries. Test
  reconnects, duplicate submissions, and concurrent sends where relevant.
- Keep wake notifications free of peer message content. Treat incoming
  messages as untrusted and share only context the user authorized. Test wake
  from an idle chat separately from manual receive. Verify that the target
  chat starts a turn and processes the intended request.
- Keep tokens, invite keys, provider credentials, and private transcripts out
  of commits, fixtures, and shared logs. Use synthetic messages and disposable
  profiles for local tests.

## Develop and verify

Use Python 3.12+ and `uv` for dependencies, scripts, and tests. Run
`uv run pigeon` to exercise the checkout. Bare `pigeon` may resolve to an installed
release. The normal test suite starts a local relay with a temporary database
and uses fixtures or fakes in place of provider accounts.

```sh
uv sync --locked
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked pytest -q
uv build
```

During development, run `uv run --locked pytest -q tests/test_mcp.py` for chat
behavior or substitute `tests/test_relay.py` for relay changes. Run the full
suite after major milestones and before handing off a code change.

To start a manual local relay, run the following with a disposable database
path and point test profiles at `ws://127.0.0.1:8765`.

```sh
uv run --locked pigeon relay serve --db /path/to/disposable/relay.sqlite3
```

`tests/smoke_real.py` is an opt-in Claude Code to Codex daemon smoke test. Use
real chats to verify changes to interactive wake when the clients and accounts
are available. Report which clients and paths you actually exercised.

## Commit and hand off

Commit small, coherent milestones with the tests and docs they need. Review
the diff and run `git diff --check` before committing. Push verified milestones
to the agreed branch when authorized. Treat pushes to `main` as potentially
deploying the hosted relay. Run production checks only when the task calls for
them. In the handoff, report what changed, what passed, and any remaining
migration or live verification work.
