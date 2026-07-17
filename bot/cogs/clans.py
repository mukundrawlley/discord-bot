import discord
from discord.ext import commands
from discord import app_commands
import logging
from datetime import datetime, timezone
import io
from sqlalchemy.future import select
from sqlalchemy import delete, update
from sqlalchemy.orm import selectinload

from bot.database.connection import get_db_session
from bot.models.clan import (
    Clan,
    ClanMember,
    ClanRole,
    ClanRolePermission,
    ClanAuditLog,
    ClanApplication,
    ClanInvite,
    ClanSettings
)
from bot.services.database_service import DatabaseService

logger = logging.getLogger("Journey.Clans")

# ==============================================================================
# HELPERS
# ==============================================================================

async def get_member_membership(session, guild_id: int, user_id: int) -> ClanMember | None:
    """Fetches a member's clan membership, including their role and permissions."""
    result = await session.execute(
        select(ClanMember)
        .options(
            selectinload(ClanMember.role).selectinload(ClanRole.permissions),
            selectinload(ClanMember.clan)
        )
        .filter_by(guild_id=guild_id, user_id=user_id)
    )
    return result.scalar_one_or_none()

async def write_audit_log(session, clan_id: int, actor_id: int, action: str, old_val: str | None = None, new_val: str | None = None) -> None:
    """Helper to log actions in the database."""
    log = ClanAuditLog(
        clan_id=clan_id,
        actor_id=actor_id,
        action=action,
        old_value=old_val,
        new_value=new_val
    )
    session.add(log)
    await session.flush()

async def sync_discord_roles(guild: discord.Guild, member_id: int, correct_role_id: int | None, clan_roles: list[ClanRole]) -> None:
    """Synchronizes a member's Discord roles to match their database clan role."""
    member = guild.get_member(member_id)
    if not member:
        return
        
    # Get all discord role objects associated with the clan's roles
    clan_discord_roles = []
    correct_discord_role = None
    
    for r in clan_roles:
        if not r.discord_role_id:
            continue
        d_role = guild.get_role(r.discord_role_id)
        if d_role:
            clan_discord_roles.append(d_role)
            if r.id == correct_role_id:
                correct_discord_role = d_role
                
    # Add correct role, remove incorrect roles
    roles_to_remove = [r for r in clan_discord_roles if r in member.roles and r != correct_discord_role]
    roles_to_add = [correct_discord_role] if correct_discord_role and correct_discord_role not in member.roles else []
    
    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove, reason="Journey Clan Role Sync")
        except discord.Forbidden:
            logger.warning(f"Missing permissions to remove roles from {member.display_name}")
    if roles_to_add:
        try:
            await member.add_roles(*roles_to_add, reason="Journey Clan Role Sync")
        except discord.Forbidden:
            logger.warning(f"Missing permissions to add role to {member.display_name}")

def parse_color(hex_str: str) -> discord.Color:
    """Safely converts hex string to discord.Color."""
    try:
        hex_clean = hex_str.strip("#").strip()
        return discord.Color(int(hex_clean, 16))
    except Exception:
        return discord.Color.default()

# ==============================================================================
# VIEWS & COMPONENT INTERFACES
# ==============================================================================

class JoinConfirmView(discord.ui.View):
    def __init__(self, target_member: discord.Member, clan_id: int, clan_name: str, invite_id: int | None = None):
        super().__init__(timeout=60)
        self.target_member = target_member
        self.clan_id = clan_id
        self.clan_name = clan_name
        self.invite_id = invite_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_member.id:
            await interaction.response.send_message("❌ This invitation is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Join Clan", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db_session() as session:
            # Verify they are not in a clan already
            existing = await get_member_membership(session, interaction.guild_id, self.target_member.id)
            if existing:
                await interaction.response.send_message("❌ You are already in a clan! Leave your current clan first.", ephemeral=True)
                return
                
            # Verify clan exists
            clan_result = await session.execute(select(Clan).filter_by(id=self.clan_id))
            clan = clan_result.scalar_one_or_none()
            if not clan:
                await interaction.response.send_message("❌ This clan no longer exists.", ephemeral=True)
                return
                
            # Fetch the recruit/lowest hierarchy role
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=self.clan_id).order_by(ClanRole.hierarchy_level.asc())
            )
            roles = list(roles_result.scalars())
            # Find the lowest system role or standard role
            recruit_role = roles[0] if roles else None
            
            # Check role limits
            if recruit_role and recruit_role.max_members is not None:
                members_count = await session.execute(
                    select(ClanMember).filter_by(clan_id=self.clan_id, role_id=recruit_role.id)
                )
                if len(list(members_count.scalars())) >= recruit_role.max_members:
                    await interaction.response.send_message("❌ The entry rank of this clan has reached its member limit.", ephemeral=True)
                    return

            # Add to clan
            membership = ClanMember(
                guild_id=interaction.guild_id,
                user_id=self.target_member.id,
                clan_id=self.clan_id,
                role_id=recruit_role.id if recruit_role else None
            )
            session.add(membership)
            
            # Update invite status if applicable
            if self.invite_id:
                await session.execute(
                    update(ClanInvite).filter_by(id=self.invite_id).values(status="accepted")
                )
                
            await session.commit()
            
            # Synchronize roles
            if recruit_role:
                await sync_discord_roles(interaction.guild, self.target_member.id, recruit_role.id, roles)
                
        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"✅ **{self.target_member.display_name}** has joined the clan **{self.clan_name}**!", view=self)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db_session() as session:
            if self.invite_id:
                await session.execute(
                    update(ClanInvite).filter_by(id=self.invite_id).values(status="declined")
                )
                await session.commit()
                
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"❌ Invitation to **{self.clan_name}** was declined.", view=self)
        self.stop()


