# Discord Mention Counter Bot

Tracks how many times each user is @-mentioned in one specific channel, and
gives you a command to list the leaderboard. Counts are stored in a SQLite
file, so they survive restarts.

## 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Under **Bot**, click **Add Bot**, then **Reset Token** to get your bot token
   (keep this secret).
3. Under **Bot > Privileged Gateway Intents**, enable:
   - **Message Content Intent**
   - **Server Members Intent**
4. Under **OAuth2 > URL Generator**, select scopes `bot` and `applications.commands`,
   and permissions `Send Messages`, `Read Message History`, `View Channel`.
   Use the generated URL to invite the bot to your server.

## 2. Get your target channel ID

In Discord, enable Developer Mode (User Settings > Advanced), then right-click
the channel you want to track and choose **Copy Channel ID**.

## 3. Set up the project

```bash
cd discord-mention-bot
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file containing:
- `DISCORD_TOKEN` — your bot token from step 1
- `TARGET_CHANNEL_ID` — the channel ID from step 2
- `DB_PATH` — optional, where to keep the SQLite file (defaults to
  `mentions.db` next to `bot.py`)

## 4. Run it

```bash
python bot.py
```

This creates `mentions.db` on first run. Keep that file and you keep your
counts; delete it and the leaderboard starts over.

## 5. Run it 24/7 with Docker

Running `python bot.py` on your laptop means the bot goes offline whenever
the machine sleeps. To keep it always-on, run it as a container:

```bash
docker compose up -d --build
docker compose logs -f          # watch it connect
```

The counts live on a Docker named volume (`mentions-data`), *not* inside the
container, so `docker compose up -d --build` after a code change keeps your
history. `restart: unless-stopped` brings the bot back after a host reboot.

To back up the database:

```bash
docker compose cp mention-bot:/data/mentions.db ./mentions-backup.db
```

## Commands

- `/mentions` — slash command, lists each user and how many times they've
  been @-mentioned in the tracked channel, most-mentioned first.
- `!mentions` — same thing as a prefix command, in case slash commands
  haven't synced yet.
- `/mentions_reset` — admin-only, clears the counts back to zero.

## Notes / things you can tweak

- **Self-mentions**: currently ignored (mentioning yourself doesn't count).
  Remove the `if user.id == message.author.id: continue` block in `bot.py`
  if you want those counted.
- **Bots**: messages from bots are ignored entirely.
- **Persistence**: counts are written to SQLite (`mentions.db`) as they
  happen, and reloaded at startup. `/mentions_reset` clears the database as
  well as memory, so a reset is permanent — there's no undo short of a backup.
- **Multiple channels**: this only tracks one channel by ID. If you'd like
  it to track several channels, or all channels, that's a quick modification.
