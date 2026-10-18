
import os
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

ALLOWED_ROLE_IDS = {
    int(role_id.strip())
    for role_id in os.getenv("ALLOWED_ROLE_IDS", "").split(",")
    if role_id.strip().isdigit()
}

MAX_MESSAGES = 20
MIN_DELAY = 3.0
MAX_DELAY = 60.0
MAX_USERS = 5

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

active_tasks = {}


def is_authorized(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False

    if interaction.guild.owner_id == interaction.user.id:
        return True

    if isinstance(interaction.user, discord.Member):
        role_ids = {role.id for role in interaction.user.roles}
        return bool(role_ids & ALLOWED_ROLE_IDS)

    return False


async def resolve_users(guild, raw_ids):
    ids = []

    for part in raw_ids.replace(" ", "").split(","):
        if not part.isdigit():
            raise ValueError(
                "Enter valid numeric Discord user IDs separated by commas."
            )

        user_id = int(part)

        if user_id not in ids:
            ids.append(user_id)

    if not 1 <= len(ids) <= MAX_USERS:
        raise ValueError("Select between 1 and 5 users.")

    members = []

    for user_id in ids:
        try:
            member = guild.get_member(user_id)

            if member is None:
                member = await guild.fetch_member(user_id)

            members.append(member)

        except discord.NotFound:
            raise ValueError(
                f"User ID {user_id} is not a member of this server."
            )
        except discord.HTTPException:
            raise ValueError(
                f"Could not retrieve user ID {user_id}. Try again."
            )

    return members


async def send_messages(
    channel, members, count, delay, message, guild_id
):
    sent = 0

    try:
        mention_text = " ".join(member.mention for member in members)

        allowed_mentions = discord.AllowedMentions(
            users=members,
            roles=False,
            everyone=False,
            replied_user=False,
        )

        for _ in range(count):
            await channel.send(
                f"{mention_text} {message}",
                allowed_mentions=allowed_mentions,
            )

            sent += 1

            if sent < count:
                await asyncio.sleep(delay)

        logging.info("Completed: %s messages sent.", sent)

    except asyncio.CancelledError:
        logging.info("Task stopped after %s messages.", sent)
        raise

    except discord.Forbidden:
        logging.error("Missing permission to send in the channel.")

    except discord.HTTPException:
        logging.exception("Discord rejected a message.")

    finally:
        if active_tasks.get(guild_id) is asyncio.current_task():
            active_tasks.pop(guild_id, None)


@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)

            logging.info(
                "Synced %s commands to server %s.",
                len(synced),
                GUILD_ID,
            )
        else:
            synced = await bot.tree.sync()
            logging.info("Synced %s global commands.", len(synced))

    except discord.HTTPException:
        logging.exception("Could not sync slash commands.")

    logging.info("Logged in as %s", bot.user)


@bot.tree.command(
    name="send",
    description="Send a limited number of messages with user mentions.",
)
@app_commands.guild_only()
@app_commands.describe(
    channel="Text channel to send messages to",
    users="Comma-separated Discord member IDs (maximum 5)",
    count="Number of messages (1 to 20)",
    delay="Seconds between messages (3 to 60)",
    message="Your custom message (1 to 500 characters)",
)
async def send(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    users: str,
    count: app_commands.Range[int, 1, 20],
    delay: app_commands.Range[float, 3.0, 60.0],
    message: str,
):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "You do not have an authorized staff role.",
            ephemeral=True,
        )
        return

    if not message.strip() or len(message) > 500:
        await interaction.response.send_message(
            "Message must contain 1 to 500 characters.",
            ephemeral=True,
        )
        return

    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Use this command inside your server.",
            ephemeral=True,
        )
        return

    if guild.id in active_tasks:
        await interaction.response.send_message(
            "A sending task is already running. Use /stop first.",
            ephemeral=True,
        )
        return

    if channel.guild.id != guild.id:
        await interaction.response.send_message(
            "Choose a channel from this server.",
            ephemeral=True,
        )
        return

    permissions = channel.permissions_for(guild.me)

    if not permissions.view_channel or not permissions.send_messages:
        await interaction.response.send_message(
            "The bot needs View Channel and Send Messages permissions.",
            ephemeral=True,
        )
        return

    try:
        members = await resolve_users(guild, users)
    except ValueError as error:
        await interaction.response.send_message(
            str(error),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Starting {count} messages in {channel.mention}, "
        f"mentioning {len(members)} selected members, "
        f"with a {delay}-second delay. Use /stop to cancel.",
        ephemeral=True,
    )

    task = asyncio.create_task(
        send_messages(
            channel=channel,
            members=members,
            count=count,
            delay=delay,
            message=message.strip(),
            guild_id=guild.id,
        )
    )

    active_tasks[guild.id] = task


@bot.tree.command(
    name="stop",
    description="Stop the active message-sending task.",
)
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if not is_authorized(interaction):
        await interaction.response.send_message(
            "You do not have an authorized staff role.",
            ephemeral=True,
        )
        return

    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Use this command inside your server.",
            ephemeral=True,
        )
        return

    task = active_tasks.get(guild.id)

    if task is None or task.done():
        await interaction.response.send_message(
            "No active sending task.",
            ephemeral=True,
        )
        return

    task.cancel()

    await interaction.response.send_message(
        "Stopping the task. Messages already sent cannot be recalled.",
        ephemeral=True,
    )


if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN is missing. Check your .env file."
    )

bot.run(TOKEN)
