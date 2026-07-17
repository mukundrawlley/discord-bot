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

        # Register global interaction check to restrict non-clan commands to servers where the bot is authorized
        async def global_interaction_check(interaction: discord.Interaction) -> bool:
            if interaction.command:
                # Bypass check for any command in the 'clan' group
                is_clan_cmd = False
                if interaction.command.name == "clan":
                    is_clan_cmd = True
                elif interaction.command.root_parent and interaction.command.root_parent.name == "clan":
                    is_clan_cmd = True
                    
                if is_clan_cmd:
                    return True
            
            # For all other commands, verify server authorization (bot is in the guild)
            if interaction.guild_id is not None:
                if interaction.client.get_guild(interaction.guild_id) is None:
                    await interaction.response.send_message(
                        "❌ This command can only be used in servers where the bot is authorized/installed.",
                        ephemeral=True
                    )
                    return False
            else:
                # Block execution in DMs/Group DMs
                await interaction.response.send_message(
                    "❌ This command requires a server context where the bot is authorized/installed.",
                    ephemeral=True
                )
                return False
                
            return True
            
        self.tree.interaction_check = global_interaction_check

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

        # Self-healing schema modifications & migrations
        logger.info("Verifying database schema integrity...")
        try:
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
                    
                    # Check clans table
                    if "clans" in inspector.get_table_names():
                        clans_cols = [col['name'] for col in inspector.get_columns("clans")]
                        if "approved" not in clans_cols:
                            logger.info("Adding missing column approved to clans...")
                            connection.execute(text("ALTER TABLE clans ADD COLUMN approved BOOLEAN DEFAULT FALSE"))
                        if "approved_by" not in clans_cols:
                            logger.info("Adding missing column approved_by to clans...")
                            connection.execute(text("ALTER TABLE clans ADD COLUMN approved_by BIGINT"))
                        if "approved_at" not in clans_cols:
                            logger.info("Adding missing column approved_at to clans...")
                            connection.execute(text("ALTER TABLE clans ADD COLUMN approved_at TIMESTAMP"))
                    
                    # Check clan_role_permissions table columns dynamically
                    if "clan_role_permissions" in inspector.get_table_names():
                        from bot.models.clan import ClanRolePermission
                        model_cols = [c.name for c in ClanRolePermission.__table__.columns if c.name != "role_id"]
                        db_cols = [col['name'] for col in inspector.get_columns("clan_role_permissions")]
                        
                        for col_name in model_cols:
                            if col_name not in db_cols:
                                logger.info(f"Adding missing permission column {col_name} to clan_role_permissions...")
                                default_val = "TRUE" if col_name == "can_deposit_coins" else "FALSE"
                                connection.execute(text(f"ALTER TABLE clan_role_permissions ADD COLUMN {col_name} BOOLEAN DEFAULT {default_val}"))
                        
                await conn.run_sync(check_and_add_columns)
                
                def migrate_old_clan_data(connection):
                    from datetime import datetime, timezone
                    inspector = inspect(connection)
                    tables = inspector.get_table_names()
                    
                    if "clans" not in tables:
                        return
                        
                    # 1. Auto-approve pre-existing clans
                    connection.execute(text("UPDATE clans SET approved = TRUE WHERE approved IS NULL OR approved = FALSE"))
                    
                    # 2. For each clan, ensure default roles exist in clan_roles
                    clans = connection.execute(text("SELECT id, owner_id FROM clans")).fetchall()
                    for clan_id, owner_id in clans:
                        # Check if roles exist
                        roles_count = connection.execute(
                            text("SELECT COUNT(*) FROM clan_roles WHERE clan_id = :clan_id"),
                            {"clan_id": clan_id}
                        ).scalar()
                        
                        if roles_count == 0:
                            logger.info(f"Migration: Creating default roles for pre-existing clan {clan_id}...")
                            now = datetime.now(timezone.utc).replace(tzinfo=None)
                            # Insert Leader role
                            connection.execute(
                                text("""
                                    INSERT INTO clan_roles 
                                    (clan_id, role_name, color, hierarchy_level, max_members, is_system_role, display_order, created_at, updated_at) 
                                    VALUES 
                                    (:clan_id, 'Leader', '#FFD700', 100, 1, TRUE, 0, :now, :now)
                                """),
                                {"clan_id": clan_id, "now": now}
                            )
                            leader_role_id = connection.execute(
                                text("SELECT id FROM clan_roles WHERE clan_id = :clan_id AND hierarchy_level = 100"),
                                {"clan_id": clan_id}
                            ).scalar()
                            
                            # Insert Member role
                            connection.execute(
                                text("""
                                    INSERT INTO clan_roles 
                                    (clan_id, role_name, color, hierarchy_level, is_system_role, display_order, created_at, updated_at) 
                                    VALUES 
                                    (:clan_id, 'Member', '#3498DB', 1, TRUE, 0, :now, :now)
                                """),
                                {"clan_id": clan_id, "now": now}
                            )
                            member_role_id = connection.execute(
                                text("SELECT id FROM clan_roles WHERE clan_id = :clan_id AND hierarchy_level = 1"),
                                {"clan_id": clan_id}
                            ).scalar()
                            
                            from bot.models.clan import get_default_permission_values
                            
                            # Insert default permissions for Leader
                            if leader_role_id:
                                exists = connection.execute(
                                    text("SELECT 1 FROM clan_role_permissions WHERE role_id = :role_id"),
                                    {"role_id": leader_role_id}
                                ).scalar()
                                if not exists:
                                    leader_perms = get_default_permission_values(is_leader=True)
                                    cols = ["role_id"] + list(leader_perms.keys())
                                    vals = [":role_id"] + [f":{k}" for k in leader_perms.keys()]
                                    stmt = f"INSERT INTO clan_role_permissions ({', '.join(cols)}) VALUES ({', '.join(vals)})"
                                    params = {"role_id": leader_role_id}
                                    params.update(leader_perms)
                                    connection.execute(text(stmt), params)
                                    
                            # Insert default permissions for Member
                            if member_role_id:
                                exists = connection.execute(
                                    text("SELECT 1 FROM clan_role_permissions WHERE role_id = :role_id"),
                                    {"role_id": member_role_id}
                                ).scalar()
                                if not exists:
                                    member_perms = get_default_permission_values(is_leader=False)
                                    cols = ["role_id"] + list(member_perms.keys())
                                    vals = [":role_id"] + [f":{k}" for k in member_perms.keys()]
                                    stmt = f"INSERT INTO clan_role_permissions ({', '.join(cols)}) VALUES ({', '.join(vals)})"
                                    params = {"role_id": member_role_id}
                                    params.update(member_perms)
                                    connection.execute(text(stmt), params)
                    
                    # 3. Check if user_guild_stats table has a clan_id column
                    if "user_guild_stats" in tables:
                        user_stats_cols = [col['name'] for col in inspector.get_columns("user_guild_stats")]
                        if "clan_id" in user_stats_cols:
                            # Fetch all stats where clan_id is set and the clan exists (prevents orphaned FK violation crash)
                            old_stats = connection.execute(text(
                                "SELECT u.guild_id, u.user_id, u.clan_id "
                                "FROM user_guild_stats u "
                                "JOIN clans c ON u.clan_id = c.id "
                                "WHERE u.clan_id IS NOT NULL"
                            )).fetchall()
                            for guild_id, user_id, clan_id in old_stats:
                                # Check if already in clan_members
                                exists = connection.execute(
                                    text("SELECT 1 FROM clan_members WHERE guild_id = :guild_id AND user_id = :user_id"),
                                    {"guild_id": guild_id, "user_id": user_id}
                                ).scalar()
                                
                                if not exists:
                                    # Find owner
                                    owner_id = connection.execute(text("SELECT owner_id FROM clans WHERE id = :clan_id"), {"clan_id": clan_id}).scalar()
                                    
                                    # Find roles
                                    if owner_id == user_id:
                                        role_id = connection.execute(
                                            text("SELECT id FROM clan_roles WHERE clan_id = :clan_id AND hierarchy_level = 100"),
                                            {"clan_id": clan_id}
                                        ).scalar()
                                    else:
                                        role_id = connection.execute(
                                            text("SELECT id FROM clan_roles WHERE clan_id = :clan_id AND hierarchy_level = 1"),
                                            {"clan_id": clan_id}
                                        ).scalar()
                                        
                                    connection.execute(
                                        text("INSERT INTO clan_members (guild_id, user_id, clan_id, role_id, join_date) VALUES (:guild_id, :user_id, :clan_id, :role_id, :join_date)"),
                                        {
                                            "guild_id": guild_id,
                                            "user_id": user_id,
                                            "clan_id": clan_id,
                                            "role_id": role_id,
                                            "join_date": datetime.now(timezone.utc).replace(tzinfo=None)
                                        }
                                    )
                                    logger.info(f"Migration: Restored member {user_id} to clan {clan_id} with role {role_id}")
                                    
                await conn.run_sync(migrate_old_clan_data)
            logger.info("Database schema integrity check completed.")
        except Exception as e:
            logger.error("❌ Database migration or integrity check failed! Startup continuing...", exc_info=e)

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

            # If test guild is configured, clear its local commands so they don't duplicate global ones
            if settings.TEST_GUILD_ID:
                test_guild = discord.Object(id=settings.TEST_GUILD_ID)
                self.tree.clear_commands(guild=test_guild)
                await self.tree.sync(guild=test_guild)
                logger.info(f"Cleared local guild commands for test guild: {settings.TEST_GUILD_ID}")
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
        # Clear any guild-local commands from this server to resolve duplicate listings
        bot.tree.clear_commands(guild=ctx.guild)
        await bot.tree.sync(guild=ctx.guild)
        await ctx.send("🧹 Cleared duplicate guild-local commands from this server.")
        
        # Synchronize commands globally
        global_synced = await bot.tree.sync()
        await ctx.send(f"✅ Global sync complete! Synced {len(global_synced)} commands globally. (Duplicates resolved!)")
    except Exception as e:
        await ctx.send(f"❌ Failed to sync commands: `{e}`")
        logger.error("Command sync failed", exc_info=e)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    # Ignore bot self updates
    if after.id == bot.user.id:
        return
        
    # Check if the role differences are relevant to clans
    role_diff = set(before.roles) ^ set(after.roles)
    if not role_diff:
        return
        
    from sqlalchemy.orm import selectinload
    async with get_db_session() as session:
        from bot.models.clan import ClanMember, ClanRole, Clan
        from sqlalchemy.future import select
        
        result = await session.execute(
            select(ClanMember)
            .options(selectinload(ClanMember.role))
            .filter_by(guild_id=after.guild.id, user_id=after.id)
        )
        membership = result.scalar_one_or_none()
        
        if membership:
            # Fetch all roles for this clan
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=membership.clan_id)
            )
            clan_roles = list(roles_result.scalars())
            correct_role_id = membership.role.discord_role_id if (membership.role and membership.role.discord_role_id) else None
            
            for role in clan_roles:
                if not role.discord_role_id:
                    continue
                discord_role = after.guild.get_role(role.discord_role_id)
                if not discord_role:
                    continue
                    
                should_have = (role.discord_role_id == correct_role_id)
                has_role = (discord_role in after.roles)
                
                if should_have and not has_role:
                    try:
                        logger.info(f"Auto-sync: Restoring missing clan role {role.role_name} to {after.display_name}")
                        await after.add_roles(discord_role, reason="Journey Auto-Sync: Restore clan role.")
                    except discord.Forbidden:
                        logger.warning(f"Auto-sync: Missing permission to add role {role.role_name} to {after.display_name}")
                elif not should_have and has_role:
                    try:
                        logger.info(f"Auto-sync: Removing incorrect clan role {role.role_name} from {after.display_name}")
                        await after.remove_roles(discord_role, reason="Journey Auto-Sync: Remove incorrect clan role.")
                    except discord.Forbidden:
                        logger.warning(f"Auto-sync: Missing permission to remove role {role.role_name} from {after.display_name}")
        else:
            # Strip any clan roles from users who are not in any clan
            clans_result = await session.execute(
                select(Clan).filter_by(guild_id=after.guild.id)
            )
            guild_clans = list(clans_result.scalars())
            if guild_clans:
                clan_ids = [c.id for c in guild_clans]
                roles_result = await session.execute(
                    select(ClanRole).filter(ClanRole.clan_id.in_(clan_ids))
                )
                all_clan_roles = list(roles_result.scalars())
                for role in all_clan_roles:
                    if not role.discord_role_id:
                        continue
                    discord_role = after.guild.get_role(role.discord_role_id)
                    if discord_role and discord_role in after.roles:
                        try:
                            logger.info(f"Auto-sync: Removing clan role {role.role_name} from non-clan member {after.display_name}")
                            await after.remove_roles(discord_role, reason="Journey Auto-Sync: Remove clan role from non-member.")
                        except discord.Forbidden:
                            pass

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
