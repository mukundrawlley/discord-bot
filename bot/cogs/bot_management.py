import discord
from discord import app_commands
from discord.ext import commands
import logging

logger = logging.getLogger("Journey.BotManagement")


@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
class BotGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="bot", description="Bot channel restriction & channel management commands.")

    @app_commands.command(name="restrict", description="Restricts or allows a bot in a specific text channel (Staff only).")
    @app_commands.describe(
        bot="The bot user to restrict or allow.",
        channel="The text channel to configure permissions for.",
        allow="Set to True to allow messaging, or False to restrict/deny messaging. (Default: False)"
    )
    @app_commands.default_permissions(administrator=True)
    async def bot_restrict(
        self,
        interaction: discord.Interaction,
        bot: discord.Member,
        channel: discord.TextChannel,
        allow: bool = False
    ) -> None:
        """Restricts or allows a bot in a specific channel."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        # 1. Staff permission check
        member_obj = interaction.guild.get_member(interaction.user.id)
        is_staff = member_obj and (
            member_obj.guild_permissions.administrator or 
            member_obj.guild_permissions.manage_channels or 
            member_obj.guild_permissions.manage_guild
        )
        if not is_staff:
            await interaction.response.send_message("❌ Only Server Staff and Administrators can manage bot channel restrictions.", ephemeral=True)
            return

        # 2. Verify target is a bot user
        if not bot.bot:
            await interaction.response.send_message(f"❌ **{bot.display_name}** is not a bot. This command is designed for restricting bots.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if allow:
                await channel.set_permissions(
                    bot,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    reason=f"Journey Bot Restriction: Allowed by Staff {interaction.user.display_name}"
                )
                action_text = "🟢 **Allowed**"
                detail_text = f"**{bot.mention}** is now allowed to view and send messages in {channel.mention}."
            else:
                await channel.set_permissions(
                    bot,
                    send_messages=False,
                    reason=f"Journey Bot Restriction: Restricted by Staff {interaction.user.display_name}"
                )
                action_text = "🔴 **Restricted**"
                detail_text = f"**{bot.mention}** is now blocked from sending messages in {channel.mention}."

            embed = discord.Embed(
                title=f"🤖 Bot Channel Restriction Updated",
                description=f"**Status:** {action_text}\n{detail_text}",
                color=discord.Color.green() if allow else discord.Color.red()
            )
            embed.set_footer(text=f"Configured by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Journey Bot lacks 'Manage Channels' or 'Manage Permissions' permission on Discord to configure overrides for {channel.mention}.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error executing bot_restrict: {e}", exc_info=True)
            await interaction.followup.send("❌ An unexpected error occurred while modifying channel permissions.", ephemeral=True)

    @app_commands.command(name="isolate", description="Isolates a bot so it can ONLY message in a single specified channel (Staff only).")
    @app_commands.describe(
        bot="The bot user to isolate.",
        allowed_channel="The ONLY text channel where this bot will be allowed to send messages."
    )
    @app_commands.default_permissions(administrator=True)
    async def bot_isolate(
        self,
        interaction: discord.Interaction,
        bot: discord.Member,
        allowed_channel: discord.TextChannel
    ) -> None:
        """Isolates a bot so it can only send messages in the specified channel across the entire server."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        member_obj = interaction.guild.get_member(interaction.user.id)
        is_staff = member_obj and (
            member_obj.guild_permissions.administrator or 
            member_obj.guild_permissions.manage_channels or 
            member_obj.guild_permissions.manage_guild
        )
        if not is_staff:
            await interaction.response.send_message("❌ Only Server Staff and Administrators can manage bot channel restrictions.", ephemeral=True)
            return

        if not bot.bot:
            await interaction.response.send_message(f"❌ **{bot.display_name}** is not a bot.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        restricted_count = 0
        try:
            for ch in interaction.guild.text_channels:
                if ch.id == allowed_channel.id:
                    await ch.set_permissions(
                        bot,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        reason=f"Journey Bot Isolation: Primary Allowed Channel set by {interaction.user.display_name}"
                    )
                else:
                    await ch.set_permissions(
                        bot,
                        send_messages=False,
                        reason=f"Journey Bot Isolation: Blocked by {interaction.user.display_name}"
                    )
                    restricted_count += 1

            embed = discord.Embed(
                title=f"🔒 Bot Isolated Successfully",
                description=(
                    f"**Bot:** {bot.mention} ({bot.display_name})\n"
                    f"🟢 **Allowed Channel:** {allowed_channel.mention}\n"
                    f"🔴 **Blocked Channels:** `{restricted_count}` text channels"
                ),
                color=discord.Color.gold()
            )
            embed.set_footer(text="All other text channels in the server have been locked for this bot.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send("❌ Journey Bot lacks 'Manage Channels' permissions to isolate this bot across server channels.", ephemeral=True)

    @app_commands.command(name="clear", description="Clears bot channel restriction overwrites (Staff only).")
    @app_commands.describe(
        bot="The bot user whose restrictions should be cleared.",
        channel="Optional: Clear restrictions for a specific channel only. (Omit to clear all channels)"
    )
    @app_commands.default_permissions(administrator=True)
    async def bot_clear(
        self,
        interaction: discord.Interaction,
        bot: discord.Member,
        channel: discord.TextChannel | None = None
    ) -> None:
        """Clears custom permission overwrites for a bot."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        member_obj = interaction.guild.get_member(interaction.user.id)
        is_staff = member_obj and (
            member_obj.guild_permissions.administrator or 
            member_obj.guild_permissions.manage_channels or 
            member_obj.guild_permissions.manage_guild
        )
        if not is_staff:
            await interaction.response.send_message("❌ Only Server Staff and Administrators can manage bot channel restrictions.", ephemeral=True)
            return

        if not bot.bot:
            await interaction.response.send_message(f"❌ **{bot.display_name}** is not a bot.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if channel:
                await channel.set_permissions(bot, overwrite=None, reason=f"Journey Bot Restriction Cleared by {interaction.user.display_name}")
                msg = f"🧹 Cleared channel restriction overwrites for **{bot.mention}** in {channel.mention}."
            else:
                cleared_count = 0
                for ch in interaction.guild.text_channels:
                    if bot in ch.overwrites:
                        await ch.set_permissions(bot, overwrite=None, reason=f"Journey Bot Restriction Cleared by {interaction.user.display_name}")
                        cleared_count += 1
                msg = f"🧹 Cleared custom channel restriction overwrites for **{bot.mention}** across `{cleared_count}` channels."

            await interaction.followup.send(msg, ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send("❌ Journey Bot lacks 'Manage Channels' permission to clear overwrites.", ephemeral=True)

    @app_commands.command(name="status", description="Views active channel restrictions for a bot (Staff only).")
    @app_commands.describe(bot="The bot user to inspect.")
    @app_commands.default_permissions(administrator=True)
    async def bot_status(
        self,
        interaction: discord.Interaction,
        bot: discord.Member
    ) -> None:
        """Displays active channel permission overwrites for a bot."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        if not bot.bot:
            await interaction.response.send_message(f"❌ **{bot.display_name}** is not a bot.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        allowed_channels = []
        denied_channels = []

        for ch in interaction.guild.text_channels:
            overwrites = ch.overwrites_for(bot)
            if overwrites.send_messages is True:
                allowed_channels.append(ch.mention)
            elif overwrites.send_messages is False:
                denied_channels.append(ch.mention)

        allowed_str = ", ".join(allowed_channels) if allowed_channels else "*None specified (Uses Default Server Roles)*"
        denied_str = ", ".join(denied_channels) if denied_channels else "*None explicitly blocked*"

        embed = discord.Embed(
            title=f"🤖 Channel Restriction Status: {bot.display_name}",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=bot.display_avatar.url)
        embed.add_field(name="🟢 Explicitly Allowed Channels", value=allowed_str, inline=False)
        embed.add_field(name="🔴 Explicitly Blocked Channels", value=denied_str, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


class BotManagementCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot_group = BotGroup()
        self.bot.tree.add_command(self.bot_group)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.bot_group.name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotManagementCog(bot))
