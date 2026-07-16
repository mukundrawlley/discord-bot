import discord
from discord.ext import commands
from discord import app_commands
import logging

from bot.database.connection import get_db_session
from bot.services.leaderboard_service import LeaderboardService

logger = logging.getLogger("Journey.LeaderboardCog")

def format_leaderboard_table(users: list, start_rank: int, timeframe_val: str, interaction: discord.Interaction) -> str:
    if not users:
        return "*No members have earned XP in this timeframe yet.*"
        
    # Table headers
    header = f"{'Rank':<5} | {'Member Name':<19} | {'Level':<5} | {'XP':<10}\n"
    separator = f"{'-'*5}-+-{'-'*19}-+-{'-'*5}-+-{'-'*10}\n"
    
    rows = []
    for idx, stats in enumerate(users):
        rank = start_rank + idx
        member = interaction.guild.get_member(stats.user_id)
        name = member.display_name if member else f"User {stats.user_id}"
        
        # Truncate long display names to keep table aligned
        if len(name) > 19:
            name = name[:16] + "..."
            
        if timeframe_val == "daily":
            score = stats.xp_daily
        elif timeframe_val == "weekly":
            score = stats.xp_weekly
        elif timeframe_val == "monthly":
            score = stats.xp_monthly
        else:
            score = stats.xp
            
        rows.append(f"#{rank:<4} | {name:<19} | Lvl {stats.level:<2} | {score:,} XP")
        
    return "```text\n" + header + separator + "\n".join(rows) + "\n```"

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
            
        table_str = format_leaderboard_table(top_users, offset + 1, self.timeframe_val, interaction)
        
        embed = discord.Embed(
            title=f"📈 Journey Leaderboard - {self.title_timeframe} XP",
            description=table_str,
            color=discord.Color.gold()
        )
        
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
            embed.set_footer(
                text=f"Standing: {rank_str} | {caller_name} | {caller_score:,} XP | Page {self.current_page}/{self.total_pages}"
            )
        else:
            embed.set_footer(
                text=f"Standing: Unranked | {caller_name} | 0 XP | Page {self.current_page}/{self.total_pages}"
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
        table_str = format_leaderboard_table(top_users, 1, timeframe_val, interaction)

        # Build Embed
        embed = discord.Embed(
            title=f"📈 Journey Leaderboard - {title_timeframe} XP",
            description=table_str,
            color=discord.Color.gold()
        )

        total_pages = max(1, (total_count + 9) // 10)

        # Display Caller's rank
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
            embed.set_footer(
                text=f"Standing: {rank_str} | {caller_name} | {caller_score:,} XP | Page 1/{total_pages}"
            )
        else:
            embed.set_footer(
                text=f"Standing: Unranked | {caller_name} | 0 XP | Page 1/{total_pages}"
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