class RoleCreateModal(discord.ui.Modal, title="Create Clan Role"):
    role_name = discord.ui.TextInput(label="Role Name", placeholder="e.g. Commander", max_length=64)
    color = discord.ui.TextInput(label="Hex Color (Optional)", placeholder="e.g. #FFCC00", required=False, max_length=7)
    limit = discord.ui.TextInput(label="Max Member Limit (Optional)", placeholder="e.g. 5 (0 or leave blank for unlimited)", required=False)

    def __init__(self, clan_id: int):
        super().__init__()
        self.clan_id = clan_id

    async def on_submit(self, interaction: discord.Interaction):
        name = self.role_name.value.strip()
        color_val = self.color.value.strip() or None
        
        limit_val = None
        if self.limit.value:
            try:
                limit_val = int(self.limit.value)
                if limit_val <= 0:
                    limit_val = None
            except ValueError:
                pass
                
        async with get_db_session() as session:
            # Check unique names
            name_check = await session.execute(
                select(ClanRole).filter_by(clan_id=self.clan_id).filter(ClanRole.role_name.ilike(name))
            )
            if name_check.scalar_one_or_none():
                await interaction.response.send_message("❌ A role with that name already exists in this clan.", ephemeral=True)
                return
                
            # Fetch all roles to calculate hierarchy level
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=self.clan_id).order_by(ClanRole.hierarchy_level.desc())
            )
            roles = list(roles_result.scalars())
            
            # The new role goes just below the Leader (hierarchy 100)
            # Find a level between Leader and the next highest rank
            leader_level = 100
            next_highest_level = 1
            
            for r in roles:
                if r.hierarchy_level < leader_level:
                    next_highest_level = r.hierarchy_level
                    break
                    
            new_level = (leader_level + next_highest_level) // 2
            if new_level == next_highest_level or new_level == leader_level:
                # If there's no room, shift all intermediate roles down
                new_level = leader_level - 1
                for r in roles:
                    if r.hierarchy_level < leader_level:
                        r.hierarchy_level -= 1
                await session.flush()

            # Create Discord role
            d_color = parse_color(color_val) if color_val else discord.Color.default()
            try:
                discord_role = await interaction.guild.create_role(
                    name=name,
                    color=d_color,
                    reason=f"Journey Clan Role Creation"
                )
            except discord.Forbidden:
                await interaction.response.send_message("❌ Journey Bot lacks 'Manage Roles' permissions on Discord to build this rank.", ephemeral=True)
                return

            # Add role to DB
            role = ClanRole(
                clan_id=self.clan_id,
                discord_role_id=discord_role.id,
                role_name=name,
                color=color_val,
                hierarchy_level=new_level,
                max_members=limit_val
            )
            session.add(role)
            await session.flush()
            
            # Initialize empty permissions
            from bot.models.clan import create_default_permissions
            perms = await create_default_permissions(session, role.id, is_leader=False)
            
            # Log action
            await write_audit_log(session, self.clan_id, interaction.user.id, "role_created", None, name)
            await session.commit()
            
        await interaction.response.send_message(f"✅ Created role **{name}** successfully!", ephemeral=True)


class RoleEditModal(discord.ui.Modal, title="Edit Clan Role"):
    role_name = discord.ui.TextInput(label="Role Name", placeholder="e.g. Commander", max_length=64)
    color = discord.ui.TextInput(label="Hex Color (Optional)", placeholder="e.g. #FFCC00", required=False, max_length=7)
    limit = discord.ui.TextInput(label="Max Member Limit (Optional)", placeholder="e.g. 5 (0 or leave blank for unlimited)", required=False)

    def __init__(self, role: ClanRole):
        super().__init__()
        self.role = role
        self.role_name.default = role.role_name
        self.color.default = role.color or ""
        self.limit.default = str(role.max_members) if role.max_members else ""

    async def on_submit(self, interaction: discord.Interaction):
        name = self.role_name.value.strip()
        color_val = self.color.value.strip() or None
        
        limit_val = None
        if self.limit.value:
            try:
                limit_val = int(self.limit.value)
                if limit_val <= 0:
                    limit_val = None
            except ValueError:
                pass
                
        async with get_db_session() as session:
            # Check unique names excluding self
            name_check = await session.execute(
                select(ClanRole)
                .filter_by(clan_id=self.role.clan_id)
                .filter(ClanRole.role_name.ilike(name))
                .filter(ClanRole.id != self.role.id)
            )
            if name_check.scalar_one_or_none():
                await interaction.response.send_message("❌ A role with that name already exists.", ephemeral=True)
                return
                
            # Load from DB to prevent detached state
            role_result = await session.execute(select(ClanRole).filter_by(id=self.role.id))
            db_role = role_result.scalar_one()
            
            old_name = db_role.role_name
            db_role.role_name = name
            db_role.color = color_val
            db_role.max_members = limit_val
            
            # Sync Discord role properties
            if db_role.discord_role_id:
                d_role = interaction.guild.get_role(db_role.discord_role_id)
                if d_role:
                    d_color = parse_color(color_val) if color_val else discord.Color.default()
                    try:
                        await d_role.edit(name=name, color=d_color, reason="Journey Clan Role Modification")
                    except discord.Forbidden:
                        pass
                        
            # Log action
            await write_audit_log(
                session, 
                db_role.clan_id, 
                interaction.user.id, 
                "role_modified", 
                f"Name: {old_name}", 
                f"Name: {name}, Color: {color_val}, Limit: {limit_val}"
            )
            await session.commit()
            
        await interaction.response.send_message(f"✅ Modified role **{name}** successfully!", ephemeral=True)


