import discord
from discord.ext import commands
from discord import app_commands
import logging
from datetime import datetime, timezone

from bot.database.connection import get_db_session
from bot.services.leaderboard_service import LeaderboardService

logger = logging.getLogger("Journey.LeaderboardCog")

def format_leaderboard_description(top_users: list, start_rank: int, timeframe_val: str, interaction: discord.Interaction) -> str:
    description_lines = []
    if not top_users:
        description_lines.append("*No members have earned XP in this timeframe yet.*")
    else:
        for idx, stats in enumerate(top_users):
            rank = start_rank + idx
            member = interaction.guild.get_member(stats.user_id)
            name = member.display_name if member else f"User ID: {stats.user_id}"
            
            if timeframe_val == "daily":
                score = stats.xp_daily
            elif timeframe_val == "weekly":
                score = stats.xp_weekly
            elif timeframe_val == "monthly":
                score = stats.xp_monthly
            else:
                score = stats.xp
                
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"#{rank}"
            description_lines.append(
                f"{medal} **{name}** - Level {stats.level} ({score:,} XP)"
            )
    return "\n".join(description_lines)

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
        super().__init__(timeout=120)
        self.author_id = author_id
        self.guild_id = guild_id
        self.timeframe_val = timeframe_val
        self.title_timeframe = title_timeframe
        self.total_count = total_count
        self.limit = limit
        self.current_page = 1
        self.total_pages = max(1, (total_count + limit - 1) // limit)
        self.message = None
        
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
            try:
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
            except Exception as e:
                logger.error(f"Failed to query database for leaderboard page: {e}", exc_info=True)
                await interaction.response.send_message("❌ Unable to load leaderboard.", ephemeral=True)
                return
                
        desc_text = format_leaderboard_description(top_users, offset + 1, self.timeframe_val, interaction)
        
        embed = discord.Embed(
            title=f"📈 Journey Leaderboard - {self.title_timeframe} XP",
            description=desc_text,
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
                text=f"Your Standing: {rank_str} | {caller_name} | {caller_score:,} XP | Page {self.current_page}/{self.total_pages}"
            )
        else:
            embed.set_footer(
                text=f"Your Standing: Unranked | {caller_name} | 0 XP | Page {self.current_page}/{self.total_pages}"
            )
            
        self.update_button_states()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            await self.update_message(interaction)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="next_page")
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
            # 1. Fetch top 10 users for page 1
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

        # 4. Create description layout
        desc_text = format_leaderboard_description(top_users, 1, timeframe_val, interaction)

        # Build Embed
        title_timeframe = timeframe.name if timeframe else "All Time"
        embed = discord.Embed(
            title=f"📈 Journey Leaderboard - {title_timeframe} XP",
            description=desc_text,
            color=discord.Color.gold()
        )

        # Display Caller's rank and pagination info
        total_pages = max(1, (total_count + 9) // 10)
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
                text=f"Your Standing: {rank_str} | {caller_name} | {caller_score:,} XP | Page 1/{total_pages}"
            )
        else:
            embed.set_footer(
                text=f"Your Standing: Unranked | {caller_name} | 0 XP | Page 1/{total_pages}"
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

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboards(bot))
