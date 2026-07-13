import discord
from discord.ext import commands
import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.future import select

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
        # 1. Initialize Database Tables
        logger.info("Initializing database schemas...")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schemas initialized.")

        # 2. Load Cogs
        logger.info("Loading extensions...")
        cogs = ["general", "xp", "paths", "profile", "leaderboards"]
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
            if settings.TEST_GUILD_ID:
                test_guild = discord.Object(id=settings.TEST_GUILD_ID)
                self.tree.copy_global_to(guild=test_guild)
                await self.tree.sync(guild=test_guild)
                logger.info(f"Synchronized slash commands instantly to test guild: {settings.TEST_GUILD_ID}")
            else:
                await self.tree.sync()
                logger.info("Synchronized slash commands globally (this may take up to an hour).")
        except discord.errors.Forbidden as e:
            logger.warning(
                f"Could not synchronize slash commands: {e}. "
                "Verify the bot is present in the server and has application.commands scope."
            )
        except Exception as e:
            logger.error(f"Error synchronizing slash commands: {e}", exc_info=True)

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