class RoleManagerView(discord.ui.View):
    def __init__(self, leader_id: int, clan_id: int, roles: list[ClanRole]):
        super().__init__(timeout=120)
        self.leader_id = leader_id
        self.clan_id = clan_id
        self.roles = roles
        self.selected_role_id = None
        self.update_select_menu()

    def update_select_menu(self):
        self.clear_items()
        
        # Select Menu to pick a role
        options = []
        for r in sorted(self.roles, key=lambda x: x.hierarchy_level, reverse=True):
            limit_str = f" [Limit: {r.max_members}]" if r.max_members else ""
            options.append(
                discord.SelectOption(
                    label=r.role_name, 
                    value=str(r.id), 
                    description=f"Hierarchy level: {r.hierarchy_level}{limit_str}"
                )
            )
            
        select_menu = discord.ui.Select(
            placeholder="Select a role to manage...",
            options=options,
            custom_id="clan_role_select"
        )
        select_menu.callback = self.select_callback
        self.add_item(select_menu)
        
        # Action Buttons
        create_btn = discord.ui.Button(label="➕ Create Next Role", style=discord.ButtonStyle.success, custom_id="clan_role_create")
        create_btn.callback = self.create_callback
        self.add_item(create_btn)
        
        edit_btn = discord.ui.Button(label="✏️ Edit Role", style=discord.ButtonStyle.primary, custom_id="clan_role_edit", disabled=self.selected_role_id is None)
        edit_btn.callback = self.edit_callback
        self.add_item(edit_btn)
        
        delete_btn = discord.ui.Button(label="🗑️ Delete Role", style=discord.ButtonStyle.danger, custom_id="clan_role_delete", disabled=self.selected_role_id is None)
        delete_btn.callback = self.delete_callback
        self.add_item(delete_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message("❌ Only the clan leader can manage roles.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        self.selected_role_id = int(interaction.data["values"][0])
        self.update_select_menu()
        
        # Find selected role
        selected_role = next(r for r in self.roles if r.id == self.selected_role_id)
        embed = discord.Embed(
            title=f"👑 Ranks Editor - {selected_role.role_name}",
            description=f"Selected Role: **{selected_role.role_name}**\nHierarchy Level: `{selected_role.hierarchy_level}`\nLimit: `{selected_role.max_members or 'Unlimited'}`\nHex Color: `{selected_role.color or 'Default'}`",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def create_callback(self, interaction: discord.Interaction):
        modal = RoleCreateModal(self.clan_id)
        await interaction.response.send_modal(modal)

    async def edit_callback(self, interaction: discord.Interaction):
        selected_role = next(r for r in self.roles if r.id == self.selected_role_id)
        if selected_role.is_system_role and selected_role.hierarchy_level == 100:
            await interaction.response.send_message("❌ The Leader system role metadata cannot be modified.", ephemeral=True)
            return
            
        modal = RoleEditModal(selected_role)
        await interaction.response.send_modal(modal)

    async def delete_callback(self, interaction: discord.Interaction):
        selected_role = next(r for r in self.roles if r.id == self.selected_role_id)
        if selected_role.is_system_role:
            await interaction.response.send_message("❌ You cannot delete system roles (Leader / entry-rank roles).", ephemeral=True)
            return
            
        async with get_db_session() as session:
            # Ensure no members are assigned to this role
            members_check = await session.execute(
                select(ClanMember).filter_by(role_id=self.selected_role_id)
            )
            if list(members_check.scalars()):
                await interaction.response.send_message("❌ You cannot delete a role while members are assigned to it. Demote or promote them first.", ephemeral=True)
                return
                
            # Delete Discord Role
            if selected_role.discord_role_id:
                d_role = interaction.guild.get_role(selected_role.discord_role_id)
                if d_role:
                    try:
                        await d_role.delete(reason="Journey Clan Role Deletion")
                    except discord.Forbidden:
                        pass
                        
            # Delete DB Record
            await session.execute(delete(ClanRole).filter_by(id=self.selected_role_id))
            
            # Log action
            await write_audit_log(session, self.clan_id, interaction.user.id, "role_deleted", selected_role.role_name, None)
            await session.commit()
            
        self.roles.remove(selected_role)
        self.selected_role_id = None
        self.update_select_menu()
        
        embed = discord.Embed(
            title="👑 Ranks Editor",
            description="Role deleted successfully. Select another role to manage.",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=self)


class PermissionsToggleView(discord.ui.View):
    def __init__(self, leader_id: int, role: ClanRole, permissions: ClanRolePermission):
        super().__init__(timeout=120)
        self.leader_id = leader_id
        self.role = role
        self.permissions = permissions
        self.build_buttons()

    def build_buttons(self):
        self.clear_items()
        
        # Define core permissions to display as toggles
        core_perms = [
            ("can_invite", "Invite Members"),
            ("can_kick", "Kick Members"),
            ("can_accept_applications", "Accept Applications"),
            ("can_reject_applications", "Reject Applications"),
            ("can_promote", "Promote"),
            ("can_demote", "Demote"),
            ("can_edit_clan_description", "Edit Description"),
            ("can_manage_roles", "Manage Roles"),
            ("can_manage_permissions", "Manage Perms"),
            ("can_view_logs", "View logs")
        ]
        
        for attr, label in core_perms:
            is_enabled = getattr(self.permissions, attr, False)
            style = discord.ButtonStyle.success if is_enabled else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=label, style=style, custom_id=attr)
            btn.callback = self.toggle_permission
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message("❌ Only the clan leader can manage permissions.", ephemeral=True)
            return False
        return True

    async def toggle_permission(self, interaction: discord.Interaction):
        attr = interaction.data["custom_id"]
        current_val = getattr(self.permissions, attr, False)
        new_val = not current_val
        
        async with get_db_session() as session:
            # Query db to avoid detached instance issues
            perms_result = await session.execute(
                select(ClanRolePermission).filter_by(role_id=self.role.id)
            )
            db_perms = perms_result.scalar_one()
            setattr(db_perms, attr, new_val)
            
            # Log action
            await write_audit_log(
                session, 
                self.role.clan_id, 
                interaction.user.id, 
                "permission_toggled", 
                f"{attr}: {current_val}", 
                f"{attr}: {new_val}"
            )
            await session.commit()
            
        # Update local object state
        setattr(self.permissions, attr, new_val)
        self.build_buttons()
        
        embed = discord.Embed(
            title=f"🔐 Edit Permissions - {self.role.role_name}",
            description=f"Click the buttons below to toggle permissions for **{self.role.role_name}**.",
            color=discord.Color.orange()
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ApplicationsView(discord.ui.View):
    def __init__(self, clan_id: int, applications: list[ClanApplication], manager_id: int):
        super().__init__(timeout=120)
        self.clan_id = clan_id
        self.applications = applications
        self.manager_id = manager_id
        self.index = 0
        self.update_view()

    def update_view(self):
        self.clear_items()
        
        if not self.applications:
            return
            
        accept_btn = discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id="app_accept")
        accept_btn.callback = self.accept_callback
        self.add_item(accept_btn)
        
        reject_btn = discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, custom_id="app_reject")
        reject_btn.callback = self.reject_callback
        self.add_item(reject_btn)

    async def get_current_app_embed(self, interaction: discord.Interaction) -> discord.Embed:
        if self.index >= len(self.applications):
            return discord.Embed(title="📋 Applications Queue", description="No pending applications.", color=discord.Color.blue())
            
        app = self.applications[self.index]
        user = await interaction.client.fetch_user(app.user_id)
        
        embed = discord.Embed(
            title="📋 Applications Queue",
            description=f"Application **{self.index+1} / {len(self.applications)}**",
            color=discord.Color.blue()
        )
        embed.add_field(name="User", value=f"{user.mention} ({user.display_name})", inline=False)
        embed.add_field(name="User ID", value=app.user_id, inline=True)
        embed.add_field(name="Applied At", value=app.created_at.strftime("%Y-%m-%d"), inline=True)
        
        return embed

    async def accept_callback(self, interaction: discord.Interaction):
        app = self.applications[self.index]
        async with get_db_session() as session:
            # Verify target not already in a clan
            existing = await get_member_membership(session, interaction.guild_id, app.user_id)
            if existing:
                await interaction.response.send_message("❌ This user has already joined a clan.", ephemeral=True)
                return
                
            # Fetch recruit/lowest role
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=self.clan_id).order_by(ClanRole.hierarchy_level.asc())
            )
            roles = list(roles_result.scalars())
            recruit_role = roles[0] if roles else None
            
            # Check limits
            if recruit_role and recruit_role.max_members is not None:
                members_count = await session.execute(
                    select(ClanMember).filter_by(clan_id=self.clan_id, role_id=recruit_role.id)
                )
                if len(list(members_count.scalars())) >= recruit_role.max_members:
                    await interaction.response.send_message("❌ The entry rank has reached its member limit.", ephemeral=True)
                    return
                    
            # Add to clan
            membership = ClanMember(
                guild_id=interaction.guild_id,
                user_id=app.user_id,
                clan_id=self.clan_id,
                role_id=recruit_role.id if recruit_role else None
            )
            session.add(membership)
            
            # Update app status
            await session.execute(
                update(ClanApplication).filter_by(id=app.id).values(status="approved")
            )
            
            # Log action
            await write_audit_log(session, self.clan_id, interaction.user.id, "application_accepted", f"User: {app.user_id}")
            await session.commit()
            
            # Sync roles
            if recruit_role:
                await sync_discord_roles(interaction.guild, app.user_id, recruit_role.id, roles)

        await interaction.response.send_message("✅ Application accepted!", ephemeral=True)
        self.applications.pop(self.index)
        
        if self.index >= len(self.applications):
            self.index = 0
            
        self.update_view()
        embed = await self.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self)

    async def reject_callback(self, interaction: discord.Interaction):
        app = self.applications[self.index]
        async with get_db_session() as session:
            await session.execute(
                update(ClanApplication).filter_by(id=app.id).values(status="rejected")
            )
            await write_audit_log(session, self.clan_id, interaction.user.id, "application_rejected", f"User: {app.user_id}")
            await session.commit()
            
        await interaction.response.send_message("❌ Application rejected.", ephemeral=True)
        self.applications.pop(self.index)
        
        if self.index >= len(self.applications):
            self.index = 0
            
        self.update_view()
        embed = await self.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self)


