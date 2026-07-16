import discord
from discord.ext import commands
from discord import app_commands
import logging
import unicodedata
from datetime import datetime, timezone

from bot.database.connection import get_db_session
from bot.services.leaderboard_service import LeaderboardService

logger = logging.getLogger("Journey.LeaderboardCog")

def get_char_width(char: str) -> int:
    o = ord(char)
    cat = unicodedata.category(char)
    # Zero-width combining marks, controls, and formatting characters
    if cat.startswith('M') or cat.startswith('C'):
        return 0
    if 32 <= o <= 126:
        return 1
    status = unicodedata.east_asian_width(char)
    if status in ('W', 'F', 'A'):
        return 2
    if cat.startswith('S') or o > 0xffff:
        return 2
    return 1

def get_visual_width(s: str) -> int:
    return sum(get_char_width(c) for c in s)

def pad_visual(s: str, target_width: int) -> str:
    total_w = get_visual_width(s)
    if total_w <= target_width:
        return s + ' ' * (target_width - total_w)
    
    limit = target_width - 3
    current_width = 0
    truncated_chars = []
    for char in s:
        char_w = get_char_width(char)
        if current_width + char_w > limit:
            break
        truncated_chars.append(char)
        current_width += char_w
        
    truncated_str = "".join(truncated_chars) + "..."
    final_w = get_visual_width(truncated_str)
    return truncated_str + ' ' * (target_width - final_w)

def format_rank_col(rank: int, is_caller: bool) -> str:
    if is_caller:
        if rank < 10:
            return f"★ {rank}  "
        else:
            return f"★ {rank} "
    if rank == 1:
        return " 🥇1  "
    elif rank == 2:
        return " 🥈2  "
    elif rank == 3:
        return " 🥉3  "
    else:
        if rank < 10:
            return f"  {rank}   "
        else:
            return f"  {rank}  "

def formatXP(score: int) -> str:
    return f"{score:,} XP"

def truncateUsername(name: str, max_len: int) -> str:
    limit = max_len - 3
    current_width = 0
    truncated_chars = []
    for char in name:
        char_w = get_char_width(char)
        if current_width + char_w > limit:
            break
        truncated_chars.append(char)
        current_width += char_w
    return "".join(truncated_chars) + "..."

def padColumn(text: str, width: int, align: str = 'left') -> str:
    total_w = get_visual_width(text)
    if total_w >= width:
        return text
    if align == 'right':
        return ' ' * (width - total_w) + text
    else:
        return text + ' ' * (width - total_w)

def centerText(text: str, width: int) -> str:
    total_w = get_visual_width(text)
    if total_w >= width:
        return text
    left_spaces = (width - total_w) // 2
    right_spaces = width - total_w - left_spaces
    return ' ' * left_spaces + text + ' ' * right_spaces

def formatLeaderboardRow(stats: dict, rank: int, is_caller: bool, timeframe_val: str, interaction: discord.Interaction) -> str:
    member = interaction.guild.get_member(stats["user_id"])
    name = member.display_name if member else f"User {stats['user_id']}"
    
    # Target column widths: PLAYER_WIDTH = 20, LEVEL_WIDTH = 9, SCORE_WIDTH = 12
    name_width_val = get_visual_width(name)
    if name_width_val > 20:
        truncated = truncateUsername(name, 20)
        name_padded = padColumn(truncated, 20, 'left')
    else:
        name_padded = padColumn(name, 20, 'left')
        
    lvl_padded = centerText(f"Lv {stats['level']}", 9)
    
    if timeframe_val == "daily":
        score = stats["xp_daily"]
    elif timeframe_val == "weekly":
        score = stats["xp_weekly"]
    elif timeframe_val == "monthly":
        score = stats["xp_monthly"]
    else:
        score = stats["xp"]
        
    score_padded = padColumn(formatXP(score), 12, 'right')
    rank_padded = format_rank_col(rank, is_caller)
    
    # Borderless row with 2 spaces separator
    row_content = f"{rank_padded}  {name_padded}  {lvl_padded}  {score_padded}"
    
    if is_caller:
        return f"\u001b[1;35m{row_content}\u001b[0m"
    elif rank == 1:
        return f"\u001b[1;33m{row_content}\u001b[0m"
    elif rank == 2:
        return f"\u001b[1;37m{row_content}\u001b[0m"
    elif rank == 3:
        return f"\u001b[0;33m{row_content}\u001b[0m"
    else:
        return f"\u001b[0;37m{row_content}\u001b[0m"

