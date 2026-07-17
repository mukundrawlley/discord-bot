import discord
from discord.ext import commands
import logging
import asyncio
import os
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.future import select
from sqlalchemy import text

from bot.config.settings import settings
from bot.database.base import Base
from bot.database.connection import engine, get_db_session
from bot.models.guild import Guild
from bot.services.leaderboard_service import LeaderboardService

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logger = logging.getLogger("Journey.Main")

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_health_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health check web server started on port {port}")

# Define discord client intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

class JourneyBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix="j!",
            intents=intents,
            help_command=None
        )
        self.scheduler = AsyncIOScheduler()

    async def setup_hook(self) -> None:
        # Start background health server for Railway/Heroku port pings
        self.loop.create_task(start_health_server())

        # Initialize Playwright browser cache
        try:
            from bot.services.browser import BrowserManager
            await BrowserManager.initialize()
        except Exception as e:
            logger.warning(f"Failed to initialize Playwright browser manager: {e}. Leaderboard image rendering will fall back to text.")

        # 1. Initialize Database Tables
        logger.info("Initializing database schemas...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schemas initialized.")

        # Self-healing schema modifications
        logger.info("Verifying database schema integrity...")
        async with engine.begin() as conn:
            from sqlalchemy import inspect
            def check_and_add_columns(connection):
                inspector = inspect(connection)
                
                # Check guild_settings table
                guild_settings_cols = [col['name'] for col in inspector.get_columns("guild_settings")]
                new_guild_cols = [
                    ("rank_msg_enabled", "BOOLEAN DEFAULT TRUE"),
                    ("rank_msg_template", "TEXT DEFAULT 'Congratulations {user}, you achieved the rank of {rank} on the {path} path!'"),
                    ("rank_msg_channel_id", "BIGINT"),
                    ("rank_msg_embed", "BOOLEAN DEFAULT FALSE"),
                    ("rank_msg_image_url", "VARCHAR(256)"),
                    ("rank_msg_mention_user", "BOOLEAN DEFAULT TRUE"),
                    ("rank_msg_mention_role_id", "BIGINT")
                ]
                for col_name, sql_def in new_guild_cols:
                    if col_name not in guild_settings_cols:
                        logger.info(f"Adding missing column {col_name} to guild_settings...")
                        connection.execute(text(f"ALTER TABLE guild_settings ADD COLUMN {col_name} {sql_def}"))
                
                # Check user_guild_stats table
                user_stats_cols = [col['name'] for col in inspector.get_columns("user_guild_stats")]
                if "clan_id" not in user_stats_cols:
                    logger.info("Adding missing column clan_id to user_guild_stats...")
                    connection.execute(text("ALTER TABLE user_guild_stats ADD COLUMN clan_id INTEGER REFERENCES clans(id) ON DELETE SET NULL"))
                    
            await conn.run_sync(check_and_add_columns)
        logger.info("Database schema integrity check completed.")

        # 2. Load Cogs
        logger.info("Loading extensions...")
        cogs = ["general", "xp", "paths", "profile", "leaderboards", "clans"]
        for cog in cogs:
            try:
                await self.load_extension(f"bot.cogs.{cog}")
                logger.info(f"Loaded extension: {cog}")
            except Exception as e:
                logger.critical(f"Failed to load extension {cog}: {e}", exc_info=True)

        # 3. Setup Scheduler
        self._setup_scheduler_jobs()
        self.scheduler.start()
        logger.info("APScheduler initialized and running.")

        # 4. Synchronize Commands
        try:
            # Always sync globally so user-installable (personal) commands register
            await self.tree.sync()
            logger.info("Synchronized global slash commands.")

            # If test guild is configured, also sync to it for instant local development
            if settings.TEST_GUILD_ID:
                test_guild = discord.Object(id=settings.TEST_GUILD_ID)
                self.tree.copy_global_to(guild=test_guild)
                await self.tree.sync(guild=test_guild)
                logger.info(f"Synchronized slash commands instantly to test guild: {settings.TEST_GUILD_ID}")
        except discord.errors.Forbidden as e:
            logger.warning(
                f"Could not synchronize slash commands: {e}. "
                "Verify the bot is present in the server and has application.commands scope."
            )
        except Exception as e:
            logger.error(f"Error synchronizing slash commands: {e}", exc_info=True)

    async def close(self) -> None:
        logger.info("Shutdown requested. Cleaning up browser resources...")
        from bot.services.browser import BrowserManager
        await BrowserManager.close()
        await super().close()

    def _setup_scheduler_jobs(self) -> None:
        """Sets up background cron resets for daily, weekly, and monthly leaderboards."""
        self.scheduler.add_job(
            self._reset_daily_cron,
            CronTrigger(hour=0, minute=0),
            id="daily_reset_job",
            replace_existing=True
        )
        self.scheduler.add_job(
            self._reset_weekly_cron,
            CronTrigger(day_of_week="sun", hour=0, minute=0),
            id="weekly_reset_job",
            replace_existing=True
        )
        self.scheduler.add_job(
            self._reset_monthly_cron,
            CronTrigger(day=1, hour=0, minute=0),
            id="monthly_reset_job",
            replace_existing=True
        )

    # -------------------------------------------------------------------------
    # Scheduler Cron Handlers
    # -------------------------------------------------------------------------
    async def _reset_daily_cron(self) -> None:
        logger.info("Triggered: Daily statistics leaderboard reset.")
        async with get_db_session() as session:
            result = await session.execute(select(Guild))
            for guild in result.scalars():
                try:
                    await LeaderboardService.take_snapshot_and_reset(session, guild.id, "daily")
                except Exception as e:
                    logger.error(f"Error resetting daily stats for guild {guild.id}: {e}")

    async def _reset_weekly_cron(self) -> None:
        logger.info("Triggered: Weekly statistics leaderboard reset.")
        async with get_db_session() as session:
            result = await session.execute(select(Guild))
            for guild in result.scalars():
                try:
                    await LeaderboardService.take_snapshot_and_reset(session, guild.id, "weekly")
                except Exception as e:
                    logger.error(f"Error resetting weekly stats for guild {guild.id}: {e}")

    async def _reset_monthly_cron(self) -> None:
        logger.info("Triggered: Monthly statistics leaderboard reset.")
        async with get_db_session() as session:
            result = await session.execute(select(Guild))
            for guild in result.scalars():
                try:
                    await LeaderboardService.take_snapshot_and_reset(session, guild.id, "monthly")
                except Exception as e:
                    logger.error(f"Error resetting monthly stats for guild {guild.id}: {e}")

