# CLAUDE.md

Context for Claude Code when working in this repo. This project was scaffolded
in a chat conversation with Claude before being moved here — this file exists
so that history isn't lost.

## What this is

A Discord bot that tracks how many times each user is @-mentioned in **one
specific channel**, with a command that lists a leaderboard of mention counts.

## Tech stack & key decisions

These were chosen deliberately during initial scoping — don't change them
without checking with the user first:

- **Language/library: Python + discord.py** — user had no strong preference
  ("not sure, you pick"), so Python was chosen for simplicity and discord.py's
  maturity.
- **Storage: SQLite** (`mentions.db`, path overridable via the `DB_PATH` env
  var), stdlib `sqlite3` — no extra dependency. Counts survive restarts.
  The original scoping chose in-memory only; the user asked for persistence
  on 2026-07-16 when moving to always-on hosting, so this decision is
  superseded. The in-memory `mention_counts` Counter is retained as a read
  cache: it's loaded from the DB at startup, `/mentions` reads only from it
  (zero DB hits), and every write goes to SQLite first then updates the cache
  (`record_mentions` / `reset_counts` in `bot.py`). WAL mode is on so
  committed writes survive an unclean shutdown.
- **DB schema is single-channel**: table `mention_counts(user_id PRIMARY KEY,
  count)`. There's no `channel_id` column — deliberately matched to the
  current single-channel scope. Multi-channel support would need a column
  add + migration.
- **Counting is idempotent, via `processed_messages(message_id PRIMARY KEY)`.**
  Every counting path goes through `record_message_mentions`, which claims the
  message ID with `INSERT OR IGNORE` and does nothing if the row already
  existed. This is what makes `/mentions_backfill` safe to re-run and safe to
  run while live tracking is active — the two paths can both see a message and
  it's still counted once, whichever wins the race. **Don't add a counting
  path that bypasses this function.** Only messages contributing >=1 mention
  are stored (re-scanning a mention-free message adds nothing anyway).
- **Scope: single channel only**, configured via `TARGET_CHANNEL_ID` env var.
  User explicitly chose "one specific channel" over "all channels the bot can
  see." If asked to expand, consider a `TARGET_CHANNEL_IDS` list (comma-separated
  env var) or a per-guild config command.
- **Self-mentions are excluded** from counts (mentioning yourself doesn't
  increment your own count). This was a design choice made without explicit
  user confirmation — flagged as easily reversible if the user wants it
  changed (see `bot.py`, the `if user.id == message.author.id: continue`
  block in `on_message`).
- **Bot messages are ignored entirely** (a bot mentioning someone, or being
  mentioned, doesn't count).

## File structure

```
bot.py              # Entire bot implementation (single file, intentionally — small project)
requirements.txt    # discord.py, python-dotenv
Dockerfile          # python:3.12-slim, runs as non-root uid 1000, DB on /data volume
docker-compose.yml  # restart: unless-stopped + named volume `mentions-data` for the DB
.dockerignore
.env                # Real secrets, NOT committed (DISCORD_TOKEN, TARGET_CHANNEL_ID)
README.md           # End-user setup instructions (Discord Developer Portal steps, running the bot)
mentions.db         # SQLite store, created at runtime (gitignore this; also *.db-wal/*.db-shm)
```

Note: there is no `.env.example` template in the repo despite older docs
referencing one — the real `.env` is the only env file present.

## Commands the bot exposes

- `/mentions` — slash command; lists each user + mention count in the tracked
  channel, sorted descending, using server display names.
- `!mentions` — identical, as a prefix-command fallback in case slash command
  sync hasn't propagated yet.
- `/mentions_reset` — admin-only (`administrator` permission), clears all
  counts to zero (and clears `processed_messages`, so a later backfill can
  rebuild from scratch).
- `/mentions_backfill` — admin-only. Scans the tracked channel's full history
  via `channel.history(limit=None, oldest_first=True)` and counts everything
  not yet counted. Resets first, so a full scan is authoritative. Checks
  `read_message_history` *before* resetting — otherwise a permissions failure
  would wipe the counts with nothing to rebuild from. Defers the interaction
  (a long scan blows the 3s response window; note the 15-min followup ceiling
  on very large channels). Commits every 500 messages rather than per message,
  since one fsync per message dominates runtime on a long history.

## Required Discord setup (already documented in README.md)

The bot needs these **Privileged Gateway Intents** enabled in the Discord
Developer Portal, or `on_message`/member-name resolution will silently fail:
- Message Content Intent
- Server Members Intent

## Running locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# create .env with DISCORD_TOKEN and TARGET_CHANNEL_ID (no template committed)
python bot.py
```

## Deployment

Target environment is the user's **Proxmox** cluster: an **LXC running Docker**
(so, Docker-in-LXC — needs `nesting=1` on the container). Deploy with
`docker compose up -d --build`. The DB lives on the named volume
`mentions-data`, never in the container's writable layer — this matters both
for surviving redeploys and because SQLite on overlayfs is where the
Docker-in-LXC pitfalls actually show up.

## Known limitations / not yet built

- No multi-channel support.
- No automated test suite (the persistence layer was verified manually via a
  throwaway script + a real container round-trip; nothing is checked in).
- No rate limiting or handling for very large servers (leaderboard just
  dumps every tracked user in one message; could hit Discord's 2000-character
  message limit on a very active channel with many unique mentioned users).
- SQLite writes are synchronous `sqlite3` calls on the event loop. Each write
  is sub-millisecond and batched to one transaction per message, which is
  fine at this scale — but a very high-traffic channel would want `aiosqlite`.
- Counts are lifetime totals only; no timestamps are stored.

## Likely next steps (if the user asks to continue building)

Ranked roughly by what's come up as natural follow-ups so far:
1. Paginate `/mentions` output for servers with long leaderboards (character
   limit issue above).
2. Multi-channel or all-channel tracking mode (needs the schema change noted
   above).
3. Per-time-period stats (e.g. "mentions this week") — would require storing
   timestamps, not just counts.
