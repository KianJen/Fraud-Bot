"""
Discord Mention Counter Bot
----------------------------
Tracks how many times each user is @-mentioned in a specific channel,
and provides a /mentions command to list the counts.

Counts are persisted to a SQLite file, so they survive restarts. The
in-memory Counter is a read cache; every write goes to the database first.

Every counted message's ID is recorded in `processed_messages`, so counting
is idempotent: /mentions_backfill can rescan history that live tracking has
already seen without double-counting anyone.
"""

import os
import sqlite3
from collections import Counter
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
DB_PATH = Path(os.getenv("DB_PATH", "mentions.db"))

intents = discord.Intents.default()
intents.message_content = True  # required to read message content/mentions
intents.members = True  # required to resolve nicer display names

bot = commands.Bot(command_prefix="!", intents=intents)

# user_id -> mention count. Read cache over the mention_counts table.
mention_counts: Counter[int] = Counter()

db: sqlite3.Connection


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    # WAL keeps already-committed writes intact through an unclean shutdown
    # (container kill, power loss).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mention_counts (
            user_id INTEGER PRIMARY KEY,
            count   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # IDs of messages already counted. Only messages that contributed at
    # least one mention are stored — re-scanning a message with no mentions
    # adds nothing, so there's nothing to guard against.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id INTEGER PRIMARY KEY
        )
        """
    )
    conn.commit()
    return conn


def load_counts(conn: sqlite3.Connection) -> Counter[int]:
    rows = conn.execute("SELECT user_id, count FROM mention_counts").fetchall()
    return Counter(dict(rows))


def record_message_mentions(
    conn: sqlite3.Connection,
    message_id: int,
    user_ids: list[int],
    commit: bool = True,
) -> bool:
    """Count one message's mentions, exactly once.

    Claiming the message ID first makes this a no-op if the message has
    already been counted — by live tracking or an earlier backfill — so the
    live path and a running backfill can safely cover the same message.
    Returns True if this call counted the message.
    """
    claimed = conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id) VALUES (?)",
        (message_id,),
    )
    if claimed.rowcount == 0:
        return False

    conn.executemany(
        """
        INSERT INTO mention_counts (user_id, count) VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET count = count + 1
        """,
        [(user_id,) for user_id in user_ids],
    )
    if commit:
        conn.commit()
    mention_counts.update(user_ids)
    return True


def reset_counts(conn: sqlite3.Connection) -> None:
    """Wipe all counting state, including which messages have been seen."""
    conn.execute("DELETE FROM mention_counts")
    conn.execute("DELETE FROM processed_messages")
    conn.commit()
    mention_counts.clear()


def mentioned_user_ids(message: discord.Message) -> list[int]:
    """The users this message should count a mention for."""
    if message.author.bot:
        return []
    return [
        user.id
        for user in message.mentions
        # Drop this condition if you want self-mentions counted too.
        if user.id != message.author.id
    ]


async def resolve_target_channel() -> tuple[object | None, str | None]:
    """Find the tracked channel, or explain precisely why we can't.

    get_channel() only reads the local cache, and a channel the bot can't view
    is never cached — so a None result is ambiguous between "wrong ID" and "no
    permission". fetch_channel() asks the API and distinguishes the two.
    Returns (channel, error_message); exactly one is non-None.
    """
    channel = bot.get_channel(TARGET_CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
        except discord.NotFound:
            return None, (
                f"No channel with ID `{TARGET_CHANNEL_ID}` exists. That's the ID in my "
                "`TARGET_CHANNEL_ID` setting — it's probably a message, server, or "
                "category ID rather than a channel ID. Right-click the *channel* in the "
                "sidebar and pick **Copy Channel ID**."
            )
        except discord.Forbidden:
            # Discord returns 403 both for "in the server, can't see the
            # channel" and "not in that server at all" — say so, and list
            # where we actually are, since that distinguishes the two.
            if bot.guilds:
                where = ", ".join(f"{g.name} ({g.id})" for g in bot.guilds)
            else:
                where = "no servers at all — I haven't been invited anywhere"
            return None, (
                f"Channel `{TARGET_CHANNEL_ID}` exists, but I have no access to it. "
                "Either I'm not in that server, or I'm in it without **View Channel** "
                f"on that channel.\nI'm currently in: {where}."
            )
        except discord.HTTPException as exc:
            return None, f"Couldn't look up channel `{TARGET_CHANNEL_ID}`: {exc}"

    # Categories and forums have IDs but no messages of their own.
    if not isinstance(channel, discord.abc.Messageable):
        kind = type(channel).__name__
        return None, (
            f"`{TARGET_CHANNEL_ID}` is a **{kind}**, which doesn't contain messages "
            "directly. Point `TARGET_CHANNEL_ID` at a text channel."
        )

    return channel, None


async def backfill_channel(channel) -> tuple[int, int, int]:
    """Scan the channel's full history, counting any message not yet counted.

    Returns (messages_scanned, messages_counted, mentions_recorded).
    """
    scanned = counted = mentions = 0
    async for message in channel.history(limit=None, oldest_first=True):
        scanned += 1
        user_ids = mentioned_user_ids(message)
        if not user_ids:
            continue
        # Commit in batches: one fsync per message would dominate the runtime
        # on a channel with a long history.
        if record_message_mentions(db, message.id, user_ids, commit=False):
            counted += 1
            mentions += len(user_ids)
        if scanned % 500 == 0:
            db.commit()
            print(f"backfill: scanned {scanned} messages, {mentions} mentions so far")
    db.commit()
    return scanned, counted, mentions


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print(f"Loaded counts for {len(mention_counts)} user(s) from {DB_PATH}")

    if bot.guilds:
        print(f"In {len(bot.guilds)} server(s):")
        for guild in bot.guilds:
            print(f"  - {guild.name} (id: {guild.id})")
    else:
        print("WARNING: I am not in any server. Re-invite me with an OAuth2 URL.")

    # Surface a bad TARGET_CHANNEL_ID here rather than letting it look like
    # "the bot runs fine but never counts anything".
    channel, error = await resolve_target_channel()
    if error is not None:
        print(f"WARNING: tracked channel unusable — {error}")
    else:
        guild = getattr(channel, "guild", None)
        where = f"{guild.name} / #{channel}" if guild else str(channel)
        print(f"Tracking channel: {where} (id: {TARGET_CHANNEL_ID})")
        if guild is not None:
            perms = channel.permissions_for(guild.me)
            missing = [
                label
                for label, ok in (
                    ("View Channel", perms.read_messages),
                    ("Read Message History", perms.read_message_history),
                    ("Send Messages", perms.send_messages),
                )
                if not ok
            ]
            if missing:
                print(f"WARNING: missing permissions on that channel: {', '.join(missing)}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


@bot.event
async def on_message(message: discord.Message):
    # Only track mentions in the configured channel
    if message.channel.id == TARGET_CHANNEL_ID:
        user_ids = mentioned_user_ids(message)
        if user_ids:
            record_message_mentions(db, message.id, user_ids)

    await bot.process_commands(message)


def build_leaderboard_text(guild: discord.Guild) -> str:
    if not mention_counts:
        return "No mentions have been tracked yet in that channel."

    lines = ["**Biggest Frauds**"]
    for rank, (user_id, count) in enumerate(mention_counts.most_common(), start=1):
        member = guild.get_member(user_id)
        name = member.display_name if member else f"<@{user_id}> (left server)"
        lines.append(f"{rank}. {name} — {count} incident{'s' if count != 1 else ''}")

    return "\n".join(lines)


@bot.tree.command(name="mentions", description="Show how many times each user was mentioned in the tracked channel.")
async def mentions_slash(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    await interaction.response.send_message(build_leaderboard_text(interaction.guild))


@bot.command(name="mentions")
async def mentions_prefix(ctx: commands.Context):
    """Prefix command version: !mentions"""
    if ctx.guild is None:
        await ctx.send("This command only works in a server.")
        return
    await ctx.send(build_leaderboard_text(ctx.guild))


@bot.tree.command(name="mentions_reset", description="Reset the mention counts (admin only).")
@app_commands.checks.has_permissions(administrator=True)
async def mentions_reset(interaction: discord.Interaction):
    reset_counts(db)
    await interaction.response.send_message("Mention counts have been reset.")


@bot.tree.command(
    name="mentions_backfill",
    description="Scan the tracked channel's whole history and count every mention in it (admin only).",
)
@app_commands.checks.has_permissions(administrator=True)
async def mentions_backfill(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    channel, error = await resolve_target_channel()
    if error is not None:
        await interaction.response.send_message(error, ephemeral=True)
        return

    # Check access before touching the database — otherwise a permissions
    # failure would leave the counts wiped with nothing to rebuild from.
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.read_messages and perms.read_message_history):
        missing = [
            label
            for label, ok in (
                ("View Channel", perms.read_messages),
                ("Read Message History", perms.read_message_history),
            )
            if not ok
        ]
        await interaction.response.send_message(
            f"I'm missing **{'** and **'.join(missing)}** in {channel.mention}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    reset_counts(db)  # full rescan is authoritative, so rebuild from zero

    try:
        scanned, counted, mentions = await backfill_channel(channel)
    except discord.Forbidden:
        await interaction.followup.send(
            "Lost access to the channel partway through. Counts are now incomplete — "
            "fix my permissions and run this again."
        )
        return

    await interaction.followup.send(
        f"Backfill complete for {channel.mention}.\n"
        f"Scanned **{scanned}** messages, counted **{mentions}** mentions "
        f"across **{counted}** messages, covering **{len(mention_counts)}** users."
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    if not TARGET_CHANNEL_ID:
        raise SystemExit("TARGET_CHANNEL_ID is not set. Add it to your .env file.")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db(DB_PATH)
    mention_counts.update(load_counts(db))

    try:
        bot.run(TOKEN)
    finally:
        db.close()