def generateTable(users: list, start_rank: int, timeframe_val: str, interaction: discord.Interaction, caller_id: int) -> str:
    if not users:
        return "No players have entered the leaderboard yet."
        
    # Borderless header with 2 spaces separator
    header = f"{'Rank':^6}  {'Player':<20}  {centerText('Level', 9)}  {padColumn('Score', 12, 'right')}"
    colored_header = f"\u001b[1;37m{header}\u001b[0m"
    
    rows = []
    for idx, stats in enumerate(users):
        rank = start_rank + idx
        is_caller = (stats["user_id"] == caller_id)
        row_text = formatLeaderboardRow(stats, rank, is_caller, timeframe_val, interaction)
        rows.append(row_text)
        
    return "```ansi\n" + colored_header + "\n" + "\n".join(rows) + "\n```"


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
        
    async def update_message(self, interaction: discord.Interaction, force_refresh: bool = False):
        offset = (self.current_page - 1) * self.limit
        async with get_db_session() as session:
            try:
                top_users = await LeaderboardService.get_leaderboard(
                    session=session,
                    guild_id=self.guild_id,
                    filter_type=self.timeframe_val,
                    limit=self.limit,
                    offset=offset,
                    force_refresh=force_refresh
                )
                
                user_rank, user_stats = await LeaderboardService.get_user_rank(
                    session=session,
                    guild_id=self.guild_id,
                    user_id=self.author_id,
                    filter_type=self.timeframe_val,
                    force_refresh=force_refresh
                )
                
                # Fetch fresh total count if refreshing
                if force_refresh:
                    self.total_count = await LeaderboardService.get_ranked_users_count(
                        session=session,
                        guild_id=self.guild_id,
                        filter_type=self.timeframe_val,
                        force_refresh=True
                    )
                    self.total_pages = max(1, (self.total_count + self.limit - 1) // self.limit)
                    if self.current_page > self.total_pages:
                        self.current_page = self.total_pages
            except Exception as e:
                logger.error(f"Failed to query database for leaderboard: {e}", exc_info=True)
                await interaction.response.send_message("❌ Unable to load leaderboard.", ephemeral=True)
                return
            
        table_str = generateTable(top_users, offset + 1, self.timeframe_val, interaction, self.author_id)
        
        # User Standing Block formatting
        if user_stats:
            if self.timeframe_val == "level":
                rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
                standing_text = f"Rank: **{rank_str}** • Level **{user_stats['level']}**"
            else:
                if self.timeframe_val == "daily":
                    caller_score = user_stats["xp_daily"]
                elif self.timeframe_val == "weekly":
                    caller_score = user_stats["xp_weekly"]
                elif self.timeframe_val == "monthly":
                    caller_score = user_stats["xp_monthly"]
                else:
                    caller_score = user_stats["xp"]
                    
                rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
                standing_text = f"Rank: **{rank_str}** • XP: **{caller_score:,}** (Level {user_stats['level']})"
        else:
            standing_text = "You're currently unranked."
            
        embed_description = (
            f"👤 **Your Standing**\n"
            f"{standing_text}\n\n"
            f"{table_str}"
        )
        
        # Dynamic Embed color
        if self.timeframe_val == "level":
            embed_color = discord.Color.teal()
        elif self.timeframe_val == "daily":
            embed_color = discord.Color.red()
        elif self.timeframe_val == "weekly":
            embed_color = discord.Color.blue()
        elif self.timeframe_val == "monthly":
            embed_color = discord.Color.purple()
        else:
            embed_color = discord.Color.gold()
            
        embed = discord.Embed(
            title="🏆 Leaderboard",
            description=embed_description,
            color=embed_color
        )
        
        embed.add_field(name="Season Type", value=f"✨ {self.title_timeframe} Rankings", inline=False)
        
        # Set Server icon as thumbnail if available
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
            
        caller_name = interaction.user.display_name
        embed.set_footer(
            text=f"Page {self.current_page}/{self.total_pages} • Total Players: {self.total_count} • Requested by {caller_name}"
        )
        embed.timestamp = datetime.now(timezone.utc)
            
        self.update_button_states()
        if interaction.response.is_done():
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬅ Previous", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            await self.update_message(interaction)
            
    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, custom_id="refresh_leaderboard")
    async def refresh_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_message(interaction, force_refresh=True)
            
    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            await self.update_message(interaction)