bot = JourneyBot()

@bot.command(name="sync")
async def sync_commands(ctx: commands.Context):
    """Developer command to sync slash commands."""
    is_owner = False
    try:
        is_owner = await bot.is_owner(ctx.author)
    except Exception:
        pass
        
    if not ctx.author.guild_permissions.administrator and not is_owner:
        await ctx.send("❌ You do not have permission to sync commands.")
        return
        
    await ctx.send("⏳ Syncing slash commands...")
    try:
        # First copy global to current guild for instant local updates
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"✅ Sync complete! Synced {len(synced)} commands to this server.")
        
        # Also trigger global sync in background
        global_synced = await bot.tree.sync()
        await ctx.send(f"✅ Global sync complete! Synced {len(global_synced)} commands globally.")
    except Exception as e:
        await ctx.send(f"❌ Failed to sync commands: `{e}`")
        logger.error("Command sync failed", exc_info=e)

@bot.event
async def on_ready() -> None:
    logger.info(f"------ Bot Connected to Discord ------")
    logger.info(f"Bot Username: {bot.user.name}")
    logger.info(f"Bot User ID:  {bot.user.id}")
    logger.info(f"--------------------------------------")

def main() -> None:
    if not settings.DISCORD_TOKEN or settings.DISCORD_TOKEN == "your_bot_token_here":
        logger.critical("DISCORD_TOKEN is missing. Please configure it in your .env file.")
        return
    bot.run(settings.DISCORD_TOKEN)

if __name__ == "__main__":
    main()
