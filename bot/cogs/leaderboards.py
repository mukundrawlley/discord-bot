import discord
from discord.ext import commands
from discord import app_commands
import logging

from bot.database.connection import get_db_session
from bot.services.leaderboard_service import LeaderboardService

logger = logging.getLogger("Journey.LeaderboardCog")

def format_leaderboard_list(users: list, start_rank: int, timeframe_val: str, interaction: discord.Interaction) -> str:
    if not users:
        return "*No members have earned XP in this timeframe yet.*"
        
    lines = []
    for idx, stats in enumerate(users):
        rank = start_rank + idx
        member = interaction.guild.get_member(stats.user_id)
        name = member.display_name if member else f"User {stats.user_id}"
        
        # Escape markdown to prevent username styling from breaking layout
        escaped_name = discord.utils.escape_markdown(name)
            
        if timeframe_val == "daily":
            score = stats.xp_daily
        elif timeframe_val == "weekly":
            score = stats.xp_weekly
        elif timeframe_val == "monthly":
            score = stats.xp_monthly
        else:
            score = stats.xp
            
        # Medal representations for top 3
        if rank == 1:
            rank_str = "🥇"
        elif rank == 2:
            rank_str = "🥈"
        elif rank == 3:
            rank_str = "🥉"
        else:
            rank_str = f"**#{rank}**"
            
        lines.append(f"{rank_str} • **{escaped_name}** • Level {stats.level} • `{score:,} XP`")
        
    return "\n".join(lines)

