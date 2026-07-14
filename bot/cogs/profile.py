import discord
from discord.ext import commands
from discord import app_commands
import logging
from sqlalchemy.future import select

from bot.database.connection import get_db_session
from bot.services.database_service import DatabaseService
from bot.services.path_service import PathService
from bot.utils.curves import get_curve
from bot.services.xp_service import XPService

logger = logging.getLogger("Journey.ProfileCog")

class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="profile", description="Displays yours or another member's profile card.")
    @app_commands.describe(member="The member whose profile you want to inspect.")
    async def profile_command(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        """Renders the leveling stats, Master Path selection, and ranks for a user."""
        target_member = member or interaction.user
        if target_member.bot:
            await interaction.response.send_message("Bots do not have Journey profiles.", ephemeral=True)
            return

        await interaction.response.defer()
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            # 1. Fetch guild settings and user statistics
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            stats = await DatabaseService.get_or_create_stats(session, guild_id, target_member.id)
            
            # 2. Identify active Master Path and Rank name
            path_name = "None Selected"
            rank_name = "None"
            
            if stats.master_path:
                path_name = stats.master_path.name
                
                # Fetch ranks
                from bot.models.rank import PathRank
                ranks_res = await session.execute(
                    select(PathRank)
                    .filter_by(path_id=stats.master_path_id)
                    .filter(PathRank.required_level <= stats.level)
                    .order_by(PathRank.required_level.desc())
                )
                highest_rank = ranks_res.scalars().first()
                if highest_rank:
                    rank_name = highest_rank.display_name
            
            # 3. Calculate level progress XP
            curve = get_curve(settings.xp_curve)
            
            # Cumulative XP required to reach start of current level
            curr_lvl_req = curve.cumulative_xp_for_level(
                stats.level, XPService.BASE_XP, float(settings.xp_multiplier)
            )
            # Cumulative XP required to reach next level
            next_lvl_req = curve.cumulative_xp_for_level(
                stats.level + 1, XPService.BASE_XP, float(settings.xp_multiplier)
            )
            
            # XP progress within current level
            level_xp_earned = stats.xp - curr_lvl_req
            level_xp_needed = next_lvl_req - curr_lvl_req
            
            # 4. Construct beautiful Embed
            embed_color = discord.Color.blurple()
            if stats.master_path and stats.master_path.color is not None:
                embed_color = discord.Color(stats.master_path.color)

            embed = discord.Embed(
                title=f"✨ {target_member.display_name}'s Journey Profile ✨",
                color=embed_color
            )
            embed.set_thumbnail(url=target_member.display_avatar.url)
            
            # Core Stats
            embed.add_field(name="📊 Level", value=f"**Level {stats.level}**", inline=True)
            embed.add_field(
                name="📈 XP Progress", 
                value=f"**{level_xp_earned:,} / {level_xp_needed:,} XP**\n*(Total: {stats.xp:,} XP)*", 
                inline=True
            )
            embed.add_field(name="🏷️ Active Title", value="Not Available", inline=True)
            
            # Master Path Info
            embed.add_field(name="🛤️ Master Path", value=f"**{path_name}**", inline=True)
            embed.add_field(name="🏆 Path Rank", value=f"**{rank_name}**", inline=True)
            embed.add_field(name="💍 Marriage Partner", value="Not Available", inline=True)
            
            # Future Features placeholders
            embed.add_field(name="🪙 Bias Coins", value="Not Available", inline=True)
            embed.add_field(name="🎟️ Gacha Spins", value="Not Available", inline=True)
            embed.add_field(name="📦 Characters Owned", value="Not Available", inline=True)
            
            embed.set_footer(text=f"User ID: {target_member.id}")
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="reset-profile", description="[Admin Only] Resets a member's Journey statistics.")
    @app_commands.describe(member="The member whose profile to reset.")
    @app_commands.default_permissions(manage_guild=True)
    async def reset_profile_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Resets all XP, level, and Master Path choices for a user back to default."""
        if member.bot:
            await interaction.response.send_message("Bots do not have profiles to reset.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            
            # Clean up old path roles
            old_path_id = stats.master_path_id
            roles_to_remove = []
            if old_path_id:
                old_path = await PathService.get_path_by_id(session, old_path_id)
                if old_path:
                    roles_to_remove.append(old_path.discord_role_id)
                    # Get old ranks
                    from bot.models.rank import PathRank
                    ranks_res = await session.execute(
                        select(PathRank).filter_by(path_id=old_path_id)
                    )
                    for r in ranks_res.scalars():
                        roles_to_remove.append(r.discord_role_id)
            
            # Reset DB stats
            stats.xp = 0
            stats.level = 1
            stats.master_path_id = None
            stats.xp_daily = 0
            stats.xp_weekly = 0
            stats.xp_monthly = 0
            
            await session.flush()
            
            # Strip roles from member
            removed_from_user = []
            for role_id in roles_to_remove:
                role = interaction.guild.get_role(role_id)
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Journey Profile Reset")
                        removed_from_user.append(role.name)
                    except discord.Forbidden:
                        logger.warning(f"Insufficient permissions to remove role {role.name} from {member}.")
            
            removed_roles_str = f" (Removed roles: {', '.join(removed_from_user)})" if removed_from_user else ""
            await interaction.followup.send(
                content=f"Successfully reset {member.mention}'s leveling stats and Master Path selector to default.{removed_roles_str}",
                ephemeral=True
            )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Cog-level error handler for slash commands."""
        logger.error(f"Error in Profile command: {error}", exc_info=error)
        try:
            # Extract root cause if it is a CommandInvokeError
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

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