class Leaderboards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    leaderboard = app_commands.Group(name="leaderboard", description="View server leaderboards.")

    @leaderboard.command(name="xp", description="Displays the XP leaderboard rankings.")
    @app_commands.describe(timeframe="The timeframe filter.")
    @app_commands.choices(
        timeframe=[
            app_commands.Choice(name="All Time", value="all_time"),
            app_commands.Choice(name="Monthly", value="monthly"),
            app_commands.Choice(name="Weekly", value="weekly"),
            app_commands.Choice(name="Daily", value="daily")
        ]
    )
    async def leaderboard_xp(
        self,
        interaction: discord.Interaction,
        timeframe: app_commands.Choice[str] | None = None
    ) -> None:
        """Displays user standings in XP rankings."""
        timeframe_val = timeframe.value if timeframe else "all_time"
        title_timeframe = timeframe.name if timeframe else "All Time"
        await self._show_leaderboard(interaction, timeframe_val, title_timeframe)

    @leaderboard.command(name="level", description="Displays the Level leaderboard rankings.")
    async def leaderboard_level(
        self,
        interaction: discord.Interaction
    ) -> None:
        """Displays user standings in Lifetime Levels."""
        await self._show_leaderboard(interaction, "level", "Lifetime Level")

    async def _show_leaderboard(
        self,
        interaction: discord.Interaction,
        timeframe_val: str,
        title_timeframe: str
    ) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id

        async with get_db_session() as session:
            try:
                # 1. Fetch top 10 users for initial page (read from cache if hit)
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
            except Exception as e:
                logger.error(f"Failed to query database for initial leaderboard view: {e}", exc_info=True)
                await interaction.followup.send("❌ Unable to load leaderboard.", ephemeral=True)
                return

        table_str = generateTable(top_users, 1, timeframe_val, interaction, interaction.user.id)

        # User Standing Block formatting
        if user_stats:
            if timeframe_val == "level":
                rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
                standing_text = f"Rank: **{rank_str}** • Level **{user_stats['level']}**"
            else:
                if timeframe_val == "daily":
                    caller_score = user_stats["xp_daily"]
                elif timeframe_val == "weekly":
                    caller_score = user_stats["xp_weekly"]
                elif timeframe_val == "monthly":
                    caller_score = user_stats["xp_monthly"]
                else:
                    caller_score = user_stats["xp"]
                    
                rank_str = f"#{user_rank}" if user_rank > 0 else "Unranked"
                standing_text = f"Rank: **{rank_str}** • XP: **{caller_score:,}** (Level {user_stats['level']})"
        else:
            standing_text = "You're currently unranked."
            
        embed_description = (
            f"👤 **Your Standing**\n"
            f"{standing_text}\n\n"
            f"{table_str}"
        )

        # Dynamic Embed color
        if timeframe_val == "level":
            embed_color = discord.Color.teal()
        elif timeframe_val == "daily":
            embed_color = discord.Color.red()
        elif timeframe_val == "weekly":
            embed_color = discord.Color.blue()
        elif timeframe_val == "monthly":
            embed_color = discord.Color.purple()
        else:
            embed_color = discord.Color.gold()

        # Build Embed
        embed = discord.Embed(
            title="🏆 Leaderboard",
            description=embed_description,
            color=embed_color
        )
        
        embed.add_field(name="Season Type", value=f"✨ {title_timeframe} Rankings", inline=False)
        
        # Set Server icon as thumbnail if available
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        total_pages = max(1, (total_count + 9) // 10)
        caller_name = interaction.user.display_name
        embed.set_footer(
            text=f"Page 1/{total_pages} • Total Players: {total_count} • Requested by {caller_name}"
        )
        embed.timestamp = datetime.now(timezone.utc)

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
