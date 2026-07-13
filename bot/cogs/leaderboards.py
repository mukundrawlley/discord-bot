import discord
from discord.ext import commands
from discord import app_commands
import logging

from bot.database.connection import get_db_session
from bot.services.leaderboard_service import LeaderboardService

logger = logging.getLogger("Journey.LeaderboardCog")

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
            # 1. Fetch top 10 users
            top_users = await LeaderboardService.get_leaderboard(
                session=session,
                guild_id=guild_id,
                filter_type=timeframe_val,
                limit=10
            )

            # 2. Fetch current member's personal rank
            user_rank, user_stats = await LeaderboardService.get_user_rank(
                session=session,
                guild_id=guild_id,
                user_id=interaction.user.id,
                filter_type=timeframe_val
            )

            # 3. Create description layout
            description_lines = []
            if not top_users:
                description_lines.append("*No members have earned XP in this timeframe yet.*")
            else:
                for idx, stats in enumerate(top_users):
                    rank = idx + 1
                    
                    # Try to fetch member objects from cache
                    member = interaction.guild.get_member(stats.user_id)
                    name = member.display_name if member else f"User ID: {stats.user_id}"
                    
                    # Read the respective timeframe score
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

            # Build Embed
            title_timeframe = timeframe.name if timeframe else "All Time"
            embed = discord.Embed(
                title=f"📈 Journey Leaderboard - {title_timeframe} XP",
                description="\n".join(description_lines),
                color=discord.Color.gold()
            )

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
                    text=f"Your Standing: {rank_str} | {caller_name} | {caller_score:,} XP"
                )
            else:
                embed.set_footer(text=f"Your Standing: Unranked | {caller_name} | 0 XP")

            await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboards(bot))
