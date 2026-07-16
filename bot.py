"""
Discord Mention Counter Bot
----------------------------
Tracks how many times each user is @-mentioned in a specific channel,
and provides a /mentions command to list the counts.

Counts are persisted to a SQLite file, so they survive restarts. The
in-memory Counter is a read cache; every write goes to the database first.
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
    conn.commit()
    return conn


def load_counts(conn: sqlite3.Connection) -> Counter[int]:
    rows = conn.execute("SELECT user_id, count FROM mention_counts").fetchall()
    return Counter(dict(rows))


def record_mentions(conn: sqlite3.Connection, user_ids: list[int]) -> None:
    """Increment each user's count by one, batched into a single transaction."""
    conn.executemany(
        """
        INSERT INTO mention_counts (user_id, count) VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET count = count + 1
        """,
        [(user_id,) for user_id in user_ids],
    )
    conn.commit()
    mention_counts.update(user_ids)


def reset_counts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM mention_counts")
    conn.commit()
    mention_counts.clear()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print(f"Loaded counts for {len(mention_counts)} user(s) from {DB_PATH}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


@bot.event
async def on_message(message: discord.Message):
    # Ignore bots (including ourselves) to avoid inflating counts
    if message.author.bot:
        await bot.process_commands(message)
        return

    # Only track mentions in the configured channel
    if message.channel.id == TARGET_CHANNEL_ID:
        mentioned = [
            user.id
            for user in message.mentions
            # Drop this condition if you want self-mentions counted too.
            if user.id != message.author.id
        ]
        if mentioned:
            record_mentions(db, mentioned)

    await bot.process_commands(message)


def build_leaderboard_text(guild: discord.Guild) -> str:
    if not mention_counts:
        return "No mentions have been tracked yet in that channel."

    lines = ["**Mention Leaderboard**"]
    for rank, (user_id, count) in enumerate(mention_counts.most_common(), start=1):
        member = guild.get_member(user_id)
        name = member.display_name if member else f"<@{user_id}> (left server)"
        lines.append(f"{rank}. {name} — {count} mention{'s' if count != 1 else ''}")

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