class AuditLogsView(discord.ui.View):
    def __init__(self, logs: list[ClanAuditLog], page_limit: int = 5):
        super().__init__(timeout=60)
        self.logs = logs
        self.page_limit = page_limit
        self.current_page = 0
        self.total_pages = max(1, (len(logs) + page_limit - 1) // page_limit)
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        
        prev_btn = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.secondary, disabled=self.current_page == 0)
        prev_btn.callback = self.prev_page
        self.add_item(prev_btn)
        
        next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self.current_page >= self.total_pages - 1)
        next_btn.callback = self.next_page
        self.add_item(next_btn)

    def get_embed(self, client: discord.Client) -> discord.Embed:
        embed = discord.Embed(
            title="📜 Clan Audit Logs",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(timezone.utc)
        )
        
        start = self.current_page * self.page_limit
        end = start + self.page_limit
        page_logs = self.logs[start:end]
        
        if not page_logs:
            embed.description = "*No audit logs found.*"
            return embed
            
        for log in page_logs:
            actor = client.get_user(log.actor_id)
            actor_name = actor.display_name if actor else f"ID: {log.actor_id}"
            
            old_str = f"\n*Old*: {log.old_value}" if log.old_value else ""
            new_str = f"\n*New*: {log.new_value}" if log.new_value else ""
            
            time_str = log.timestamp.strftime("%Y-%m-%d %H:%M UTC")
            embed.add_field(
                name=f"⚡ {log.action.replace('_', ' ').title()}",
                value=f"**Actor**: {actor_name}{old_str}{new_str}\n*Time*: {time_str}",
                inline=False
            )
            
        embed.set_footer(text=f"Page {self.current_page + 1} / {self.total_pages}")
        return embed

    async def prev_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(interaction.client), view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(interaction.client), view=self)

