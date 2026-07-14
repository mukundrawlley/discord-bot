import discord
from discord.ext import commands
from discord import app_commands
import logging

logger = logging.getLogger("Journey.GeneralCog")

class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Displays a list of all available commands.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Sends a beautiful menu containing all user and administrative slash commands."""
        embed = discord.Embed(
            title="✨ Journey Bot - Commands List ✨",
            description="Welcome to Journey, a modular leveling and character collection Discord bot! Here is a summary of all commands.",
            color=discord.Color.blurple()
        )
        
        # User Commands Category
        user_cmds = (
            "**`/profile [member]`** - View your/another user's rank, level, XP, and path details.\n"
            "**`/path choose [path]`** - Join a specific Master Path.\n"
            "**`/xp view [member]`** - View leveling stats.\n"
            "**`/rank view [member]`** - Shortcut to view path rank status.\n"
            "**`/leaderboard [type] [filter]`** - Display guild leaderboards.\n"
            "**`/help`** - Shows this menu."
        )
        embed.add_field(name="🌐 User Commands", value=user_cmds, inline=False)
        
        # Admin Commands Category (Check permissions internally to show only relevant or mark clearly)
        admin_cmds = (
            "**`/xp settings [opts]`** - Configure XP system variables.\n"
            "**`/xp add/remove/set/reset`** - Manage user XP scores.\n"
            "**`/level set/reset`** - Adjust user levels.\n"
            "**`/path create/edit/delete/list/role/ranks`** - Manage guild Master Paths.\n"
            "**`/rank add/edit/remove`** - Configure Path Rank role thresholds.\n"
            "**`/recalculatexp`** - Recalculate levels for all members.\n"
            "**`/profile reset`** - Reset stats and path choice for a user.\n"
            "**`/reload [cog]`** - Hot-reload bot logic."
        )
        embed.add_field(name="⚙️ Admin Commands (Manage Guild)", value=admin_cmds, inline=False)
        
        embed.set_footer(text="Journey Bot | Phase 1 Core Leveling")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="reload", description="Hot-reloads a specific cog or all cogs.")
    @app_commands.default_permissions(manage_guild=True)
    async def reload_command(self, interaction: discord.Interaction, cog_name: str | None = None) -> None:
        """Admin command to reload python modules (cogs) on-the-fly."""
        # Defer response since reloading might take a brief second
        await interaction.response.defer(ephemeral=True)
        
        cogs_to_reload = []
        if cog_name:
            # Check if it starts with bot.cogs.
            full_name = cog_name if cog_name.startswith("bot.cogs.") else f"bot.cogs.{cog_name}"
            cogs_to_reload.append(full_name)
        else:
            # Reload all registered extensions
            cogs_to_reload = list(self.bot.extensions.keys())
            
        success_list = []
        fail_list = []
        
        for extension in cogs_to_reload:
            try:
                await self.bot.reload_extension(extension)
                success_list.append(extension.split(".")[-1])
            except Exception as e:
                logger.error(f"Failed to reload cog {extension}: {e}")
                fail_list.append(f"{extension.split('.')[-1]} ({type(e).__name__})")
                
        # Build clean message response
        msg_parts = []
        if success_list:
            msg_parts.append(f"Successfully reloaded cogs: {', '.join(success_list)}")
        if fail_list:
            msg_parts.append(f"❌ Failed to reload: {', '.join(fail_list)}")
            
        final_msg = "\n".join(msg_parts) or "No cogs reloaded."
        await interaction.followup.send(content=final_msg, ephemeral=True)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
