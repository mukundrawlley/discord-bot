import discord
from discord.ext import commands
from discord import app_commands
import logging
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.connection import get_db_session
from bot.models.guild import GuildSettings
from bot.services.database_service import DatabaseService
from bot.services.xp_service import XPService
from bot.services.path_service import PathService
from bot.utils.curves import get_curve
from bot.utils.formatters import format_level_up_message

logger = logging.getLogger("Journey.XPCog")

class XP(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------------------
    # Discord Event Listeners
    # -------------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Processes incoming messages to award leveling XP if all anti-spam criteria pass."""
        # Pre-flight check: must be in a guild
        if not message.guild:
            return
            
        guild_id = message.guild.id
        user_id = message.author.id
        
        async with get_db_session() as session:
            # 1. Fetch guild configuration settings
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            # 2. Check if message qualifies for XP
            eligible, reason = XPService.should_give_xp(message, settings)
            if not eligible:
                # Silently skip if on cooldown or duplicate, log other reasons if debug
                if reason not in ("cooldown", "duplicate", "bot_or_webhook", "disabled"):
                    logger.debug(f"Skipped XP for {message.author}: {reason}")
                return
                
            # 3. Calculate XP to award
            xp_gain = XPService.calculate_xp_gain(message, settings)
            if xp_gain <= 0:
                return
                
            # 4. Award XP and check level-up status
            old_lvl, new_lvl, leveled_up = await XPService.add_xp(
                session=session,
                guild_settings=settings,
                user_id=user_id,
                amount=xp_gain
            )
            
            # 5. Apply message cooldown
            XPService.set_cooldown(guild_id, user_id, settings.xp_cooldown)
            
            if leveled_up:
                logger.info(f"User {message.author} leveled up: Level {old_lvl} -> {new_lvl} in guild {guild_id}.")
                await self._handle_level_up(message, session, settings, old_lvl, new_lvl)

    async def _handle_level_up(
        self,
        message: discord.Message,
        session: AsyncSession,
        settings: GuildSettings,
        old_level: int,
        new_level: int
    ) -> None:
        """Handles role rewards allocation, level-up, and rank-up message announcements."""
        member = message.author
        guild = message.guild
        
        # 1. Fetch User stats and path info
        stats = await DatabaseService.get_or_create_stats(session, guild.id, member.id)
        path_name = "None"
        rank_name = "None"
        highest_rank = None
        old_highest_rank = None
        
        if stats.master_path:
            path_name = stats.master_path.name
            
            # Fetch highest earned rank name at new level
            from bot.models.rank import PathRank
            ranks_res = await session.execute(
                select(PathRank)
                .filter_by(path_id=stats.master_path_id)
                .filter(PathRank.required_level <= new_level)
                .order_by(PathRank.required_level.desc())
            )
            highest_rank = ranks_res.scalars().first()
            if highest_rank:
                rank_name = highest_rank.display_name
                
            # Fetch highest earned rank name at old level
            old_ranks_res = await session.execute(
                select(PathRank)
                .filter_by(path_id=stats.master_path_id)
                .filter(PathRank.required_level <= old_level)
                .order_by(PathRank.required_level.desc())
            )
            old_highest_rank = old_ranks_res.scalars().first()
                
        # 2. Level Up Message announcement
        if settings.level_msg_enabled:
            # Build target channel
            channel = guild.get_channel(settings.level_msg_channel_id) if settings.level_msg_channel_id else message.channel
            
            if channel and isinstance(channel, discord.TextChannel):
                formatted_text = format_level_up_message(
                    template=settings.level_msg_template,
                    member=member,
                    level=new_level,
                    xp=stats.xp,
                    path_name=path_name,
                    rank_name=rank_name
                )
                
                # Mentions checking
                content_payload = ""
                if settings.level_msg_mention_user:
                    content_payload += f"{member.mention} "
                if settings.level_msg_mention_role_id:
                    role_mention = guild.get_role(settings.level_msg_mention_role_id)
                    if role_mention:
                        content_payload += f"{role_mention.mention} "
                
                if settings.level_msg_embed:
                    embed = discord.Embed(
                        description=formatted_text,
                        color=discord.Color.green()
                    )
                    if settings.level_msg_image_url:
                        embed.set_image(url=settings.level_msg_image_url)
                    await channel.send(content=content_payload.strip(), embed=embed)
                else:
                    msg_text = f"{content_payload}{formatted_text}".strip()
                    if settings.level_msg_image_url:
                        msg_text += f"\n{settings.level_msg_image_url}"
                    await channel.send(content=msg_text)

        # 2.5. Rank Up Message announcement
        if settings.rank_msg_enabled and highest_rank and (not old_highest_rank or highest_rank.id != old_highest_rank.id):
            channel = guild.get_channel(settings.rank_msg_channel_id) if settings.rank_msg_channel_id else message.channel
            if channel and isinstance(channel, discord.TextChannel):
                formatted_rank_text = format_level_up_message(
                    template=settings.rank_msg_template,
                    member=member,
                    level=new_level,
                    xp=stats.xp,
                    path_name=path_name,
                    rank_name=rank_name
                )
                
                # Mentions checking
                content_payload = ""
                if settings.rank_msg_mention_user:
                    content_payload += f"{member.mention} "
                if settings.rank_msg_mention_role_id:
                    role_mention = guild.get_role(settings.rank_msg_mention_role_id)
                    if role_mention:
                        content_payload += f"{role_mention.mention} "
                
                if settings.rank_msg_embed:
                    embed = discord.Embed(
                        description=formatted_rank_text,
                        color=discord.Color.gold()
                    )
                    if settings.rank_msg_image_url:
                        embed.set_image(url=settings.rank_msg_image_url)
                    await channel.send(content=content_payload.strip(), embed=embed)
                else:
                    msg_text = f"{content_payload}{formatted_rank_text}".strip()
                    if settings.rank_msg_image_url:
                        msg_text += f"\n{settings.rank_msg_image_url}"
                    await channel.send(content=msg_text)

        # 3. Role Rewards Adjustments
        if stats.master_path_id:
            roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                session, stats, new_level, settings
            )
            
            # Map role IDs to Discord role objects
            add_objs = [guild.get_role(rid) for rid in roles_to_add if guild.get_role(rid)]
            remove_objs = [guild.get_role(rid) for rid in roles_to_remove if guild.get_role(rid)]
            
            # Remove old roles
            remove_objs = [r for r in remove_objs if r in member.roles]
            if remove_objs:
                try:
                    await member.remove_roles(*remove_objs, reason="Journey Rank Level Up - Role Replacement")
                except discord.Forbidden:
                    logger.warning(f"Could not remove roles {remove_objs} from {member} (Forbidden).")
                    
            # Add new roles
            add_objs = [r for r in add_objs if r not in member.roles]
            if add_objs:
                try:
                    await member.add_roles(*add_objs, reason="Journey Rank Level Up - Role Award")
                except discord.Forbidden:
                    logger.warning(f"Could not add roles {add_objs} to {member} (Forbidden).")

    # -------------------------------------------------------------------------
    # Slash Groups and Commands
    # -------------------------------------------------------------------------
    xp_group = app_commands.Group(name="xp", description="XP management commands.")
    level_group = app_commands.Group(name="level", description="Level management commands.")

    @xp_group.command(name="view", description="Quickly checks user XP progress statistics.")
    @app_commands.describe(member="The member to view statistics for.")
    async def xp_view_command(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        """Displays simple string containing member XP progression details."""
        target_member = member or interaction.user
        if target_member.bot:
            await interaction.response.send_message("Bots do not have leveling stats.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            stats = await DatabaseService.get_or_create_stats(session, guild_id, target_member.id)
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            
            curve = get_curve(guild.settings.xp_curve)
            curr_lvl_req = curve.cumulative_xp_for_level(stats.level, XPService.BASE_XP, float(guild.settings.xp_multiplier))
            next_lvl_req = curve.cumulative_xp_for_level(stats.level + 1, XPService.BASE_XP, float(guild.settings.xp_multiplier))
            
            progress_xp = stats.xp - curr_lvl_req
            needed_xp = next_lvl_req - curr_lvl_req
            
            await interaction.response.send_message(
                f"📊 **{target_member.display_name}** is **Level {stats.level}** "
                f"({progress_xp:,} / {needed_xp:,} XP) - *Total XP: {stats.xp:,}*"
            )


    # -------------------------------------------------------------------------
    # Admin XP Commands
    # -------------------------------------------------------------------------
    @xp_group.command(name="settings", description="[Admin Only] Configures the server's leveling system settings.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        enabled="Enable or disable the XP system.",
        min_xp="Minimum random XP per message.",
        max_xp="Maximum random XP per message.",
        cooldown="Message XP cooldown in seconds.",
        mode="XP awarding mode: 'random' or 'per_word'.",
        xp_per_word="XP given per word if mode is 'per_word'.",
        curve="Calculations formula curve: 'linear', 'quadratic', or 'exponential'.",
        multiplier="XP curve multiplier. Higher values slow down level ups.",
        max_level="Hard cap on levels.",
        rank_mode="Rank role rewards distribution: 'stack' or 'replace'.",
        keep_path_role="Keep or remove base Master Path role when user gets rank roles.",
        msg_enabled="Enable level-up message alerts.",
        msg_template="Custom level-up message template text.",
        msg_channel="Channel for announcements.",
        msg_embed="Announce in an Embed layout.",
        msg_image="URL link to an image/GIF attachment.",
        msg_mention_user="Ping user on level up announcement.",
        msg_mention_role="Role to ping on level up announcements.",
        rank_msg_enabled="Enable rank-up message alerts.",
        rank_msg_template="Custom rank-up message template text.",
        rank_msg_channel="Channel for rank-up announcements.",
        rank_msg_embed="Announce rank-ups in an Embed layout.",
        rank_msg_image="URL link to an image/GIF for rank-up.",
        rank_msg_mention_user="Ping user on rank-up announcement.",
        rank_msg_mention_role="Role to ping on rank-up announcements."
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Random XP", value="random"),
            app_commands.Choice(name="XP Per Word", value="per_word")
        ],
        curve=[
            app_commands.Choice(name="Linear", value="linear"),
            app_commands.Choice(name="Quadratic", value="quadratic"),
            app_commands.Choice(name="Exponential", value="exponential")
        ],
        rank_mode=[
            app_commands.Choice(name="Stack Rewards", value="stack"),
            app_commands.Choice(name="Replace Rewards", value="replace")
        ]
    )
    async def xp_settings_command(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        min_xp: int | None = None,
        max_xp: int | None = None,
        cooldown: int | None = None,
        mode: app_commands.Choice[str] | None = None,
        xp_per_word: float | None = None,
        curve: app_commands.Choice[str] | None = None,
        multiplier: float | None = None,
        max_level: int | None = None,
        rank_mode: app_commands.Choice[str] | None = None,
        keep_path_role: bool | None = None,
        msg_enabled: bool | None = None,
        msg_template: str | None = None,
        msg_channel: discord.TextChannel | None = None,
        msg_embed: bool | None = None,
        msg_image: str | None = None,
        msg_mention_user: bool | None = None,
        msg_mention_role: discord.Role | None = None,
        rank_msg_enabled: bool | None = None,
        rank_msg_template: str | None = None,
        rank_msg_channel: discord.TextChannel | None = None,
        rank_msg_embed: bool | None = None,
        rank_msg_image: str | None = None,
        rank_msg_mention_user: bool | None = None,
        rank_msg_mention_role: discord.Role | None = None
    ) -> None:
        """Updates server-specific settings parameters for XP leveling."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings

            if enabled is not None:
                settings.xp_enabled = enabled
            if min_xp is not None:
                settings.xp_min = max(0, min_xp)
            if max_xp is not None:
                settings.xp_max = max(min_xp or settings.xp_min, max_xp)
            if cooldown is not None:
                settings.xp_cooldown = max(0, cooldown)
            if mode is not None:
                settings.xp_mode = mode.value
            if xp_per_word is not None:
                settings.xp_per_word_val = max(0.0, xp_per_word)
            if curve is not None:
                settings.xp_curve = curve.value
            if multiplier is not None:
                settings.xp_multiplier = max(0.1, multiplier)
            if max_level is not None:
                settings.xp_max_level = max(1, max_level)
            if rank_mode is not None:
                settings.rank_role_mode = rank_mode.value
            if keep_path_role is not None:
                settings.keep_master_path_role = keep_path_role
            if msg_enabled is not None:
                settings.level_msg_enabled = msg_enabled
            if msg_template is not None:
                settings.level_msg_template = msg_template
            if msg_channel is not None:
                settings.level_msg_channel_id = msg_channel.id
            if msg_embed is not None:
                settings.level_msg_embed = msg_embed
            if msg_image is not None:
                settings.level_msg_image_url = msg_image
            if msg_mention_user is not None:
                settings.level_msg_mention_user = msg_mention_user
            if msg_mention_role is not None:
                settings.level_msg_mention_role_id = msg_mention_role.id

            if rank_msg_enabled is not None:
                settings.rank_msg_enabled = rank_msg_enabled
            if rank_msg_template is not None:
                settings.rank_msg_template = rank_msg_template
            if rank_msg_channel is not None:
                settings.rank_msg_channel_id = rank_msg_channel.id
            if rank_msg_embed is not None:
                settings.rank_msg_embed = rank_msg_embed
            if rank_msg_image is not None:
                settings.rank_msg_image_url = rank_msg_image
            if rank_msg_mention_user is not None:
                settings.rank_msg_mention_user = rank_msg_mention_user
            if rank_msg_mention_role is not None:
                settings.rank_msg_mention_role_id = rank_msg_mention_role.id

            # Sync roles retroactively if rank role mode or base path role keeping policy is updated
            if rank_mode is not None or keep_path_role is not None:
                # Fetch all user stats with master_path_id
                users_res = await session.execute(
                    select(UserGuildStats)
                    .filter_by(guild_id=guild_id)
                    .filter(UserGuildStats.master_path_id.isnot(None))
                )
                all_users = list(users_res.scalars())
                
                for stats in all_users:
                    roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                        session, stats, stats.level, settings
                    )
                    member_obj = interaction.guild.get_member(stats.user_id)
                    if member_obj:
                        add_objs = [interaction.guild.get_role(rid) for rid in roles_to_add if interaction.guild.get_role(rid)]
                        remove_objs = [interaction.guild.get_role(rid) for rid in roles_to_remove if interaction.guild.get_role(rid)]
                        
                        remove_objs = [r for r in remove_objs if r in member_obj.roles]
                        if remove_objs:
                            try:
                                await member_obj.remove_roles(*remove_objs, reason="Journey Rank Settings Update")
                            except discord.Forbidden:
                                pass
                                
                        add_objs = [r for r in add_objs if r not in member_obj.roles]
                        if add_objs:
                            try:
                                await member_obj.add_roles(*add_objs, reason="Journey Rank Settings Update")
                            except discord.Forbidden:
                                pass

            await session.flush()
            
        await interaction.followup.send("✅ Successfully updated server XP & Leveling settings.", ephemeral=True)

    @xp_group.command(name="add", description="[Admin Only] Adds XP to a member.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member to receive XP.", amount="Amount of XP to add.")
    async def xp_add_command(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        """Forces XP increments onto a user in the database."""
        if member.bot:
            await interaction.response.send_message("Bots do not have leveling stats.", ephemeral=True)
            return
            
        if amount <= 0:
            await interaction.response.send_message("Amount must be a positive integer.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            old_lvl, new_lvl, leveled_up = await XPService.add_xp(
                session=session,
                guild_settings=settings,
                user_id=member.id,
                amount=amount
            )
            
            if leveled_up:
                await self._handle_level_up(interaction.message or message_mock_from_interaction(interaction, member), session, settings, old_lvl, new_lvl)

        await interaction.followup.send(
            f"✅ Added {amount:,} XP to {member.mention}. Level: {old_lvl} ➡️ {new_lvl}.",
            ephemeral=True
        )

    @xp_group.command(name="remove", description="[Admin Only] Removes XP from a member.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member to lose XP.", amount="Amount of XP to remove.")
    async def xp_remove_command(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        """Forces XP decrements onto a user in the database."""
        if member.bot:
            await interaction.response.send_message("Bots do not have leveling stats.", ephemeral=True)
            return
            
        if amount <= 0:
            await interaction.response.send_message("Amount must be a positive integer.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            old_lvl, new_lvl, _ = await XPService.add_xp(
                session=session,
                guild_settings=settings,
                user_id=member.id,
                amount=-amount
            )
            
            # Recalculate role rewards for the new level
            stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            if stats.master_path_id:
                roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                    session, stats, new_lvl, settings
                )
                
                # Apply changes to member
                add_objs = [interaction.guild.get_role(rid) for rid in roles_to_add if interaction.guild.get_role(rid)]
                remove_objs = [interaction.guild.get_role(rid) for rid in roles_to_remove if interaction.guild.get_role(rid)]
                
                # Remove unearned roles
                remove_objs = [r for r in remove_objs if r in member.roles]
                if remove_objs:
                    await member.remove_roles(*remove_objs, reason="Journey XP Deducted")
                # Add correct stack roles
                add_objs = [r for r in add_objs if r not in member.roles]
                if add_objs:
                    await member.add_roles(*add_objs, reason="Journey XP Deducted")

        await interaction.followup.send(
            f"✅ Removed {amount:,} XP from {member.mention}. Level: {old_lvl} ➡️ {new_lvl}.",
            ephemeral=True
        )

    @xp_group.command(name="set", description="[Admin Only] Sets a member's XP directly.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member.", amount="The exact XP amount.")
    async def xp_set_command(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        """Sets a user's XP directly in the database."""
        if member.bot:
            await interaction.response.send_message("Bots do not have leveling stats.", ephemeral=True)
            return
            
        if amount < 0:
            await interaction.response.send_message("Amount must be a non-negative integer.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            
            diff = amount - stats.xp
            old_lvl, new_lvl, leveled_up = await XPService.add_xp(
                session=session,
                guild_settings=settings,
                user_id=member.id,
                amount=diff
            )
            
            if diff < 0 or leveled_up:
                # Synchronize roles
                if stats.master_path_id:
                    roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                        session, stats, new_lvl, settings
                    )
                    add_objs = [interaction.guild.get_role(rid) for rid in roles_to_add if interaction.guild.get_role(rid)]
                    remove_objs = [interaction.guild.get_role(rid) for rid in roles_to_remove if interaction.guild.get_role(rid)]
                    
                    remove_objs = [r for r in remove_objs if r in member.roles]
                    if remove_objs:
                        await member.remove_roles(*remove_objs, reason="Journey XP Set")
                    add_objs = [r for r in add_objs if r not in member.roles]
                    if add_objs:
                        await member.add_roles(*add_objs, reason="Journey XP Set")

        await interaction.followup.send(
            f"✅ Set {member.mention}'s XP to {amount:,}. Level: {old_lvl} ➡️ {new_lvl}.",
            ephemeral=True
        )

    @xp_group.command(name="reset", description="[Admin Only] Resets a member's XP back to 0.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member.")
    async def xp_reset_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Resets a user's XP back to 0."""
        await self.xp_set_command(interaction, member, 0)

    # -------------------------------------------------------------------------
    # Admin Level Commands
    # -------------------------------------------------------------------------
    @level_group.command(name="set", description="[Admin Only] Sets a member's level directly.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member.", level="The level to assign.")
    async def level_set_command(self, interaction: discord.Interaction, member: discord.Member, level: int) -> None:
        """Sets a user's level directly by adjusting their XP to the level's baseline required cumulative."""
        if member.bot:
            await interaction.response.send_message("Bots do not have leveling stats.", ephemeral=True)
            return
            
        if level < 1:
            await interaction.response.send_message("Level must be 1 or higher.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            # Calculate required XP for the target level
            curve = get_curve(settings.xp_curve)
            target_xp = curve.cumulative_xp_for_level(level, XPService.BASE_XP, float(settings.xp_multiplier))
            
            # Adjust user statistics
            stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            diff = target_xp - stats.xp
            
            old_lvl, new_lvl, _ = await XPService.add_xp(
                session=session,
                guild_settings=settings,
                user_id=member.id,
                amount=diff
            )
            
            # Force level variable to match exactly in database (e.g. overrides caps in curves if level was explicitly forced)
            stats.level = level
            await session.flush()
            
            # Synchronize roles
            if stats.master_path_id:
                roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                    session, stats, level, settings
                )
                add_objs = [interaction.guild.get_role(rid) for rid in roles_to_add if interaction.guild.get_role(rid)]
                remove_objs = [interaction.guild.get_role(rid) for rid in roles_to_remove if interaction.guild.get_role(rid)]
                
                remove_objs = [r for r in remove_objs if r in member.roles]
                if remove_objs:
                    await member.remove_roles(*remove_objs, reason="Journey Level Set")
                add_objs = [r for r in add_objs if r not in member.roles]
                if add_objs:
                    await member.add_roles(*add_objs, reason="Journey Level Set")

        await interaction.followup.send(
            f"✅ Forced {member.mention}'s Level to **Level {level}** (XP set to {target_xp:,}).",
            ephemeral=True
        )

    @level_group.command(name="reset", description="[Admin Only] Resets a member's level to 1.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member.")
    async def level_reset_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Resets a user's level back to 1."""
        await self.level_set_command(interaction, member, 1)

    # -------------------------------------------------------------------------
    # Recalculate XP Admin Command
    # -------------------------------------------------------------------------
    @app_commands.command(name="recalculatexp", description="[Admin Only] Recalculates levels for all members based on current curve settings.")
    @app_commands.default_permissions(manage_guild=True)
    async def recalculatexp_command(self, interaction: discord.Interaction) -> None:
        """Forces full guild level evaluations from stored user cumulative XP values."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            curve = get_curve(settings.xp_curve)
            
            # Fetch all user stats for this guild
            stats_result = await session.execute(
                select(UserGuildStats).filter_by(guild_id=guild_id)
            )
            all_stats = stats_result.scalars().all()
            
            updated_count = 0
            for stats in all_stats:
                old_lvl = stats.level
                
                # Re-evaluate level from their total XP
                new_lvl = curve.level_for_xp(
                    xp=stats.xp,
                    base_xp=XPService.BASE_XP,
                    multiplier=float(settings.xp_multiplier),
                    max_level=settings.xp_max_level
                )
                
                if old_lvl != new_lvl:
                    stats.level = new_lvl
                    updated_count += 1
                    
                    # Sync roles if they belong to a path
                    if stats.master_path_id:
                        roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                            session, stats, new_lvl, settings
                        )
                        member = interaction.guild.get_member(stats.user_id)
                        if member:
                            add_objs = [interaction.guild.get_role(rid) for rid in roles_to_add if interaction.guild.get_role(rid)]
                            remove_objs = [interaction.guild.get_role(rid) for rid in roles_to_remove if interaction.guild.get_role(rid)]
                            
                            remove_objs = [r for r in remove_objs if r in member.roles]
                            if remove_objs:
                                try:
                                    await member.remove_roles(*remove_objs, reason="Journey XP Recalculation")
                                except discord.Forbidden:
                                    pass
                            add_objs = [r for r in add_objs if r not in member.roles]
                            if add_objs:
                                try:
                                    await member.add_roles(*add_objs, reason="Journey XP Recalculation")
                                except discord.Forbidden:
                                    pass
            await session.flush()
            
        await interaction.followup.send(
            f"✅ Recalculation complete. Evaluated levels for {len(all_stats)} users. Updated {updated_count} user levels.",
            ephemeral=True
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Cog-level error handler for slash commands."""
        logger.error(f"Error in XP command: {error}", exc_info=error)
        try:
            if isinstance(error, app_commands.AppCommandError):
                if isinstance(error, app_commands.CommandInvokeError):
                    error_msg = f"Database/Internal Error: {error.original}"
                else:
                    error_msg = str(error)
                    
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ An error occurred: `{error_msg}`", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ An error occurred: `{error_msg}`", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to send command error message: {e}")

# Helper mock for admin forced updates if interaction.message is unavailable
class FakeMessage:
    def __init__(self, guild, author, channel):
        self.guild = guild
        self.author = author
        self.channel = channel
        self.stickers = []
        self.attachments = []
        self.content = ""

def message_mock_from_interaction(interaction: discord.Interaction, author: discord.Member) -> discord.Message:
    return FakeMessage(interaction.guild, author, interaction.channel)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(XP(bot))