# ==============================================================================
# CLAN SLASH COMMANDS COG
# ==============================================================================

@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
class ClanGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="clan", description="MMORPG Dynamic Clan Hierarchy Systems.")

    @app_commands.command(name="create", description="Creates a new clan (requires Staff approval).")
    @app_commands.describe(
        name="The name of your new clan.",
        description="A short description for your clan."
    )
    async def clan_create(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str | None = None
    ) -> None:
        """Creates a new clan in unapproved state."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        if len(name) > 64:
            await interaction.response.send_message("❌ Clan name cannot exceed 64 characters.", ephemeral=True)
            return
        if description and len(description) > 256:
            await interaction.response.send_message("❌ Description cannot exceed 256 characters.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        
        async with get_db_session() as session:
            # Verify target not already in a clan
            existing = await get_member_membership(session, guild_id, user_id)
            if existing:
                await interaction.response.send_message("❌ You are already in a clan! Leave your current clan first.", ephemeral=True)
                return
                
            # Verify name unique
            name_check = await session.execute(
                select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(name))
            )
            if name_check.scalar_one_or_none():
                await interaction.response.send_message("❌ A clan with that name already exists in this server.", ephemeral=True)
                return
                
            # Create unapproved clan
            clan = Clan(
                guild_id=guild_id,
                owner_id=user_id,
                name=name,
                description=description,
                approved=False
            )
            session.add(clan)
            await session.flush()
            
            # Setup default settings
            settings = ClanSettings(clan_id=clan.id)
            session.add(settings)
            
            await session.commit()
            
        await interaction.response.send_message(f"🎉 Clan **{name}** has been registered! It is now pending **Staff Approval** before roles are initialized.")

    @app_commands.command(name="approve", description="Approves a pending clan (Staff Only).")
    @app_commands.describe(name="The name of the clan to approve.")
    async def clan_approve(self, interaction: discord.Interaction, name: str) -> None:
        """Staff command to approve a clan and initialize dynamic roles."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        # Verify Staff permissions
        perms = interaction.user.guild_permissions
        is_staff = perms.administrator or perms.manage_guild or perms.manage_roles or (interaction.guild.owner_id == interaction.user.id)
        if not is_staff:
            await interaction.response.send_message("❌ Only server administrators or staff can approve clans.", ephemeral=True)
            return

        async with get_db_session() as session:
            clan_result = await session.execute(
                select(Clan).filter_by(guild_id=interaction.guild_id).filter(Clan.name.ilike(name))
            )
            clan = clan_result.scalar_one_or_none()
            if not clan:
                await interaction.response.send_message(f"❌ No clan found with name '{name}'.", ephemeral=True)
                return
                
            if clan.approved:
                await interaction.response.send_message("❌ This clan has already been approved.", ephemeral=True)
                return
                
            # Create dynamic Leader and Member roles on Discord
            try:
                leader_d_role = await interaction.guild.create_role(
                    name=f"{clan.name} Leader",
                    color=discord.Color.gold(),
                    reason=f"Journey Clan Approval: Initialize Leader role."
                )
                member_d_role = await interaction.guild.create_role(
                    name=f"{clan.name} Member",
                    color=discord.Color.blue(),
                    reason=f"Journey Clan Approval: Initialize Member role."
                )
            except discord.Forbidden:
                await interaction.response.send_message("❌ Journey Bot lacks 'Manage Roles' permission on Discord to create the clan ranks.", ephemeral=True)
                return
                
            # Write roles to DB
            leader_role = ClanRole(
                clan_id=clan.id,
                discord_role_id=leader_d_role.id,
                role_name="Leader",
                color="#FFD700",
                hierarchy_level=100,
                max_members=1,
                is_system_role=True
            )
            member_role = ClanRole(
                clan_id=clan.id,
                discord_role_id=member_d_role.id,
                role_name="Member",
                color="#3498DB",
                hierarchy_level=1,
                is_system_role=True
            )
            session.add_all([leader_role, member_role])
            await session.flush()
            
            # Setup default permissions via the centralized helper
            from bot.models.clan import create_default_permissions
            leader_perms = await create_default_permissions(session, leader_role.id, is_leader=True)
            member_perms = await create_default_permissions(session, member_role.id, is_leader=False)
            
            # Add clan owner to Leader role
            membership = ClanMember(
                guild_id=interaction.guild_id,
                user_id=clan.owner_id,
                clan_id=clan.id,
                role_id=leader_role.id
            )
            session.add(membership)
            
            # Update approval state
            clan.approved = True
            clan.approved_by = interaction.user.id
            clan.approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
            
            # Log action
            await write_audit_log(session, clan.id, interaction.user.id, "clan_approved")
            await session.commit()
            
            # Assign Discord leader role to owner
            owner = interaction.guild.get_member(clan.owner_id)
            if owner:
                try:
                    await owner.add_roles(leader_d_role, reason="Journey Clan Owner Initial Assignment")
                except discord.Forbidden:
                    pass
                    
        await interaction.response.send_message(f"✅ Clan **{clan.name}** has been approved! Ranks have been initialized and visual roles registered in Discord.")

    @app_commands.command(name="role", description="Manages clan roles/hierarchy (Leader only).")
    async def clan_role(self, interaction: discord.Interaction) -> None:
        """Interactive ranks management dashboard."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        async with get_db_session() as session:
            membership = await get_member_membership(session, interaction.guild_id, interaction.user.id)
            if not membership or membership.clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan Leader can manage roles.", ephemeral=True)
                return
                
            # Fetch roles
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=membership.clan_id)
            )
            roles = list(roles_result.scalars())
            
        embed = discord.Embed(
            title="👑 Ranks Editor",
            description="Use the select menu to inspect a role, or click the action buttons to create/edit your clan ranks.",
            color=discord.Color.blue()
        )
        
        view = RoleManagerView(interaction.user.id, membership.clan_id, roles)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="permissions", description="Configures permissions for a specific role (Leader only).")
    @app_commands.describe(role_name="The name of the role to customize.")
    async def clan_permissions(self, interaction: discord.Interaction, role_name: str) -> None:
        """Toggles clan authority permissions per rank."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        async with get_db_session() as session:
            membership = await get_member_membership(session, interaction.guild_id, interaction.user.id)
            if not membership or membership.clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan Leader can configure permissions.", ephemeral=True)
                return
                
            role_result = await session.execute(
                select(ClanRole)
                .options(selectinload(ClanRole.permissions))
                .filter_by(clan_id=membership.clan_id)
                .filter(ClanRole.role_name.ilike(role_name))
            )
            role = role_result.scalar_one_or_none()
            if not role:
                await interaction.response.send_message(f"❌ Role '{role_name}' not found.", ephemeral=True)
                return
                
            if role.is_system_role and role.hierarchy_level == 100:
                await interaction.response.send_message("❌ The Leader system role permissions cannot be edited.", ephemeral=True)
                return
                
            permissions = role.permissions

        embed = discord.Embed(
            title=f"🔐 Edit Permissions - {role.role_name}",
            description=f"Click the buttons below to toggle permissions for **{role.role_name}**.",
            color=discord.Color.orange()
        )
        
        view = PermissionsToggleView(interaction.user.id, role, permissions)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="promote", description="Promotes a member to the next rank in the hierarchy.")
    @app_commands.describe(member="The member to promote.")
    async def clan_promote(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Promotes a user inside the clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot promote yourself.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            # Fetch executor and target memberships
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_promote = exec_member.role.permissions.can_promote if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_promote:
                await interaction.response.send_message("❌ You lack permission to promote members.", ephemeral=True)
                return
                
            target_member = await get_member_membership(session, guild_id, member.id)
            if not target_member or target_member.clan_id != exec_member.clan_id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            # Fetch all roles sorted by hierarchy
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=exec_member.clan_id).order_by(ClanRole.hierarchy_level.asc())
            )
            roles = list(roles_result.scalars())
            
            current_role_idx = next(i for i, r in enumerate(roles) if r.id == target_member.role_id)
            if current_role_idx == len(roles) - 1:
                await interaction.response.send_message("❌ Target member is already at the highest possible rank.", ephemeral=True)
                return
                
            next_role = roles[current_role_idx + 1]
            
            # Check hierarchy boundaries
            if not is_leader:
                if exec_member.role.hierarchy_level <= target_member.role.hierarchy_level:
                    await interaction.response.send_message("❌ Your rank must be higher than the target's rank.", ephemeral=True)
                    return
                if exec_member.role.hierarchy_level <= next_role.hierarchy_level:
                    await interaction.response.send_message("❌ You cannot promote someone to a rank equal to or higher than yours.", ephemeral=True)
                    return

            # Check role limits
            if next_role.max_members is not None:
                members_count = await session.execute(
                    select(ClanMember).filter_by(clan_id=exec_member.clan_id, role_id=next_role.id)
                )
                if len(list(members_count.scalars())) >= next_role.max_members:
                    await interaction.response.send_message("❌ This rank has reached its maximum member limit.", ephemeral=True)
                    return

            # Execute promotion
            old_role_name = target_member.role.role_name
            target_member.role_id = next_role.id
            
            # Log action
            await write_audit_log(
                session, 
                exec_member.clan_id, 
                interaction.user.id, 
                "member_promoted", 
                f"Member: {member.id}, Role: {old_role_name}", 
                f"Role: {next_role.role_name}"
            )
            await session.commit()
            
            # Synchronize roles
            await sync_discord_roles(interaction.guild, member.id, next_role.id, roles)
            
        await interaction.response.send_message(f"📈 **{member.display_name}** has been promoted to **{next_role.role_name}**!")

    @app_commands.command(name="demote", description="Demotes a member to the previous rank in the hierarchy.")
    @app_commands.describe(member="The member to demote.")
    async def clan_demote(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Demotes a user inside the clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot demote yourself.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_demote = exec_member.role.permissions.can_demote if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_demote:
                await interaction.response.send_message("❌ You lack permission to demote members.", ephemeral=True)
                return
                
            target_member = await get_member_membership(session, guild_id, member.id)
            if not target_member or target_member.clan_id != exec_member.clan_id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            # Leader immunity
            if target_member.clan.owner_id == member.id:
                await interaction.response.send_message("❌ You cannot demote the clan Leader.", ephemeral=True)
                return
                
            # Fetch roles sorted by hierarchy
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=exec_member.clan_id).order_by(ClanRole.hierarchy_level.asc())
            )
            roles = list(roles_result.scalars())
            
            current_role_idx = next(i for i, r in enumerate(roles) if r.id == target_member.role_id)
            if current_role_idx == 0:
                await interaction.response.send_message("❌ Target member is already at the lowest possible rank.", ephemeral=True)
                return
                
            prev_role = roles[current_role_idx - 1]
            
            # Check hierarchy boundaries
            if not is_leader:
                if exec_member.role.hierarchy_level <= target_member.role.hierarchy_level:
                    await interaction.response.send_message("❌ Your rank must be higher than the target's rank.", ephemeral=True)
                    return

            # Execute demotion
            old_role_name = target_member.role.role_name
            target_member.role_id = prev_role.id
            
            # Log action
            await write_audit_log(
                session, 
                exec_member.clan_id, 
                interaction.user.id, 
                "member_demoted", 
                f"Member: {member.id}, Role: {old_role_name}", 
                f"Role: {prev_role.role_name}"
            )
            await session.commit()
            
            # Synchronize roles
            await sync_discord_roles(interaction.guild, member.id, prev_role.id, roles)
            
        await interaction.response.send_message(f"📉 **{member.display_name}** has been demoted to **{prev_role.role_name}**.")

    @app_commands.command(name="invite", description="Invites a member to join your clan.")
    @app_commands.describe(member="The member you want to invite.")
    async def clan_invite(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Invites a user to the clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        if member.bot:
            await interaction.response.send_message("❌ You cannot invite bots to a clan.", ephemeral=True)
            return
            
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You are already in your clan.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_invite = exec_member.role.permissions.can_invite if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_invite:
                await interaction.response.send_message("❌ You lack permission to invite members.", ephemeral=True)
                return
                
            # Verify target not already in a clan
            target_check = await get_member_membership(session, guild_id, member.id)
            if target_check:
                await interaction.response.send_message("❌ That member is already in a clan.", ephemeral=True)
                return
                
            # Create Invite record
            invite = ClanInvite(
                clan_id=exec_member.clan_id,
                user_id=member.id,
                invited_by=interaction.user.id
            )
            session.add(invite)
            await session.flush()
            
            invite_id = invite.id
            clan_id = exec_member.clan_id
            clan_name = exec_member.clan.name

        view = JoinConfirmView(target_member=member, clan_id=clan_id, clan_name=clan_name, invite_id=invite_id)
        await interaction.response.send_message(
            content=f"✉️ {member.mention}, you have been invited to join the clan **{clan_name}** by **{interaction.user.display_name}**!",
            view=view
        )

    @app_commands.command(name="apply", description="Applies to join an invite-only clan.")
    @app_commands.describe(clan_name="The name of the clan to apply to.")
    async def clan_apply(self, interaction: discord.Interaction, clan_name: str) -> None:
        """Submits a membership application to a clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            # Verify not in a clan
            existing = await get_member_membership(session, guild_id, interaction.user.id)
            if existing:
                await interaction.response.send_message("❌ You are already in a clan.", ephemeral=True)
                return
                
            # Fetch clan
            clan_result = await session.execute(
                select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(clan_name))
            )
            clan = clan_result.scalar_one_or_none()
            if not clan:
                await interaction.response.send_message(f"❌ Clan '{clan_name}' not found.", ephemeral=True)
                return
                
            # Create application
            app = ClanApplication(
                clan_id=clan.id,
                user_id=interaction.user.id
            )
            session.add(app)
            await session.commit()
            
        await interaction.response.send_message(f"✅ Your application to join **{clan.name}** has been submitted!")

    @app_commands.command(name="applications", description="View and manage pending applications (Officers with permission only).")
    async def clan_applications(self, interaction: discord.Interaction) -> None:
        """View applications queue."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_accept = exec_member.role.permissions.can_accept_applications if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_accept:
                await interaction.response.send_message("❌ You lack permission to manage applications.", ephemeral=True)
                return
                
            # Fetch pending applications
            apps_result = await session.execute(
                select(ClanApplication).filter_by(clan_id=exec_member.clan_id, status="pending").order_by(ClanApplication.created_at.asc())
            )
            applications = list(apps_result.scalars())
            
            clan_id = exec_member.clan_id

        if not applications:
            await interaction.response.send_message("📋 There are no pending applications for your clan.", ephemeral=True)
            return
            
        view = ApplicationsView(clan_id, applications, interaction.user.id)
        embed = await view.get_current_app_embed(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="logs", description="Views the clan's audit log history.")
    async def clan_logs(self, interaction: discord.Interaction) -> None:
        """Displays audit log history."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_view = exec_member.role.permissions.can_view_logs if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_view:
                await interaction.response.send_message("❌ You lack permission to view clan logs.", ephemeral=True)
                return
                
            # Fetch audit logs
            logs_result = await session.execute(
                select(ClanAuditLog).filter_by(clan_id=exec_member.clan_id).order_by(ClanAuditLog.timestamp.desc())
            )
            logs = list(logs_result.scalars())

        view = AuditLogsView(logs)
        await interaction.response.send_message(embed=view.get_embed(interaction.client), view=view, ephemeral=True)

    @app_commands.command(name="info", description="Displays details about a clan.")
    @app_commands.describe(target="The clan name or user mention/ID to query (leave blank for yours).")
    async def clan_info(
        self,
        interaction: discord.Interaction,
        target: str | None = None
    ) -> None:
        """Shows details about a specific clan, member's clan, or the caller's clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            clan = None
            if target is None:
                # Fetch caller stats and their clan
                exec_member = await get_member_membership(session, guild_id, interaction.user.id)
                if exec_member:
                    clan = exec_member.clan
                else:
                    await interaction.response.send_message("❌ You are not currently in a clan. Use `/clan create` to register one!", ephemeral=True)
                    return
            else:
                # Check user mention/id first
                user_id = None
                if target.startswith("<@") and target.endswith(">"):
                    try:
                        user_id = int(target.strip("<@!>"))
                    except ValueError:
                        pass
                else:
                    try:
                        user_id = int(target)
                    except ValueError:
                        pass
                
                if user_id:
                    target_member = await get_member_membership(session, guild_id, user_id)
                    if target_member:
                        clan = target_member.clan
                    else:
                        await interaction.response.send_message("❌ That user is not in a clan.", ephemeral=True)
                        return
                else:
                    # Search by name
                    clan_result = await session.execute(
                        select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(target))
                    )
                    clan = clan_result.scalar_one_or_none()
                    if not clan:
                        await interaction.response.send_message(f"❌ No clan found with name '{target}'.", ephemeral=True)
                        return

            # Eager load members sorted by hierarchy level
            members_result = await session.execute(
                select(ClanMember)
                .options(selectinload(ClanMember.role))
                .filter_by(clan_id=clan.id)
            )
            members = list(members_result.scalars())
            
            # Fetch roles to know correct sorting order
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id)
            )
            clan_roles = list(roles_result.scalars())
            
        # Resolve leader name
        leader_member = interaction.guild.get_member(clan.owner_id) if interaction.guild else None
        if not leader_member:
            try:
                leader_member = await interaction.client.fetch_user(clan.owner_id)
            except Exception:
                pass
        leader_name = leader_member.display_name if leader_member else f"ID: {clan.owner_id}"
        
        embed = discord.Embed(
            title=f"🛡️ Clan: {clan.name} " + ("" if clan.approved else "(Pending Approval ⏳)"),
            description=clan.description or "*No description set.*",
            color=discord.Color.blue()
        )
        embed.add_field(name="👑 Leader", value=f"<@{clan.owner_id}> ({leader_name})", inline=True)
        embed.add_field(name="📅 Created", value=clan.created_at.strftime("%Y-%m-%d"), inline=True)
        
        # Sort members by hierarchy level descending
        members.sort(key=lambda m: m.role.hierarchy_level if m.role else 0, reverse=True)
        
        members_list = []
        for idx, m in enumerate(members):
            member_obj = interaction.guild.get_member(m.user_id) if interaction.guild else None
            if not member_obj:
                try:
                    member_obj = await interaction.client.fetch_user(m.user_id)
                except Exception:
                    pass
            name = member_obj.display_name if member_obj else f"User {m.user_id}"
            
            role_suffix = ""
            if m.role:
                if m.role.hierarchy_level == 100:
                    role_suffix = " (Leader) 👑"
                else:
                    role_suffix = f" ({m.role.role_name})"
                    
            members_list.append(f"{idx+1}. <@{m.user_id}> ({name}){role_suffix}")
            
        members_str = "\n".join(members_list) if members_list else "*No members.*"
        embed.add_field(name=f"👥 Members ({len(members)})", value=members_str, inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Leaves your current clan.")
    async def clan_leave(self, interaction: discord.Interaction) -> None:
        """Leaves the clan. Disbands it if owner leaves."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            membership = await get_member_membership(session, guild_id, interaction.user.id)
            if not membership:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            clan = membership.clan
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id)
            )
            clan_roles = list(roles_result.scalars())
            
            if clan.owner_id == interaction.user.id:
                # Disband clan if Leader leaves
                clan_name = clan.name
                # Get all members to strip roles
                members_result = await session.execute(
                    select(ClanMember).filter_by(clan_id=clan.id)
                )
                members_list = list(members_result.scalars())
                
                await session.execute(delete(ClanMember).filter_by(clan_id=clan.id))
                await session.execute(delete(Clan).filter_by(id=clan.id))
                await session.commit()
                
                # Delete discord roles and clear member caches
                for r in clan_roles:
                    if r.discord_role_id:
                        d_role = interaction.guild.get_role(r.discord_role_id)
                        if d_role:
                            try:
                                await d_role.delete(reason="Journey Clan Disband")
                            except discord.Forbidden:
                                pass
                                
                await interaction.response.send_message(f"💥 Clan **{clan_name}** has been disbanded because the leader left.")
            else:
                # Remove member
                await session.execute(
                    delete(ClanMember).filter_by(guild_id=guild_id, user_id=interaction.user.id)
                )
                await write_audit_log(session, clan.id, interaction.user.id, "member_left")
                await session.commit()
                
                # Sync roles to strip
                await sync_discord_roles(interaction.guild, interaction.user.id, None, clan_roles)
                await interaction.response.send_message(f"👋 You have left the clan **{clan.name}**.")

    @app_commands.command(name="kick", description="Kicks a member from your clan.")
    @app_commands.describe(member="The member to kick.")
    async def clan_kick(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Kicks a member from the clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot kick yourself.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_kick = exec_member.role.permissions.can_kick if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_kick:
                await interaction.response.send_message("❌ You lack permission to kick members.", ephemeral=True)
                return
                
            target_member = await get_member_membership(session, guild_id, member.id)
            if not target_member or target_member.clan_id != exec_member.clan_id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            # Hierarchy checks
            if not is_leader:
                if exec_member.role.hierarchy_level <= target_member.role.hierarchy_level:
                    await interaction.response.send_message("❌ Your rank must be higher than the target's rank to kick them.", ephemeral=True)
                    return
                    
            # Delete member DB record
            clan = exec_member.clan
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id)
            )
            clan_roles = list(roles_result.scalars())
            
            await session.execute(
                delete(ClanMember).filter_by(guild_id=guild_id, user_id=member.id)
            )
            
            # Log action
            await write_audit_log(session, clan.id, interaction.user.id, "member_kicked", f"Member: {member.id}")
            await session.commit()
            
            # Sync roles to strip
            await sync_discord_roles(interaction.guild, member.id, None, clan_roles)
            
        await interaction.response.send_message(f"👢 **{member.display_name}** has been kicked from the clan **{clan.name}**.")

    @app_commands.command(name="disband", description="Disbands your clan (Leader only).")
    async def clan_disband(self, interaction: discord.Interaction) -> None:
        """Disbands the clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            membership = await get_member_membership(session, guild_id, interaction.user.id)
            if not membership or membership.clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan Leader can disband the clan.", ephemeral=True)
                return
                
            clan = membership.clan
            clan_name = clan.name
            
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id)
            )
            clan_roles = list(roles_result.scalars())
            
            # Fetch all members to strip roles later
            members_result = await session.execute(
                select(ClanMember).filter_by(clan_id=clan.id)
            )
            members_list = list(members_result.scalars())
            
            await session.execute(delete(ClanMember).filter_by(clan_id=clan.id))
            await session.execute(delete(Clan).filter_by(id=clan.id))
            await session.commit()
            
            # Delete discord roles
            for r in clan_roles:
                if r.discord_role_id:
                    d_role = interaction.guild.get_role(r.discord_role_id)
                    if d_role:
                        try:
                            await d_role.delete(reason="Journey Clan Disband")
                        except discord.Forbidden:
                            pass
                            
        await interaction.response.send_message(f"💥 Clan **{clan_name}** has been successfully disbanded.")


class Clans(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(ClanGroup())

    async def cog_unload(self):
        # Remove tree group command when reloading
        self.bot.tree.remove_command("clan")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Clans(bot))
