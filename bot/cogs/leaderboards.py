import discord
from discord.ext import commands
from discord import app_commands
import logging
from datetime import datetime, timezone
import io
from sqlalchemy.future import select

from bot.database.connection import get_db_session
from bot.services.leaderboard_service import LeaderboardService

logger = logging.getLogger("Journey.LeaderboardCog")

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
                # 1. Fetch top 10 users for this page
                top_users = await LeaderboardService.get_leaderboard(
                    session=session,
                    guild_id=self.guild_id,
                    filter_type=self.timeframe_val,
                    limit=self.limit,
                    offset=offset
                )
                
                # 2. Fetch current member's personal rank
                user_rank, user_stats = await LeaderboardService.get_user_rank(
                    session=session,
                    guild_id=self.guild_id,
                    user_id=self.author_id,
                    filter_type=self.timeframe_val
                )
                
                # 3. Fetch total ranked users count
                total_count = await LeaderboardService.get_ranked_users_count(
                    session=session,
                    guild_id=self.guild_id,
                    filter_type=self.timeframe_val
                )
                
                # 4. Fetch GuildSettings to calculate XP progress accurately
                from bot.models.guild import GuildSettings
                from bot.utils.curves import get_curve
                from bot.services.xp_service import XPService
                
                guild_settings_result = await session.execute(
                    select(GuildSettings).filter_by(guild_id=self.guild_id)
                )
                guild_settings = guild_settings_result.scalar_one_or_none()
                
            except Exception as e:
                logger.error(f"Failed to query database for leaderboard page: {e}", exc_info=True)
                await interaction.response.send_message("❌ Unable to load leaderboard data.", ephemeral=True)
                return

        # Fallbacks for guild settings
        curve_name = guild_settings.xp_curve if guild_settings else "linear"
        multiplier = float(guild_settings.xp_multiplier) if guild_settings else 1.0
        curve = get_curve(curve_name)
        base_xp = XPService.BASE_XP

        # Build Leaderboard Rows
        leaderboard_data = []
        for idx, stats in enumerate(top_users):
            rank = offset + idx + 1
            member = interaction.guild.get_member(stats.user_id)
            username = member.display_name if member else f"User {stats.user_id}"
            avatar = member.display_avatar.url if member else None
            
            if self.timeframe_val == "daily":
                score = stats.xp_daily
            elif self.timeframe_val == "weekly":
                score = stats.xp_weekly
            elif self.timeframe_val == "monthly":
                score = stats.xp_monthly
            else:
                score = stats.xp
                
            leaderboard_data.append({
                "rank": rank,
                "user_id": stats.user_id,
                "username": username,
                "avatar": avatar,
                "level": stats.level,
                "score": score
            })

        # Build Caller details
        caller_member = interaction.guild.get_member(self.author_id)
        caller_username = caller_member.display_name if caller_member else "Unknown User"
        caller_avatar = caller_member.display_avatar.url if caller_member else None
        
        caller_level = user_stats.level if user_stats else 1
        caller_xp = user_stats.xp if user_stats else 0
        
        if self.timeframe_val == "daily":
            caller_score = user_stats.xp_daily if user_stats else 0
        elif self.timeframe_val == "weekly":
            caller_score = user_stats.xp_weekly if user_stats else 0
        elif self.timeframe_val == "monthly":
            caller_score = user_stats.xp_monthly if user_stats else 0
        else:
            caller_score = caller_xp

        # Calculate caller XP progress using the curve
        start_xp = curve.cumulative_xp_for_level(caller_level, base_xp, multiplier)
        next_xp = curve.cumulative_xp_for_level(caller_level + 1, base_xp, multiplier)
        current_level_xp = max(0, caller_xp - start_xp)
        next_level_xp = max(1, next_xp - start_xp)
        
        caller_path_name = user_stats.master_path.name if (user_stats and user_stats.master_path) else None

        # Build final JSON Payload
        render_payload = {
            "timeframe_name": self.title_timeframe,
            "server_name": interaction.guild.name,
            "total_players": total_count,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "current_page": self.current_page,
            "total_pages": max(1, (total_count + self.limit - 1) // self.limit),
            "caller": {
                "user_id": self.author_id,
                "username": caller_username,
                "avatar": caller_avatar,
                "rank": user_rank,
                "score": caller_score,
                "level": caller_level,
                "path_name": caller_path_name,
                "current_level_xp": current_level_xp,
                "next_level_xp": next_level_xp
            },
            "leaderboard": leaderboard_data
        }

        # Defer message update since Playwright rendering takes ~100ms
        await interaction.response.defer()

        # Render the template in Playwright
        from bot.services.render import RendererService
        
        try:
            png_bytes = await RendererService.render_template(
                template_name="leaderboard",
                data=render_payload,
                width=1200,
                height=900
            )
            
            # Send file update in edit_message
            file = discord.File(io.BytesIO(png_bytes), filename="leaderboard.png")
            self.update_button_states()
            await interaction.followup.edit_message(message_id=interaction.message.id, attachments=[file], view=self)
            
        except Exception as e:
            logger.error(f"Renderer Service failed: {e}", exc_info=True)
            await interaction.followup.send("❌ Error rendering leaderboard image.", ephemeral=True)

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

        # Defer immediately since we will call the playwright rendering engine which takes ~100-150ms
        await interaction.response.defer()
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            try:
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
                
                # 4. Fetch GuildSettings
                from bot.models.guild import GuildSettings
                from bot.utils.curves import get_curve
                from bot.services.xp_service import XPService
                
                guild_settings_result = await session.execute(
                    select(GuildSettings).filter_by(guild_id=guild_id)
                )
                guild_settings = guild_settings_result.scalar_one_or_none()
                
            except Exception as e:
                logger.error(f"Failed to query database for leaderboard initial view: {e}", exc_info=True)
                await interaction.followup.send("❌ Unable to load leaderboard data.", ephemeral=True)
                return

        # Fallbacks for guild settings
        curve_name = guild_settings.xp_curve if guild_settings else "linear"
        multiplier = float(guild_settings.xp_multiplier) if guild_settings else 1.0
        curve = get_curve(curve_name)
        base_xp = XPService.BASE_XP

        # Build Leaderboard Rows
        leaderboard_data = []
        for idx, stats in enumerate(top_users):
            rank = idx + 1
            member = interaction.guild.get_member(stats.user_id)
            username = member.display_name if member else f"User {stats.user_id}"
            avatar = member.display_avatar.url if member else None
            
            if timeframe_val == "daily":
                score = stats.xp_daily
            elif timeframe_val == "weekly":
                score = stats.xp_weekly
            elif timeframe_val == "monthly":
                score = stats.xp_monthly
            else:
                score = stats.xp
                
            leaderboard_data.append({
                "rank": rank,
                "user_id": stats.user_id,
                "username": username,
                "avatar": avatar,
                "level": stats.level,
                "score": score
            })

        # Build Caller details
        caller_member = interaction.guild.get_member(interaction.user.id)
        caller_username = caller_member.display_name if caller_member else "Unknown User"
        caller_avatar = caller_member.display_avatar.url if caller_member else None
        
        caller_level = user_stats.level if user_stats else 1
        caller_xp = user_stats.xp if user_stats else 0
        
        if timeframe_val == "daily":
            caller_score = user_stats.xp_daily if user_stats else 0
        elif timeframe_val == "weekly":
            caller_score = user_stats.xp_weekly if user_stats else 0
        elif timeframe_val == "monthly":
            caller_score = user_stats.xp_monthly if user_stats else 0
        else:
            caller_score = caller_xp

        # Calculate caller XP progress
        start_xp = curve.cumulative_xp_for_level(caller_level, base_xp, multiplier)
        next_xp = curve.cumulative_xp_for_level(caller_level + 1, base_xp, multiplier)
        current_level_xp = max(0, caller_xp - start_xp)
        next_level_xp = max(1, next_xp - start_xp)
        
        caller_path_name = user_stats.master_path.name if (user_stats and user_stats.master_path) else None

        title_timeframe = timeframe.name if timeframe else "All Time"
        total_pages = max(1, (total_count + 9) // 10)

        # Build final JSON Payload
        render_payload = {
            "timeframe_name": title_timeframe,
            "server_name": interaction.guild.name,
            "total_players": total_count,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "current_page": 1,
            "total_pages": total_pages,
            "caller": {
                "user_id": interaction.user.id,
                "username": caller_username,
                "avatar": caller_avatar,
                "rank": user_rank,
                "score": caller_score,
                "level": caller_level,
                "path_name": caller_path_name,
                "current_level_xp": current_level_xp,
                "next_level_xp": next_level_xp
            },
            "leaderboard": leaderboard_data
        }

        # Render the template in Playwright
        from bot.services.render import RendererService
        
        try:
            png_bytes = await RendererService.render_template(
                template_name="leaderboard",
                data=render_payload,
                width=1200,
                height=900
            )
            
            # Send file to Discord
            file = discord.File(io.BytesIO(png_bytes), filename="leaderboard.png")
            
            view = LeaderboardView(
                author_id=interaction.user.id,
                guild_id=guild_id,
                timeframe_val=timeframe_val,
                title_timeframe=title_timeframe,
                total_count=total_count,
                limit=10
            )
            
            message = await interaction.followup.send(file=file, view=view)
            view.message = message
            
        except Exception as e:
            logger.warning(f"Renderer Service failed, falling back to text leaderboard: {e}")
            
            description_lines = []
            if not leaderboard_data:
                description_lines.append("*No members have earned XP in this timeframe yet.*")
            else:
                for stats in leaderboard_data:
                    medal = "🥇" if stats["rank"] == 1 else "🥈" if stats["rank"] == 2 else "🥉" if stats["rank"] == 3 else f"#{stats['rank']}"
                    description_lines.append(
                        f"{medal} **{stats['username']}** - Level {stats['level']} ({stats['score']:,} XP)"
                    )
            
            embed = discord.Embed(
                title=f"📈 Journey Leaderboard - {title_timeframe} XP",
                description="\n".join(description_lines),
                color=discord.Color.gold()
            )
            
            if user_stats:
                rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
                embed.set_footer(
                    text=f"Your Standing: {rank_str} | {caller_username} | {caller_score:,} XP | Page 1/{total_pages}"
                )
            else:
                embed.set_footer(
                    text=f"Your Standing: Unranked | {caller_username} | 0 XP | Page 1/{total_pages}"
                )
                
            await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboards(bot))
