import discord
from discord.ext import commands
from discord import app_commands
import logging
from sqlalchemy.future import select
from sqlalchemy import delete

from bot.database.connection import get_db_session
from bot.services.database_service import DatabaseService
from bot.services.path_service import PathService
from bot.models.path import MasterPath
from bot.models.rank import PathRank
from bot.models.user import UserGuildStats
from bot.models.guild import Guild

logger = logging.getLogger("Journey.PathsCog")

class Paths(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------------------
    # Onboarding Role Sync Listener
    # -------------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Listens to member role changes to sync with Master Path onboarding configurations."""
        if before.roles == after.roles:
            return
            
        guild_id = after.guild.id
        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            # Fetch all paths to see if any role maps to them
            paths = await PathService.get_paths(session, guild_id)
            if not paths:
                return
                
            path_role_ids = {p.discord_role_id for p in paths}
            
            # Determine which path roles were added or removed
            before_path_roles = {r.id for r in before.roles if r.id in path_role_ids}
            after_path_roles = {r.id for r in after.roles if r.id in path_role_ids}
            
            if before_path_roles == after_path_roles:
                return
                
            # Let's perform sync
            stats = await DatabaseService.get_or_create_stats(session, guild_id, after.id)
            current_path_role_id = None
            if stats.master_path:
                current_path_role_id = stats.master_path.discord_role_id
                
            # If they got a new path role
            added_roles = after_path_roles - before_path_roles
            removed_roles = before_path_roles - after_path_roles
            
            target_path_id = None
            if added_roles:
                # User was assigned a new path role, take the first one
                role_id = list(added_roles)[0]
                target_path = next(p for p in paths if p.discord_role_id == role_id)
                target_path_id = target_path.id
            elif removed_roles and current_path_role_id in removed_roles:
                # User lost their current path role
                target_path_id = None
            else:
                # No change in active path assignment
                return
                
            # Assign path and fetch role corrections
            to_add, to_remove = await PathService.assign_path(
                session, settings, after.id, target_path_id
            )
            
            # Apply Discord additions/removals
            add_objs = [after.guild.get_role(rid) for rid in to_add if after.guild.get_role(rid)]
            remove_objs = [after.guild.get_role(rid) for rid in to_remove if after.guild.get_role(rid)]
            
            # Keep only valid role operations
            remove_objs = [r for r in remove_objs if r in after.roles]
            if remove_objs:
                try:
                    await after.remove_roles(*remove_objs, reason="Journey Onboarding Synchronization")
                except discord.Forbidden:
                    logger.warning(f"Could not remove roles {remove_objs} from {after} (Forbidden).")
                    
            add_objs = [r for r in add_objs if r not in after.roles]
            if add_objs:
                try:
                    await after.add_roles(*add_objs, reason="Journey Onboarding Synchronization")
                except discord.Forbidden:
                    logger.warning(f"Could not add roles {add_objs} to {after} (Forbidden).")

    # -------------------------------------------------------------------------
    # Autocomplete Handlers
    # -------------------------------------------------------------------------
    async def _path_autocomplete(
        self, 
        interaction: discord.Interaction, 
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocompletes path search queries."""
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            result = await session.execute(
                select(MasterPath).filter_by(guild_id=guild_id)
            )
            paths = list(result.scalars())
            
        return [
            app_commands.Choice(name=p.name, value=p.name)
            for p in paths
            if current.lower() in p.name.lower()
        ][:25]

    # -------------------------------------------------------------------------
    # Slash Command Group
    # -------------------------------------------------------------------------
    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    class PathGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="path", description="Master Path management commands.")

    @app_commands.allowed_installs(guilds=True, users=False)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    class RankGroup(app_commands.Group):
        def __init__(self):
            super().__init__(name="rank", description="Path Rank progression rewards configuration.")

    path_group = PathGroup()
    rank_group = RankGroup()

    @rank_group.command(name="view", description="Displays current level and Path Rank details.")
    @app_commands.describe(member="The member to view rank details for.")
    async def rank_view_command(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        """Checks level and path rank progression stats."""
        target_member = member or interaction.user
        if target_member.bot:
            await interaction.response.send_message("Bots do not have Journey ranks.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            stats = await DatabaseService.get_or_create_stats(session, guild_id, target_member.id)
            path_name = "None"
            rank_name = "None"
            
            if stats.master_path:
                path_name = stats.master_path.name
                
                # Fetch ranks
                ranks_res = await session.execute(
                    select(PathRank)
                    .filter_by(path_id=stats.master_path_id)
                    .filter(PathRank.required_level <= stats.level)
                    .order_by(PathRank.required_level.desc())
                )
                highest_rank = ranks_res.scalars().first()
                if highest_rank:
                    rank_name = highest_rank.display_name
                    
            await interaction.response.send_message(
                f"🏆 **{target_member.display_name}**'s Rank Info:\n"
                f"• **Level**: {stats.level}\n"
                f"• **Master Path**: {path_name}\n"
                f"• **Path Rank**: {rank_name}"
            )

    @path_group.command(name="list", description="Lists all Master Paths configured in the server.")
    async def path_list_command(self, interaction: discord.Interaction) -> None:
        """Displays all Master Paths."""
        await interaction.response.defer()
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            paths = await PathService.get_paths(session, guild_id)
            
            if not paths:
                await interaction.followup.send("There are no Master Paths configured for this server yet.")
                return
                
            embed = discord.Embed(
                title="🛤️ Master Paths Available",
                description="Choose your destiny using `/path choose [name]`",
                color=discord.Color.blurple()
            )
            
            for path in paths:
                role = interaction.guild.get_role(path.discord_role_id)
                role_str = role.mention if role else f"Role ID: {path.discord_role_id}"
                desc = path.description or "*No description provided.*"
                embed.add_field(
                    name=f"{path.name} {'(Disabled)' if not path.enabled else ''}",
                    value=f"• Role: {role_str}\n• Info: {desc}",
                    inline=False
                )
                
            await interaction.followup.send(embed=embed)

    @path_group.command(name="choose", description="Join a specific Master Path.")
    @app_commands.describe(path="The path name to select.")
    @app_commands.autocomplete(path=_path_autocomplete)
    async def path_choose_command(self, interaction: discord.Interaction, path: str) -> None:
        """Allows users to choose their Master Path. Revokes old roles."""
        await interaction.response.defer()
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        
        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            # Resolve path object
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path or not target_path.enabled:
                await interaction.followup.send(f"❌ Master Path '{path}' was not found or is disabled.")
                return
                
            # Perform role mapping and database changes
            to_add, to_remove = await PathService.assign_path(
                session, settings, user_id, target_path.id
            )
            
            # Apply Discord additions/removals
            member = interaction.user
            add_objs = [interaction.guild.get_role(rid) for rid in to_add if interaction.guild.get_role(rid)]
            remove_objs = [interaction.guild.get_role(rid) for rid in to_remove if interaction.guild.get_role(rid)]
            
            removed_names = []
            remove_objs = [r for r in remove_objs if r in member.roles]
            if remove_objs:
                try:
                    await member.remove_roles(*remove_objs, reason="Journey Path Change")
                    removed_names = [r.name for r in remove_objs]
                except discord.Forbidden:
                    logger.warning(f"Could not remove roles {remove_objs} (Forbidden).")
                    
            add_objs = [r for r in add_objs if r not in member.roles]
            if add_objs:
                try:
                    await member.add_roles(*add_objs, reason="Journey Path Choice")
                except discord.Forbidden:
                    await interaction.followup.send(
                        f"⚠️ Changed path to **{target_path.name}**, but could not award the role. "
                        "Please verify the bot has permissions to manage roles."
                    )
                    return
                    
            removed_suffix = f" (Removed roles: {', '.join(removed_names)})" if removed_names else ""
            await interaction.followup.send(
                f"✅ You have successfully chosen the path of the **{target_path.name}**!{removed_suffix}"
            )

    # -------------------------------------------------------------------------
    # Admin Path Management Commands
    # -------------------------------------------------------------------------
    @path_group.command(name="create", description="[Admin Only] Creates a new Master Path.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        role="Associated Discord role.",
        name="Optional: Custom path name (Defaults to role name if omitted).",
        description="Path details.",
        color_hex="Embed display color in Hex (e.g. FF5500)."
    )
    async def path_create_command(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        name: str | None = None,
        description: str | None = None,
        color_hex: str | None = None
    ) -> None:
        """Admin command to register a Master Path."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        # Check Admin / Staff permissions
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not (member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        path_name = (name.strip() if name and name.strip() else role.name)
        
        # Validate hex color
        color_val = None
        if color_hex:
            try:
                color_val = int(color_hex.lstrip("#"), 16)
            except ValueError:
                await interaction.followup.send("❌ Invalid Hex color value.", ephemeral=True)
                return

        async with get_db_session() as session:
            await DatabaseService.get_or_create_guild(session, guild_id)
            
            # Check duplicates
            existing = await PathService.get_path_by_name(session, guild_id, path_name)
            if existing:
                await interaction.followup.send(f"❌ A path named **'{path_name}'** already exists.", ephemeral=True)
                return
                
            path = MasterPath(
                guild_id=guild_id,
                name=path_name,
                discord_role_id=role.id,
                description=description,
                color=color_val
            )
            session.add(path)
            await session.flush()
            
        await interaction.followup.send(f"✅ Created Master Path **{path_name}** mapped to {role.mention}.", ephemeral=True)

    @path_group.command(name="edit", description="[Admin Only] Edits an existing Master Path.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        path="Path name to edit.",
        new_name="New name.",
        role="New Discord role.",
        description="New details.",
        color_hex="New color.",
        enabled="Enable or disable path."
    )
    @app_commands.autocomplete(path=_path_autocomplete)
    async def path_edit_command(
        self,
        interaction: discord.Interaction,
        path: str,
        new_name: str | None = None,
        role: discord.Role | None = None,
        description: str | None = None,
        color_hex: str | None = None,
        enabled: bool | None = None
    ) -> None:
        """Admin command to update Master Path configurations."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        member_obj = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        async with get_db_session() as session:
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path:
                await interaction.followup.send(f"❌ Master Path '{path}' not found.", ephemeral=True)
                return
                
            if new_name is not None:
                target_path.name = new_name
            if role is not None:
                target_path.discord_role_id = role.id
            if description is not None:
                target_path.description = description
            if enabled is not None:
                target_path.enabled = enabled
            if color_hex is not None:
                try:
                    target_path.color = int(color_hex.lstrip("#"), 16)
                except ValueError:
                    await interaction.followup.send("❌ Invalid Hex color value.", ephemeral=True)
                    return
                    
            await session.flush()
            
        await interaction.followup.send(f"✅ Updated Master Path configuration.", ephemeral=True)

    @path_group.command(name="delete", description="[Admin Only] Deletes a Master Path.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(path="Path to delete.")
    @app_commands.autocomplete(path=_path_autocomplete)
    async def path_delete_command(self, interaction: discord.Interaction, path: str) -> None:
        """Deletes a Master Path. Resets affected users."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        member_obj = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        async with get_db_session() as session:
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path:
                await interaction.followup.send(f"❌ Master Path '{path}' not found.", ephemeral=True)
                return
                
            path_id = target_path.id
            
            # Fetch all affected users stats to strip role
            users_res = await session.execute(
                select(UserGuildStats).filter_by(guild_id=guild_id, master_path_id=path_id)
            )
            affected_users = list(users_res.scalars())
            
            # Delete from DB (ondelete CASCADE will delete ranks automatically)
            await session.delete(target_path)
            await session.flush()
            
            # Strip roles from Discord
            deleted_role_id = target_path.discord_role_id
            for stats in affected_users:
                member = interaction.guild.get_member(stats.user_id)
                if member:
                    role = interaction.guild.get_role(deleted_role_id)
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Journey Path Deleted")
                        except discord.Forbidden:
                            pass
                            
        await interaction.followup.send(f"✅ Deleted Master Path and reset path variables for {len(affected_users)} members.", ephemeral=True)

    @path_group.command(name="remove-user", description="[Admin Only] Removes the Master Path from a member.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member to remove the path from.")
    async def path_remove_user_command(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Removes a user's Master Path selection and revokes all path/rank roles."""
        if member.bot:
            await interaction.response.send_message("❌ Bots do not have Master Paths.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        member_obj = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return
        
        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            # Check if user has path
            stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            if not stats.master_path_id:
                await interaction.followup.send(f"❌ {member.mention} does not have an active Master Path.", ephemeral=True)
                return
                
            old_path = stats.master_path.name if stats.master_path else "Unknown"
            
            # assign_path with None removes it
            to_add, to_remove = await PathService.assign_path(
                session, settings, member.id, None
            )
            
            # Apply Discord additions/removals
            remove_objs = [interaction.guild.get_role(rid) for rid in to_remove if interaction.guild.get_role(rid)]
            remove_objs = [r for r in remove_objs if r in member.roles]
            if remove_objs:
                try:
                    await member.remove_roles(*remove_objs, reason="Journey Path Removed by Admin")
                except discord.Forbidden:
                    logger.warning(f"Could not remove roles {remove_objs} (Forbidden).")

            await session.flush()
            
        await interaction.followup.send(f"✅ Successfully removed path **{old_path}** from {member.mention}.", ephemeral=True)

    @path_group.command(name="change-user", description="[Admin Only] Changes the Master Path for a member.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The member to change path for.", path="The new path name.")
    @app_commands.autocomplete(path=_path_autocomplete)
    async def path_change_user_command(self, interaction: discord.Interaction, member: discord.Member, path: str) -> None:
        """Forces a Master Path change for a user and syncs roles."""
        if member.bot:
            await interaction.response.send_message("❌ Bots do not have Master Paths.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        member_obj = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings
            
            # Resolve path object
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path or not target_path.enabled:
                await interaction.followup.send(f"❌ Master Path '{path}' was not found or is disabled.", ephemeral=True)
                return
                
            to_add, to_remove = await PathService.assign_path(
                session, settings, member.id, target_path.id
            )
            
            # Apply Discord additions/removals
            add_objs = [interaction.guild.get_role(rid) for rid in to_add if interaction.guild.get_role(rid)]
            remove_objs = [interaction.guild.get_role(rid) for rid in to_remove if interaction.guild.get_role(rid)]
            
            remove_objs = [r for r in remove_objs if r in member.roles]
            if remove_objs:
                try:
                    await member.remove_roles(*remove_objs, reason="Journey Path Changed by Admin")
                except discord.Forbidden:
                    logger.warning(f"Could not remove roles {remove_objs} (Forbidden).")
                    
            add_objs = [r for r in add_objs if r not in member.roles]
            if add_objs:
                try:
                    await member.add_roles(*add_objs, reason="Journey Path Changed by Admin")
                except discord.Forbidden:
                    await interaction.followup.send(
                        f"⚠️ Changed path to **{target_path.name}**, but could not award the role. "
                        "Please verify the bot has permissions to manage roles.",
                        ephemeral=True
                    )
                    return
            await session.flush()
            
        await interaction.followup.send(f"✅ Successfully set {member.mention}'s Master Path to **{target_path.name}**.", ephemeral=True)

    @path_group.command(name="role", description="[Admin Only] Queries or updates role associated with a path.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(path="Path name.", role="Associated Discord role.")
    @app_commands.autocomplete(path=_path_autocomplete)
    async def path_role_command(self, interaction: discord.Interaction, path: str, role: discord.Role | None = None) -> None:
        """Admin helper command to check or change the base Discord role assigned to a Master Path."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        member_obj = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        async with get_db_session() as session:
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path:
                await interaction.followup.send(f"❌ Master Path '{path}' not found.", ephemeral=True)
                return
                
            if role is not None:
                target_path.discord_role_id = role.id
                await session.flush()
                await interaction.followup.send(f"✅ Updated role for {target_path.name} to {role.mention}.", ephemeral=True)
            else:
                curr_role = interaction.guild.get_role(target_path.discord_role_id)
                role_mention = curr_role.mention if curr_role else f"Role ID: {target_path.discord_role_id}"
                await interaction.followup.send(f"🛤️ Master Path **{target_path.name}** is mapped to: {role_mention}.", ephemeral=True)

    @path_group.command(name="ranks", description="Lists all rank progression rewards defined for a Master Path.")
    @app_commands.describe(path="Path name.")
    @app_commands.autocomplete(path=_path_autocomplete)
    async def path_ranks_command(self, interaction: discord.Interaction, path: str) -> None:
        """Displays ranks list for a path."""
        await interaction.response.defer()
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path:
                await interaction.followup.send(f"❌ Master Path '{path}' not found.")
                return
                
            ranks_res = await session.execute(
                select(PathRank)
                .filter_by(path_id=target_path.id)
                .order_by(PathRank.required_level.asc())
            )
            ranks = list(ranks_res.scalars())
            
            if not ranks:
                await interaction.followup.send(f"No rank progression rewards defined for path **{target_path.name}** yet.")
                return
                
            embed = discord.Embed(
                title=f"🏆 Ranks List - {target_path.name}",
                description="Progression rewards earned by leveling up:",
                color=target_path.color or discord.Color.blurple()
            )
            
            for rank in ranks:
                role = interaction.guild.get_role(rank.discord_role_id)
                role_str = role.mention if role else f"Role ID: {rank.discord_role_id}"
                embed.add_field(
                    name=f"Level {rank.required_level}: {rank.display_name}",
                    value=f"• Role: {role_str}",
                    inline=False
                )
                
            await interaction.followup.send(embed=embed)

    @path_group.command(name="repair", description="[Admin Only] Audits and syncs path roles and missing level reward roles for all members.")
    @app_commands.default_permissions(manage_guild=True)
    async def path_repair_command(self, interaction: discord.Interaction) -> None:
        """Audits all server members, awarding missing level rank rewards and syncing Master Path roles."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        member_obj = interaction.guild.get_member(interaction.user.id)
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings

            # Fetch all user stats with active master paths
            users_res = await session.execute(
                select(UserGuildStats)
                .filter_by(guild_id=guild_id)
                .filter(UserGuildStats.master_path_id.isnot(None))
            )
            user_stats_list = list(users_res.scalars())

            members_scanned = 0
            members_repaired = 0
            roles_added_total = 0
            roles_removed_total = 0
            repaired_summary = []

            for stats in user_stats_list:
                member = interaction.guild.get_member(stats.user_id)
                if not member:
                    continue

                members_scanned += 1
                roles_to_add, roles_to_remove = await PathService.evaluate_roles_for_level(
                    session, stats, stats.level, settings
                )

                add_objs = [interaction.guild.get_role(rid) for rid in roles_to_add if interaction.guild.get_role(rid)]
                remove_objs = [interaction.guild.get_role(rid) for rid in roles_to_remove if interaction.guild.get_role(rid)]

                remove_objs = [r for r in remove_objs if r in member.roles]
                add_objs = [r for r in add_objs if r not in member.roles]

                changes_made = False
                added_names = []
                removed_names = []

                if remove_objs:
                    try:
                        await member.remove_roles(*remove_objs, reason="Journey Path Repair: Remove unearned or obsolete rank roles")
                        roles_removed_total += len(remove_objs)
                        removed_names = [r.name for r in remove_objs]
                        changes_made = True
                    except discord.Forbidden:
                        pass

                if add_objs:
                    try:
                        await member.add_roles(*add_objs, reason="Journey Path Repair: Award missing path/rank roles")
                        roles_added_total += len(add_objs)
                        added_names = [r.name for r in add_objs]
                        changes_made = True
                    except discord.Forbidden:
                        pass

                if changes_made:
                    members_repaired += 1
                    detail_parts = []
                    if added_names:
                        detail_parts.append(f"➕ Added: {', '.join(added_names)}")
                    if removed_names:
                        detail_parts.append(f"➖ Removed: {', '.join(removed_names)}")
                    repaired_summary.append(f"• **{member.display_name}** (Lvl {stats.level}): {' | '.join(detail_parts)}")

            embed = discord.Embed(
                title="⚙️ Master Path & Rank Rewards Repair Audit",
                color=discord.Color.green() if members_repaired > 0 else discord.Color.blue()
            )
            embed.add_field(name="👥 Members Scanned", value=f"`{members_scanned}`", inline=True)
            embed.add_field(name="🔧 Members Repaired", value=f"`{members_repaired}`", inline=True)
            embed.add_field(name="✨ Total Roles Given / Stripped", value=f"➕ `{roles_added_total}` / ➖ `{roles_removed_total}`", inline=True)

            if repaired_summary:
                summary_text = "\n".join(repaired_summary[:15])
                if len(repaired_summary) > 15:
                    summary_text += f"\n*...and {len(repaired_summary) - 15} more members.*"
                embed.add_field(name="📋 Repair Details", value=summary_text, inline=False)
            else:
                embed.description = "✅ All member Master Path roles and rank level rewards are already up to date! No breakage found."

            await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------------
    # Admin Rank Rewards Management Commands
    # -------------------------------------------------------------------------
    @rank_group.command(name="add", description="[Admin Only] Adds a new rank tier level reward to a path.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        path="Target Path.",
        level="Required leveling milestone.",
        role="Associated Discord role.",
        name="Name of this rank tier."
    )
    @app_commands.autocomplete(path=_path_autocomplete)
    async def rank_add_command(
        self,
        interaction: discord.Interaction,
        path: str,
        level: int,
        role: discord.Role,
        name: str | None = None
    ) -> None:
        """Registers a rank progression reward milestone."""
        if level < 1:
            await interaction.response.send_message("Milestone level must be 1 or higher.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        member_obj = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member_obj or not (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_guild or member_obj.guild_permissions.manage_roles):
            await interaction.followup.send("❌ Only Server Administrators and Staff with 'Manage Server' or 'Manage Roles' permission can run this command.", ephemeral=True)
            return

        rank_name = name or role.name
        
        async with get_db_session() as session:
            guild = await DatabaseService.get_or_create_guild(session, guild_id)
            settings = guild.settings

            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path:
                await interaction.followup.send(f"❌ Master Path '{path}' not found.", ephemeral=True)
                return
                
            # Verify if level or role duplicated in path
            dup_res = await session.execute(
                select(PathRank)
                .filter_by(path_id=target_path.id)
                .filter((PathRank.required_level == level) | (PathRank.discord_role_id == role.id))
            )
            if dup_res.scalars().first():
                await interaction.followup.send("❌ A rank with that level milestone or role already exists in this path.", ephemeral=True)
                return
                
            rank = PathRank(
                path_id=target_path.id,
                required_level=level,
                discord_role_id=role.id,
                display_name=rank_name
            )
            session.add(rank)
            await session.flush()
            
            # Retroactively apply new rank/role to all users currently at or above the milestone level
            users_res = await session.execute(
                select(UserGuildStats)
                .filter_by(guild_id=guild_id, master_path_id=target_path.id)
                .filter(UserGuildStats.level >= level)
            )
            affected_users = list(users_res.scalars())
            
            for stats in affected_users:
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
                            await member_obj.remove_roles(*remove_objs, reason="Retroactive Rank Reward Update")
                        except discord.Forbidden:
                            pass
                            
                    add_objs = [r for r in add_objs if r not in member_obj.roles]
                    if add_objs:
                        try:
                            await member_obj.add_roles(*add_objs, reason="Retroactive Rank Reward Update")
                        except discord.Forbidden:
                            pass
                            
        await interaction.followup.send(f"✅ Added rank reward **{rank_name}** (Level {level}) to path {target_path.name} and updated affected members.", ephemeral=True)

    @rank_group.command(name="remove", description="[Admin Only] Removes a rank level reward from a path.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(path="Path name.", level="Milestone level to remove.")
    @app_commands.autocomplete(path=_path_autocomplete)
    async def rank_remove_command(self, interaction: discord.Interaction, path: str, level: int) -> None:
        """Removes a rank progression reward milestone."""
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            target_path = await PathService.get_path_by_name(session, guild_id, path)
            if not target_path:
                await interaction.followup.send(f"❌ Master Path '{path}' not found.", ephemeral=True)
                return
                
            del_res = await session.execute(
                select(PathRank).filter_by(path_id=target_path.id, required_level=level)
            )
            rank = del_res.scalar_one_or_none()
            if not rank:
                await interaction.followup.send(f"❌ Rank for Level {level} not found in this path.", ephemeral=True)
                return
                
            await session.delete(rank)
            await session.flush()
            
        await interaction.followup.send(f"✅ Removed Level {level} rank reward from path {target_path.name}.", ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Cog-level error handler for slash commands."""
        logger.error(f"Error in Paths command: {error}", exc_info=error)
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
    await bot.add_cog(Paths(bot))