class LeaderboardView(discord.ui.View):
    def __init__(
        self, 
        author_id: int, 
        guild_id: int, 
        timeframe_val: str, 
        title_timeframe: str,
        total_count: int,
        limit: int = 10
    ):
        super().__init__(timeout=120)  # Active for 2 minutes
        self.author_id = author_id
        self.guild_id = guild_id
        self.timeframe_val = timeframe_val
        self.title_timeframe = title_timeframe
        self.total_count = total_count
        self.limit = limit
        self.current_page = 1
        self.total_pages = max(1, (total_count + limit - 1) // limit)
        self.message = None
        
        # Update button states on init
        self.update_button_states()
        
    def update_button_states(self):
        self.prev_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages
        
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the command author can navigate this leaderboard.", ephemeral=True)
            return False
        return True
        
    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass
        
    async def update_message(self, interaction: discord.Interaction):
        offset = (self.current_page - 1) * self.limit
        async with get_db_session() as session:
            top_users = await LeaderboardService.get_leaderboard(
                session=session,
                guild_id=self.guild_id,
                filter_type=self.timeframe_val,
                limit=self.limit,
                offset=offset
            )
            
            user_rank, user_stats = await LeaderboardService.get_user_rank(
                session=session,
                guild_id=self.guild_id,
                user_id=self.author_id,
                filter_type=self.timeframe_val
            )
            
        list_str = format_leaderboard_list(top_users, offset + 1, self.timeframe_val, interaction)
        
        # Build description with personal standing block at the top
        caller_name = interaction.user.display_name
        if user_stats:
            if self.timeframe_val == "daily":
                caller_score = user_stats.xp_daily
            elif self.timeframe_val == "weekly":
                caller_score = user_stats.xp_weekly
            elif self.timeframe_val == "monthly":
                caller_score = user_stats.xp_monthly
            else:
                caller_score = user_stats.xp
                
            rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
            description = (
                f"👤 **Your Standing:**\n"
                f"You are ranked **{rank_str}** on this server with **{caller_score:,} XP** (Level {user_stats.level})\n\n"
                f"---\n"
                f"{list_str}"
            )
        else:
            description = (
                f"👤 **Your Standing:**\n"
                f"You are ranked **Unranked** on this server with **0 XP** (Level 0)\n\n"
                f"---\n"
                f"{list_str}"
            )

        embed = discord.Embed(
            title=f"📈 Journey Leaderboard - {self.title_timeframe} XP",
            description=description,
            color=discord.Color.gold()
        )
        
        embed.set_footer(
            text=f"Page {self.current_page}/{self.total_pages} | Total Ranked: {self.total_count}"
        )
            
        self.update_button_states()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.blurple, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            await self.update_message(interaction)
            
    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.blurple, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            await self.update_message(interaction)

class Leaderboards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Displays the leaderboard rankings.")
    @app_commands.describe(
        type="The type of leaderboard to display.",
        timeframe="The timeframe filter (applicable to XP)."
    )
    @app_commands.choices(
        type=[
            app_commands.Choice(name="XP", value="xp"),
            app_commands.Choice(name="Bias Coins (Future)", value="coins"),
            app_commands.Choice(name="Gacha Spins (Future)", value="spins"),
            app_commands.Choice(name="Characters (Future)", value="characters")
        ],
        timeframe=[
            app_commands.Choice(name="All Time", value="all_time"),
            app_commands.Choice(name="Monthly", value="monthly"),
            app_commands.Choice(name="Weekly", value="weekly"),
            app_commands.Choice(name="Daily", value="daily")
        ]
    )
    async def leaderboard_command(
        self,
        interaction: discord.Interaction,
        type: app_commands.Choice[str],
        timeframe: app_commands.Choice[str] | None = None
    ) -> None:
        """Displays user standings in the guild. Fetches statistical ranking sheets."""
        board_type = type.value
        timeframe_val = timeframe.value if timeframe else "all_time"

        # Check for future feature placeholders
        if board_type != "xp":
            await interaction.response.send_message(
                content=f"🚫 The **{type.name}** leaderboard is **Not Available** in Phase 1.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            # 1. Fetch top 10 users for initial page
            top_users = await LeaderboardService.get_leaderboard(
                session=session,
                guild_id=guild_id,
                filter_type=timeframe_val,
                limit=10,
                offset=0
            )

            # 2. Fetch current member's personal rank
            user_rank, user_stats = await LeaderboardService.get_user_rank(
                session=session,
                guild_id=guild_id,
                user_id=interaction.user.id,
                filter_type=timeframe_val
            )
            
            # 3. Fetch total ranked users count
            total_count = await LeaderboardService.get_ranked_users_count(
                session=session,
                guild_id=guild_id,
                filter_type=timeframe_val
            )

        title_timeframe = timeframe.name if timeframe else "All Time"
        list_str = format_leaderboard_list(top_users, 1, timeframe_val, interaction)

        # Build description with personal standing block at the top
        caller_name = interaction.user.display_name
        if user_stats:
            if timeframe_val == "daily":
                caller_score = user_stats.xp_daily
            elif timeframe_val == "weekly":
                caller_score = user_stats.xp_weekly
            elif timeframe_val == "monthly":
                caller_score = user_stats.xp_monthly
            else:
                caller_score = user_stats.xp
            
            rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
            description = (
                f"👤 **Your Standing:**\n"
                f"You are ranked **{rank_str}** on this server with **{caller_score:,} XP** (Level {user_stats.level})\n\n"
                f"---\n"
                f"{list_str}"
            )
        else:
            description = (
                f"👤 **Your Standing:**\n"
                f"You are ranked **Unranked** on this server with **0 XP** (Level 0)\n\n"
                f"---\n"
                f"{list_str}"
            )

        total_pages = max(1, (total_count + 9) // 10)

        # Build Embed
        embed = discord.Embed(
            title=f"📈 Journey Leaderboard - {title_timeframe} XP",
            description=description,
            color=discord.Color.gold()
        )
        
        embed.set_footer(
            text=f"Page 1/{total_pages} | Total Ranked: {total_count}"
        )

        view = LeaderboardView(
            author_id=interaction.user.id,
            guild_id=guild_id,
            timeframe_val=timeframe_val,
            title_timeframe=title_timeframe,
            total_count=total_count,
            limit=10
        )
        
        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Cog-level error handler for slash commands."""
        logger.error(f"Error in Leaderboards command: {error}", exc_info=error)
        try:
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
    await bot.add_cog(Leaderboards(bot))
