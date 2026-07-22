import discord
from discord.ext import commands
from discord import app_commands
from discord.http import Route
import logging
from datetime import datetime, timezone, timedelta
import io
from sqlalchemy.future import select
from sqlalchemy import delete, update
from sqlalchemy.orm import selectinload

from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.connection import get_db_session
from bot.models.clan import (
    Clan,
    ClanMember,
    ClanRole,
    ClanRolePermission,
    ClanAuditLog,
    ClanApplication,
    ClanInvite,
    ClanSettings,
    ClanOnboarding
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
    membership = result.scalar_one_or_none()
    if membership and not membership.clan:
        try:
            await session.delete(membership)
            await session.commit()
        except Exception:
            pass
        return None
    return membership

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
    """Synchronizes a member's Discord roles to match their database clan role, stripping all previous clan roles."""
    member = guild.get_member(member_id)
    if not member:
        return

    # Find the target correct discord role
    correct_discord_role = None
    target_crole = next((r for r in clan_roles if r.id == correct_role_id), None)
    
    if target_crole:
        if target_crole.discord_role_id:
            correct_discord_role = guild.get_role(target_crole.discord_role_id)
        if not correct_discord_role:
            correct_discord_role = discord.utils.get(guild.roles, name=target_crole.role_name)

    # Collect ALL discord role IDs and role names belonging to this clan
    clan_role_names = {r.role_name.lower() for r in clan_roles}
    clan_role_ids = {r.discord_role_id for r in clan_roles if r.discord_role_id}

    roles_to_remove = []
    for m_role in member.roles:
        # Check if member role matches any clan role by ID or case-insensitive name
        is_clan_role = m_role.id in clan_role_ids or m_role.name.lower() in clan_role_names
        is_correct = correct_discord_role and m_role.id == correct_discord_role.id
        
        if is_clan_role and not is_correct:
            roles_to_remove.append(m_role)

    roles_to_add = [correct_discord_role] if correct_discord_role and correct_discord_role not in member.roles else []

    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove, reason="Journey Clan Role Sync - Strip Previous Clan Roles")
        except discord.Forbidden:
            logger.warning(f"Missing permissions to remove roles from {member.display_name}")

    if roles_to_add:
        try:
            await member.add_roles(*roles_to_add, reason="Journey Clan Role Sync - Assign New Clan Role")
        except discord.Forbidden:
            logger.warning(f"Missing permissions to add role to {member.display_name}")

def parse_color(hex_str: str) -> discord.Color:
    """Safely converts hex string to discord.Color."""
    try:
        hex_clean = hex_str.strip("#").strip()
        if "," in hex_clean or "-" in hex_clean:
            parts = [p.strip().strip("#") for p in hex_clean.replace("-", ",").split(",") if p.strip()]
            hex_clean = parts[0] if parts else hex_clean
        return discord.Color(int(hex_clean, 16))
    except Exception:
        return discord.Color.default()

def parse_color_gradient(hex_str: str | None, guild: discord.Guild | None = None) -> tuple[discord.Color, list[int] | None]:
    """Converts hex or gradient string (e.g. #FF0000,#00FF00) to (primary_color, gradient_colors_array)."""
    if not hex_str:
        return (discord.Color.default(), None)
    try:
        clean_str = hex_str.strip()
        parts = [p.strip().strip("#") for p in clean_str.replace("-", ",").split(",") if p.strip()]
        if not parts:
            return (discord.Color.default(), None)
        
        primary_int = int(parts[0], 16)
        primary_color = discord.Color(primary_int)

        if len(parts) >= 2 and guild and guild.premium_tier >= 2:
            second_int = int(parts[1], 16)
            return (primary_color, [primary_int, second_int])
        
        return (primary_color, None)
    except Exception:
        return (discord.Color.default(), None)

def find_clan_role_anchor_position(guild: discord.Guild) -> int:
    """Calculates the target role position for clan roles:
    - Below Server Booster roles and custom staff/developer roles
    - Above server dedicated level roles (e.g. Level 1, Level 10, AmariBot/Arcane level roles)
    """
    bot_top = max(1, guild.me.top_role.position - 1)
    roles = [r for r in guild.roles if not r.is_integration() and not r.managed and r != guild.default_role]
    
    # 1. Look for Server Booster role
    booster_role = next((r for r in reversed(roles) if r.is_premium_subscriber() or "booster" in r.name.lower()), None)
    if booster_role:
        return max(1, booster_role.position - 1)

    # 2. Look for highest level-based role (e.g. "Level", "Lvl", "Rank")
    level_roles = [r for r in roles if any(k in r.name.lower() for k in ["level", "lvl", "rank "])]
    if level_roles:
        highest_level = max(level_roles, key=lambda r: r.position)
        return min(bot_top, highest_level.position + 1)

    return bot_top

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
    color = discord.ui.TextInput(label="Primary Hex Color (Optional)", placeholder="e.g. #FFCC00", required=False, max_length=7)
    color2 = discord.ui.TextInput(label="Secondary Gradient Color (Optional)", placeholder="e.g. #00FF00 (Level 2+ Boost)", required=False, max_length=7)
    limit = discord.ui.TextInput(label="Max Member Limit (Optional)", placeholder="e.g. 5 (0 or leave blank for unlimited)", required=False)

    def __init__(self, clan_id: int):
        super().__init__()
        self.clan_id = clan_id

    async def on_submit(self, interaction: discord.Interaction):
        name = self.role_name.value.strip()
        color_val = self.color.value.strip() or None
        color2_val = self.color2.value.strip() or None
        
        c1_int = None
        c2_int = None
        if color_val:
            try:
                c1_int = int(color_val.strip("#"), 16)
            except ValueError:
                c1_int = None
        if color2_val:
            try:
                c2_int = int(color2_val.strip("#"), 16)
            except ValueError:
                c2_int = None

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
            leader_level = 100
            next_highest_level = 1
            
            for r in roles:
                if r.hierarchy_level < leader_level:
                    next_highest_level = r.hierarchy_level
                    break
                    
            new_level = (leader_level + next_highest_level) // 2
            if new_level == next_highest_level or new_level == leader_level:
                new_level = leader_level - 1
                for r in roles:
                    if r.hierarchy_level < leader_level:
                        r.hierarchy_level -= 1
                await session.flush()

            # Create Discord role
            d_color = discord.Color(c1_int) if c1_int is not None else discord.Color.default()
            try:
                discord_role = await interaction.guild.create_role(
                    name=name,
                    color=d_color,
                    reason=f"Journey Clan Role Creation"
                )
            except discord.Forbidden:
                await interaction.response.send_message("❌ Journey Bot lacks 'Manage Roles' permissions on Discord to build this rank.", ephemeral=True)
                return

            # Apply gradient if color2_val provided
            if c1_int is not None and c2_int is not None and interaction.guild:
                route = Route('PATCH', '/guilds/{guild_id}/roles/{role_id}', guild_id=interaction.guild.id, role_id=discord_role.id)
                json_payload = {
                    "name": name,
                    "color": c1_int,
                    "secondary_color": c2_int,
                    "colors": {
                        "primary_color": c1_int,
                        "secondary_color": c2_int,
                        "tertiary_color": None
                    },
                    "role_colors": {
                        "primary_color": c1_int,
                        "secondary_color": c2_int,
                        "tertiary_color": None
                    },
                    "mentionable": True
                }
                try:
                    await interaction.client.http.request(route, json=json_payload, reason="Journey Clan Role Creation (Enhanced Gradient)")
                except Exception as err:
                    logger.warning(f"Could not apply gradient on role creation: {err}")

            # Add role to DB
            role = ClanRole(
                clan_id=self.clan_id,
                discord_role_id=discord_role.id,
                role_name=name,
                color=color_val,
                color2=color2_val,
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
    color = discord.ui.TextInput(label="Primary Hex Color (Optional)", placeholder="e.g. #FFCC00", required=False, max_length=7)
    color2 = discord.ui.TextInput(label="Secondary Gradient Color (Optional)", placeholder="e.g. #00FF00 (Level 2+ Boost)", required=False, max_length=7)
    pingable = discord.ui.TextInput(label="Pingable / Mentionable? (True / False)", placeholder="True or False", default="True", required=False, max_length=5)
    limit = discord.ui.TextInput(label="Max Member Limit (Optional)", placeholder="e.g. 5 (0 or leave blank for unlimited)", required=False)

    def __init__(self, role: ClanRole):
        super().__init__()
        self.role = role
        self.role_name.default = role.role_name
        self.color.default = role.color or ""
        self.color2.default = getattr(role, "color2", None) or ""
        self.pingable.default = "True" if getattr(role, "is_mentionable", True) else "False"
        self.limit.default = str(role.max_members) if role.max_members else ""

    async def on_submit(self, interaction: discord.Interaction):
        name = self.role_name.value.strip()
        color_val = self.color.value.strip() or None
        color2_val = self.color2.value.strip() or None
        
        # Parse pingable input
        pingable_str = self.pingable.value.strip().lower()
        is_pingable = pingable_str in ["true", "yes", "1", "y", "t"]

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
            
            # Check for gradient color input and server boost tier
            boost_notice = ""
            c1_int = None
            c2_int = None
            if color_val:
                try:
                    c1_int = int(color_val.strip("#"), 16)
                except ValueError:
                    c1_int = None
            if color2_val:
                try:
                    c2_int = int(color2_val.strip("#"), 16)
                except ValueError:
                    c2_int = None

            gradient_colors = [c1_int, c2_int] if (c1_int is not None and c2_int is not None) else None
            if color2_val and (not interaction.guild or interaction.guild.premium_tier < 2):
                boost_notice = "\n💡 *Note: Role color gradients require Server Boost Level 2+. Applied primary solid color.*"

            db_role.color = color_val
            if hasattr(db_role, "color2"):
                db_role.color2 = color2_val
            if hasattr(db_role, "is_mentionable"):
                db_role.is_mentionable = is_pingable

            # Update in-memory self.role instance so UI retains values
            self.role.role_name = name
            self.role.color = color_val
            setattr(self.role, "color2", color2_val)
            setattr(self.role, "is_mentionable", is_pingable)
            self.role.max_members = limit_val

            if db_role.hierarchy_level == 100 and limit_val is None:
                db_role.max_members = 1
            else:
                db_role.max_members = limit_val
            
            clan_res = await session.execute(select(Clan).filter_by(id=db_role.clan_id))
            clan_obj = clan_res.scalar_one_or_none()

            # Exact role name specified by leader (no clan prefix)
            expected_d_name = name

            # Clean up old duplicate/legacy roles on Discord if they exist
            if clan_obj and interaction.guild:
                legacy_names = [f"{clan_obj.name} {old_name}", f"{clan_obj.name} Leader", f"{clan_obj.name} Member", f"{clan_obj.name} {name}"]
                for g_role in interaction.guild.roles:
                    if g_role.id != db_role.discord_role_id and g_role.name in legacy_names:
                        try:
                            await g_role.delete(reason="Journey Clan Role Edit: Cleaned up old duplicate role.")
                        except discord.Forbidden:
                            pass

            # Sync Discord role properties & position cleanly via raw REST API payload
            if db_role.discord_role_id and interaction.guild:
                d_role = interaction.guild.get_role(db_role.discord_role_id)
                if d_role:
                    target_pos = find_clan_role_anchor_position(interaction.guild)
                    primary_color = discord.Color(c1_int) if c1_int is not None else discord.Color.default()

                    route = Route('PATCH', '/guilds/{guild_id}/roles/{role_id}', guild_id=interaction.guild.id, role_id=d_role.id)
                    json_payload = {
                        "name": expected_d_name,
                        "mentionable": is_pingable
                    }
                    if c1_int is not None and c2_int is not None:
                        json_payload["color"] = c1_int
                        json_payload["secondary_color"] = c2_int
                        json_payload["colors"] = {
                            "primary_color": c1_int,
                            "secondary_color": c2_int,
                            "tertiary_color": None
                        }
                        json_payload["role_colors"] = {
                            "primary_color": c1_int,
                            "secondary_color": c2_int,
                            "tertiary_color": None
                        }
                    elif c1_int is not None:
                        json_payload["color"] = c1_int

                    try:
                        await interaction.client.http.request(route, json=json_payload, reason="Journey Clan Role Modification (Enhanced Gradient & Pings)")
                    except discord.Forbidden:
                        boost_notice += "\n⚠️ **Hierarchy Notice**: Move the 'Journey' bot role ABOVE your clan roles in Discord Server Settings ➔ Roles so the bot can apply colors, pings, and position!"
                    except discord.HTTPException as e:
                        if e.status in (400, 403):
                            boost_notice += "\n⚠️ **Role Style Notice**: Creating dual-color gradient roles requires Server Boost Level 2 or Enhanced Role Styles. Fallback primary color applied."
                        else:
                            try:
                                await d_role.edit(name=expected_d_name, color=primary_color, mentionable=is_pingable, reason="Journey Clan Role Modification")
                            except Exception:
                                pass
                        
            # Log action
            await write_audit_log(
                session,
                self.role.clan_id,
                interaction.user.id,
                "role_updated",
                old_name,
                f"Name: {name}, Color: {color_val}, Limit: {limit_val}"
            )
            await session.commit()
            
        await interaction.response.send_message(f"✅ Updated role to **{name}**! Synced Discord role name, color, and hierarchy.{boost_notice}", ephemeral=True)


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
        
        move_up_btn = discord.ui.Button(label="⬆️ Move Up", style=discord.ButtonStyle.secondary, custom_id="clan_role_up", disabled=self.selected_role_id is None)
        move_up_btn.callback = self.move_up_callback
        self.add_item(move_up_btn)

        move_down_btn = discord.ui.Button(label="⬇️ Move Down", style=discord.ButtonStyle.secondary, custom_id="clan_role_down", disabled=self.selected_role_id is None)
        move_down_btn.callback = self.move_down_callback
        self.add_item(move_down_btn)

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

    async def move_up_callback(self, interaction: discord.Interaction):
        selected_role = next(r for r in self.roles if r.id == self.selected_role_id)
        sorted_roles = sorted(self.roles, key=lambda r: r.hierarchy_level)
        idx = sorted_roles.index(selected_role)
        if idx >= len(sorted_roles) - 1:
            await interaction.response.send_message("❌ Role is already at the top position.", ephemeral=True)
            return

        other_role = sorted_roles[idx + 1]
        async with get_db_session() as session:
            r1 = (await session.execute(select(ClanRole).filter_by(id=selected_role.id))).scalar_one()
            r2 = (await session.execute(select(ClanRole).filter_by(id=other_role.id))).scalar_one()
            r1.hierarchy_level, r2.hierarchy_level = r2.hierarchy_level, r1.hierarchy_level
            selected_role.hierarchy_level, other_role.hierarchy_level = r1.hierarchy_level, r2.hierarchy_level
            await session.commit()

        if selected_role.discord_role_id and interaction.guild:
            d_role = interaction.guild.get_role(selected_role.discord_role_id)
            if d_role:
                try:
                    await d_role.edit(position=d_role.position + 1, reason="Journey Clan Role Reorder")
                except discord.Forbidden:
                    pass

        self.update_select_menu()
        embed = discord.Embed(
            title=f"👑 Ranks Editor - Position Updated",
            description=f"Moved **{selected_role.role_name}** above **{other_role.role_name}**.",
            color=discord.Color.green()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def move_down_callback(self, interaction: discord.Interaction):
        selected_role = next(r for r in self.roles if r.id == self.selected_role_id)
        sorted_roles = sorted(self.roles, key=lambda r: r.hierarchy_level)
        idx = sorted_roles.index(selected_role)
        if idx <= 0:
            await interaction.response.send_message("❌ Role is already at the bottom position.", ephemeral=True)
            return

        other_role = sorted_roles[idx - 1]
        async with get_db_session() as session:
            r1 = (await session.execute(select(ClanRole).filter_by(id=selected_role.id))).scalar_one()
            r2 = (await session.execute(select(ClanRole).filter_by(id=other_role.id))).scalar_one()
            r1.hierarchy_level, r2.hierarchy_level = r2.hierarchy_level, r1.hierarchy_level
            selected_role.hierarchy_level, other_role.hierarchy_level = r1.hierarchy_level, r2.hierarchy_level
            await session.commit()

        if selected_role.discord_role_id and interaction.guild:
            d_role = interaction.guild.get_role(selected_role.discord_role_id)
            if d_role:
                try:
                    await d_role.edit(position=max(1, d_role.position - 1), reason="Journey Clan Role Reorder")
                except discord.Forbidden:
                    pass

        self.update_select_menu()
        embed = discord.Embed(
            title=f"👑 Ranks Editor - Position Updated",
            description=f"Moved **{selected_role.role_name}** below **{other_role.role_name}**.",
            color=discord.Color.green()
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


async def fetch_usernames(client: discord.Client, user_ids: list[int]) -> dict[int, str]:
    """Resolves usernames concurrently for pending application drop-downs."""
    names = {}
    for uid in user_ids:
        user = client.get_user(uid)
        if not user:
            try:
                user = await client.fetch_user(uid)
            except Exception:
                pass
        names[uid] = user.display_name if user else f"User {uid}"
    return names

async def validate_and_submit_application(
    session: AsyncSession,
    guild: discord.Guild,
    user_id: int,
    clan_id: int,
    source: str
) -> tuple[bool, str]:
    """Validates eligibility and creates a pending clan application in the database."""
    from sqlalchemy import func
    from bot.models.user import UserGuildStats
    from bot.models.clan import Clan, ClanMember, ClanRole, ClanApplication, ClanSettings

    # 1. Fetch user stats (User existence check)
    stats_res = await session.execute(
        select(UserGuildStats).filter_by(guild_id=guild.id, user_id=user_id)
    )
    stats = stats_res.scalar_one_or_none()
    if not stats:
        return False, "You do not have leveling stats initialized yet. Speak in the server first."
        
    # 2. Fetch clan (Existence check)
    clan_res = await session.execute(
        select(Clan).filter_by(id=clan_id)
    )
    clan = clan_res.scalar_one_or_none()
    if not clan or clan.guild_id != guild.id:
        return False, "The target clan does not exist."
        
    # 3. Check approved
    if not clan.approved:
        return False, "This clan is pending Staff Approval and cannot accept applications."
        
    # 4. Check settings (Applications enabled)
    clan_settings_res = await session.execute(
        select(ClanSettings).filter_by(clan_id=clan.id)
    )
    clan_settings = clan_settings_res.scalar_one_or_none()
    if not clan_settings or clan_settings.join_type != "apply":
        return False, "This clan has disabled applications (invite only or open)."
        
    # 5. Check if user already in a clan
    existing_member_res = await session.execute(
        select(ClanMember).filter_by(guild_id=guild.id, user_id=user_id)
    )
    existing_member = existing_member_res.scalar_one_or_none()
    if existing_member:
        return False, "You are already in a clan."
        
    # 6. Check pending applications
    pending_res = await session.execute(
        select(ClanApplication).filter_by(clan_id=clan.id, user_id=user_id, status="pending")
    )
    pending = pending_res.scalars().first()
    if pending:
        return False, "You already have a pending application for this clan."
        
    # 7. Check clan member limit (Clan not full)
    members_count_res = await session.execute(
        select(func.count(ClanMember.user_id)).filter_by(clan_id=clan.id)
    )
    members_count = members_count_res.scalar() or 0
    if members_count >= 50:
        return False, "This clan is currently full (maximum 50 members)."
        
    # Create application
    app = ClanApplication(
        guild_id=guild.id,
        clan_id=clan.id,
        user_id=user_id,
        status="pending",
        application_source=source
    )
    session.add(app)
    await session.flush()
    
    # Audit log
    await write_audit_log(
        session,
        clan.id,
        user_id,
        "application_created",
        None,
        f"Source: {source}, App ID: {app.id}"
    )
    
    # Send Notification to clan officers & leader
    try:
        owner = guild.get_member(clan.owner_id)
        pending_count_res = await session.execute(
            select(func.count(ClanApplication.id)).filter_by(clan_id=clan.id, status="pending")
        )
        pending_count = pending_count_res.scalar() or 0
        
        if owner:
            embed = discord.Embed(
                title="🔔 New Clan Application",
                description=f"**<@{user_id}>** applied to your clan **{clan.name}**!",
                color=discord.Color.green()
            )
            embed.add_field(name="Application Source", value=source.capitalize(), inline=True)
            embed.add_field(name="Pending Applications", value=str(pending_count), inline=True)
            try:
                await owner.send(embed=embed)
            except discord.Forbidden:
                pass
    except Exception as e:
        logger.warning(f"Failed to send application notification: {e}")
        
    return True, "Application submitted successfully."

class ApplicationSelect(discord.ui.Select):
    def __init__(self, applications: list[ClanApplication], user_names: dict[int, str]):
        options = []
        for app in applications[:25]:
            username = user_names.get(app.user_id, f"User {app.user_id}")
            options.append(discord.SelectOption(
                label=username,
                value=str(app.id),
                description=f"Source: {app.application_source} | Applied At: {app.created_at.strftime('%Y-%m-%d')}"
            ))
        super().__init__(placeholder="Select an application to inspect...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "ApplicationsView" = self.view
        selected_id = int(self.values[0])
        view.selected_app_idx = next(i for i, app in enumerate(view.applications) if app.id == selected_id)
        embed = await view.get_current_app_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=view)

class RejectReasonModal(discord.ui.Modal, title="Rejection Reason"):
    reason = discord.ui.TextInput(
        label="Optional Reason",
        placeholder="Enter a reason for the rejection...",
        required=False,
        max_length=200
    )

    def __init__(self, view: "ApplicationsView", app_id: int):
        super().__init__()
        self.view = view
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        app = next((a for a in self.view.applications if a.id == self.app_id), None)
        if not app:
            await interaction.followup.send("❌ That application is no longer pending.", ephemeral=True)
            return
            
        reason_text = self.reason.value or "No reason specified"
        async with get_db_session() as session:
            await session.execute(
                update(ClanApplication)
                .filter_by(id=app.id)
                .values(status="rejected", reviewed_at=datetime.utcnow(), reviewed_by=interaction.user.id, reason=reason_text)
            )
            await write_audit_log(
                session,
                self.view.clan_id,
                interaction.user.id,
                "application_rejected",
                f"User: {app.user_id}",
                f"Reason: {reason_text}"
            )
            await session.commit()
            
            try:
                applicant = interaction.guild.get_member(app.user_id) or await interaction.client.fetch_user(app.user_id)
                if applicant:
                    embed = discord.Embed(
                        title="❌ Clan Application Status",
                        description=f"Your application to join **{interaction.guild.name}**'s clan has been rejected.",
                        color=discord.Color.red()
                    )
                    embed.add_field(name="Reason", value=reason_text, inline=False)
                    await applicant.send(embed=embed)
            except Exception:
                pass
                
        self.view.applications = [a for a in self.view.applications if a.id != self.app_id]
        if self.view.selected_app_idx >= len(self.view.applications):
            self.view.selected_app_idx = max(0, len(self.view.applications) - 1)
            
        self.view.refresh_dropdown_options()
        embed = await self.view.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self.view)
        await interaction.followup.send("✅ Application rejected successfully.", ephemeral=True)

class ApplicationsView(discord.ui.View):
    def __init__(self, clan_id: int, applications: list[ClanApplication], manager_id: int, user_names: dict[int, str]):
        super().__init__(timeout=180)
        self.clan_id = clan_id
        self.applications = applications
        self.manager_id = manager_id
        self.user_names = user_names
        self.selected_app_idx = 0
        self.refresh_dropdown_options()

    def refresh_dropdown_options(self):
        self.clear_items()
        if not self.applications:
            return
            
        self.add_item(ApplicationSelect(self.applications, self.user_names))
        
        accept_btn = discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, row=1)
        accept_btn.callback = self.accept_callback
        self.add_item(accept_btn)
        
        reject_btn = discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, row=1)
        reject_btn.callback = self.reject_callback
        self.add_item(reject_btn)
        
        profile_btn = discord.ui.Button(label="View Profile", style=discord.ButtonStyle.secondary, row=1)
        profile_btn.callback = self.profile_callback
        self.add_item(profile_btn)
        
        bulk_accept_btn = discord.ui.Button(label="Accept All", style=discord.ButtonStyle.success, row=2)
        bulk_accept_btn.callback = self.bulk_accept_callback
        self.add_item(bulk_accept_btn)
        
        bulk_reject_btn = discord.ui.Button(label="Reject All", style=discord.ButtonStyle.danger, row=2)
        bulk_reject_btn.callback = self.bulk_reject_callback
        self.add_item(bulk_reject_btn)
        
        refresh_btn = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.primary, row=2)
        refresh_btn.callback = self.refresh_callback
        self.add_item(refresh_btn)

    async def get_current_app_embed(self, interaction: discord.Interaction) -> discord.Embed:
        if not self.applications or self.selected_app_idx >= len(self.applications):
            return discord.Embed(
                title="📋 Pending Applications Queue",
                description="*No pending applications.*",
                color=discord.Color.blue()
            )
            
        app = self.applications[self.selected_app_idx]
        user = interaction.guild.get_member(app.user_id)
        if not user:
            try:
                user = await interaction.client.fetch_user(app.user_id)
            except Exception:
                pass
                
        username = user.display_name if user else f"User {app.user_id}"
        
        embed = discord.Embed(
            title="📋 Pending Applications Queue",
            description=f"Inspecting application **{self.selected_app_idx + 1} / {len(self.applications)}**",
            color=discord.Color.blue()
        )
        
        embed.add_field(name="Username", value=username, inline=True)
        embed.add_field(name="User ID", value=str(app.user_id), inline=True)
        embed.add_field(name="Application Source", value=app.application_source.capitalize(), inline=True)
        embed.add_field(name="Applied At", value=app.created_at.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
        embed.add_field(name="Current Clan", value="None", inline=True)
        
        if user and isinstance(user, discord.Member):
            joined_server_days = (datetime.now(timezone.utc) - user.joined_at).days
            embed.add_field(name="Joined Server", value=f"{joined_server_days} days ago", inline=True)
            acct_age_days = (datetime.now(timezone.utc) - user.created_at).days
            embed.add_field(name="Account Age", value=f"{acct_age_days} days ago", inline=True)
        else:
            embed.add_field(name="Joined Server", value="Not in server", inline=True)
            embed.add_field(name="Account Age", value="Unknown", inline=True)
            
        return embed

    async def accept_callback(self, interaction: discord.Interaction):
        if not self.applications or self.selected_app_idx >= len(self.applications):
            await interaction.response.send_message("❌ No application is currently selected.", ephemeral=True)
            return
            
        app = self.applications[self.selected_app_idx]
        await interaction.response.defer(ephemeral=True)
        
        async with get_db_session() as session:
            from sqlalchemy import func
            member = interaction.guild.get_member(app.user_id)
            if not member:
                await interaction.followup.send("❌ Target member is no longer in the server.", ephemeral=True)
                return
                
            existing = await get_member_membership(session, interaction.guild_id, app.user_id)
            if existing:
                await interaction.followup.send("❌ This user has already joined a clan.", ephemeral=True)
                return
                
            members_count_res = await session.execute(
                select(func.count(ClanMember.user_id)).filter_by(clan_id=self.clan_id)
            )
            members_count = members_count_res.scalar() or 0
            if members_count >= 50:
                await interaction.followup.send("❌ The clan has reached its maximum size of 50 members.", ephemeral=True)
                return
                
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=self.clan_id).order_by(ClanRole.hierarchy_level.asc())
            )
            roles = list(roles_result.scalars())
            recruit_role = roles[0] if roles else None
            
            membership = ClanMember(
                guild_id=interaction.guild_id,
                user_id=app.user_id,
                clan_id=self.clan_id,
                role_id=recruit_role.id if recruit_role else None
            )
            session.add(membership)
            
            await session.execute(
                update(ClanApplication)
                .filter_by(id=app.id)
                .values(status="approved", reviewed_at=datetime.utcnow(), reviewed_by=interaction.user.id)
            )
            
            await write_audit_log(session, self.clan_id, interaction.user.id, "application_accepted", f"User: {app.user_id}")
            await session.commit()
            
            if recruit_role:
                await sync_discord_roles(interaction.guild, app.user_id, recruit_role.id, roles)
                
            try:
                embed = discord.Embed(
                    title="🎉 Clan Application Status",
                    description=f"Your application to join **{interaction.guild.name}**'s clan has been approved!",
                    color=discord.Color.green()
                )
                await member.send(embed=embed)
            except Exception:
                pass
                
        self.applications.pop(self.selected_app_idx)
        if self.selected_app_idx >= len(self.applications):
            self.selected_app_idx = max(0, len(self.applications) - 1)
            
        await self.refresh_dropdown_options()
        embed = await self.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("✅ Application approved successfully.", ephemeral=True)

    async def reject_callback(self, interaction: discord.Interaction):
        if not self.applications or self.selected_app_idx >= len(self.applications):
            await interaction.response.send_message("❌ No application is currently selected.", ephemeral=True)
            return
            
        app = self.applications[self.selected_app_idx]
        modal = RejectReasonModal(self, app.id)
        await interaction.response.send_modal(modal)

    async def profile_callback(self, interaction: discord.Interaction):
        if not self.applications or self.selected_app_idx >= len(self.applications):
            await interaction.response.send_message("❌ No application is currently selected.", ephemeral=True)
            return
            
        app = self.applications[self.selected_app_idx]
        await interaction.response.defer(ephemeral=True)
        async with get_db_session() as session:
            from bot.models.user import UserGuildStats
            stats = await DatabaseService.get_or_create_stats(session, interaction.guild_id, app.user_id)
            msg = (
                f"👤 **Player Profile Summary** (ID: `{app.user_id}`):\n"
                f"🌟 **Level:** {stats.level} | **Total XP:** {stats.xp:,}\n"
                f"🗺️ **Master Path ID:** {stats.master_path_id or 'None'}\n"
                f"📅 **Registered At:** {stats.created_at.strftime('%Y-%m-%d')}"
            )
        await interaction.followup.send(msg, ephemeral=True)

    async def bulk_accept_callback(self, interaction: discord.Interaction):
        if not self.applications:
            await interaction.response.send_message("❌ No pending applications in the queue.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        accepted = 0
        skipped = 0
        failed = 0
        
        async with get_db_session() as session:
            from sqlalchemy import func
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=self.clan_id).order_by(ClanRole.hierarchy_level.asc())
            )
            roles = list(roles_result.scalars())
            recruit_role = roles[0] if roles else None
            
            for app in list(self.applications):
                member = interaction.guild.get_member(app.user_id)
                if not member:
                    skipped += 1
                    continue
                    
                existing = await get_member_membership(session, interaction.guild_id, app.user_id)
                if existing:
                    skipped += 1
                    continue
                    
                members_count_res = await session.execute(
                    select(func.count(ClanMember.user_id)).filter_by(clan_id=self.clan_id)
                )
                members_count = members_count_res.scalar() or 0
                if members_count >= 50:
                    failed += 1
                    continue
                    
                membership = ClanMember(
                    guild_id=interaction.guild_id,
                    user_id=app.user_id,
                    clan_id=self.clan_id,
                    role_id=recruit_role.id if recruit_role else None
                )
                session.add(membership)
                
                await session.execute(
                    update(ClanApplication)
                    .filter_by(id=app.id)
                    .values(status="approved", reviewed_at=datetime.utcnow(), reviewed_by=interaction.user.id)
                )
                
                await write_audit_log(session, self.clan_id, interaction.user.id, "application_accepted", f"User: {app.user_id}")
                
                if recruit_role:
                    try:
                        await sync_discord_roles(interaction.guild, app.user_id, recruit_role.id, roles)
                    except Exception:
                        pass
                accepted += 1
                
            await session.commit()
            
        self.applications = []
        self.selected_app_idx = 0
        await self.refresh_dropdown_options()
        embed = await self.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self)
        
        summary = (
            f"✅ **Bulk Approval Complete!**\n"
            f"👍 **Accepted:** {accepted}\n"
            f"⏭️ **Skipped:** {skipped}\n"
            f"⚠️ **Failed:** {failed}"
        )
        await interaction.followup.send(summary, ephemeral=True)

    async def bulk_reject_callback(self, interaction: discord.Interaction):
        if not self.applications:
            await interaction.response.send_message("❌ No pending applications in the queue.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        rejected = 0
        skipped = 0
        
        async with get_db_session() as session:
            for app in list(self.applications):
                await session.execute(
                    update(ClanApplication)
                    .filter_by(id=app.id)
                    .values(status="rejected", reviewed_at=datetime.utcnow(), reviewed_by=interaction.user.id, reason="Bulk rejected by officer")
                )
                await write_audit_log(session, self.clan_id, interaction.user.id, "application_rejected", f"User: {app.user_id}", "Bulk rejected")
                rejected += 1
                
            await session.commit()
            
        self.applications = []
        self.selected_app_idx = 0
        await self.refresh_dropdown_options()
        embed = await self.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self)
        
        summary = (
            f"❌ **Bulk Rejection Complete!**\n"
            f"👎 **Rejected:** {rejected}\n"
            f"⏭️ **Skipped:** {skipped}"
        )
        await interaction.followup.send(summary, ephemeral=True)

    async def refresh_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cutoff = datetime.utcnow() - timedelta(days=7)
        async with get_db_session() as session:
            apps_result = await session.execute(
                select(ClanApplication)
                .filter_by(clan_id=self.clan_id, status="pending")
                .filter(ClanApplication.created_at >= cutoff)
                .order_by(ClanApplication.created_at.asc())
            )
            self.applications = list(apps_result.scalars())
            user_ids = [app.user_id for app in self.applications]
            self.user_names = await fetch_usernames(interaction.client, user_ids)
            
        self.selected_app_idx = 0
        await self.refresh_dropdown_options()
        embed = await self.get_current_app_embed(interaction)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("🔄 Applications queue refreshed.", ephemeral=True)


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

ACTIVE_ELECTIONS: dict[int, dict] = {}

class CandidateSelect(discord.ui.Select):
    def __init__(self, clan_id: int, candidates: dict):
        options = [
            discord.SelectOption(label=name[:100], value=str(uid), description="Vote for this candidate")
            for uid, name in candidates.items()
        ]
        super().__init__(placeholder="Select a candidate to vote for...", min_values=1, max_values=1, options=options)
        self.clan_id = clan_id

    async def callback(self, interaction: discord.Interaction):
        candidate_id = int(self.values[0])
        async with get_db_session() as session:
            voter_membership = await get_member_membership(session, interaction.guild_id, interaction.user.id)
            if not voter_membership or voter_membership.clan_id != self.clan_id:
                await interaction.response.send_message("❌ Only members of this clan can vote in this election.", ephemeral=True)
                return
                
        election = ACTIVE_ELECTIONS.get(self.clan_id)
        if not election:
            await interaction.response.send_message("❌ Election is no longer active.", ephemeral=True)
            return
            
        election["votes"][interaction.user.id] = candidate_id
        candidate_name = election["candidates"].get(candidate_id, "Candidate")
        await interaction.response.send_message(f"✅ Your vote for **{candidate_name}** has been cast!", ephemeral=True)


class ClanElectionView(discord.ui.View):
    def __init__(self, clan_id: int, clan_name: str):
        super().__init__(timeout=None)
        self.clan_id = clan_id
        self.clan_name = clan_name

    def get_embed(self, guild: discord.Guild) -> discord.Embed:
        election = ACTIVE_ELECTIONS.get(self.clan_id, {"candidates": {}, "votes": {}})
        candidates = election["candidates"]
        votes = election["votes"]

        tallies = {uid: 0 for uid in candidates}
        for voter_id, cand_id in votes.items():
            if cand_id in tallies:
                tallies[cand_id] += 1

        cand_list = []
        for uid, name in candidates.items():
            count = tallies.get(uid, 0)
            cand_list.append(f"• **{name}** (`{count} vote{'s' if count != 1 else ''}`)")

        desc = (
            f"👑 **Clan Leader Election in Progress** for **{self.clan_name}**!\n"
            f"The previous leader has departed. Clan members can nominate themselves or cast their vote below.\n\n"
            f"**Candidates List:**\n" + ("\n".join(cand_list) if cand_list else "*No candidates nominated yet. Click 'Nominate Myself' below!*") + "\n\n"
            f"**Total Votes Cast:** `{len(votes)}`"
        )
        embed = discord.Embed(title=f"🗳️ Clan Leadership Election: {self.clan_name}", description=desc, color=discord.Color.gold())
        embed.set_footer(text="Staff can conclude the election when voting is complete.")
        return embed

    @discord.ui.button(label="🙋 Nominate Myself", style=discord.ButtonStyle.primary, custom_id="election_nominate")
    async def nominate(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db_session() as session:
            membership = await get_member_membership(session, interaction.guild_id, interaction.user.id)
            if not membership or membership.clan_id != self.clan_id:
                await interaction.response.send_message("❌ Only members of this clan can enter the election.", ephemeral=True)
                return

        election = ACTIVE_ELECTIONS.setdefault(self.clan_id, {"candidates": {}, "votes": {}})
        election["candidates"][interaction.user.id] = interaction.user.display_name
        await interaction.response.edit_message(embed=self.get_embed(interaction.guild), view=self)

    @discord.ui.button(label="🗳️ Cast Vote", style=discord.ButtonStyle.success, custom_id="election_vote")
    async def vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        election = ACTIVE_ELECTIONS.get(self.clan_id)
        if not election or not election["candidates"]:
            await interaction.response.send_message("❌ No candidates have nominated themselves yet.", ephemeral=True)
            return

        view = discord.ui.View()
        view.add_item(CandidateSelect(self.clan_id, election["candidates"]))
        await interaction.response.send_message("Select the candidate you want to vote for:", view=view, ephemeral=True)

    @discord.ui.button(label="🏁 Conclude Election (Staff Only)", style=discord.ButtonStyle.danger, custom_id="election_conclude")
    async def conclude(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        is_staff = member and (member.guild_permissions.administrator or member.guild_permissions.manage_roles)
        if not is_staff:
            await interaction.response.send_message("❌ Only Server Staff can conclude the election.", ephemeral=True)
            return

        election = ACTIVE_ELECTIONS.pop(self.clan_id, None)
        if not election or not election["candidates"]:
            await interaction.response.send_message("❌ Cannot conclude: No candidates or election data found.", ephemeral=True)
            return

        tallies = {uid: 0 for uid in election["candidates"]}
        for voter_id, cand_id in election["votes"].items():
            if cand_id in tallies:
                tallies[cand_id] += 1

        winner_id = max(tallies, key=tallies.get)
        max_votes = tallies[winner_id]
        winner_name = election["candidates"][winner_id]

        async with get_db_session() as session:
            clan_res = await session.execute(select(Clan).filter_by(id=self.clan_id))
            clan = clan_res.scalar_one_or_none()
            if clan:
                clan.owner_id = winner_id

            roles_res = await session.execute(select(ClanRole).filter_by(clan_id=self.clan_id).order_by(ClanRole.hierarchy_level.desc()))
            roles = list(roles_res.scalars())
            leader_role = roles[0] if roles else None

            if leader_role:
                member_res = await session.execute(select(ClanMember).filter_by(clan_id=self.clan_id, user_id=winner_id))
                cmember = member_res.scalar_one_or_none()
                if cmember:
                    cmember.role_id = leader_role.id
                    await session.commit()
                    
                if interaction.guild:
                    await sync_discord_roles(interaction.guild, winner_id, leader_role.id, roles)

        for child in self.children:
            child.disabled = True

        embed = discord.Embed(
            title=f"🎉 Election Concluded - {self.clan_name}",
            description=f"👑 **{winner_name}** won the election with **{max_votes} vote{'s' if max_votes != 1 else ''}** and is now the official **Clan Leader** of **{self.clan_name}**!",
            color=discord.Color.green()
        )
        await interaction.response.edit_message(embed=embed, view=self)


# ==============================================================================
# CLAN SLASH COMMANDS COG
# ==============================================================================

@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
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
    @app_commands.describe(
        name="The name of the clan to approve.",
        create_text="Create a private text channel for this clan? (Default: True)",
        create_voice="Create a private voice channel for this clan? (Default: True)",
        category_name="Name of the category to place channels in. (Default: '🏆 CLAN CATEGORY')"
    )
    async def clan_approve(
        self,
        interaction: discord.Interaction,
        name: str,
        create_text: bool = True,
        create_voice: bool = True,
        category_name: str = "🏆 CLAN CATEGORY"
    ) -> None:
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
            
            # Enable applications
            from bot.models.clan import ClanSettings
            clan_settings = await session.get(ClanSettings, clan.id)
            if clan_settings:
                clan_settings.join_type = "apply"
                
            # Setup channels if requested
            if create_text or create_voice:
                try:
                    category = discord.utils.get(interaction.guild.categories, name=category_name)
                    if not category:
                        category = await interaction.guild.create_category(
                            name=category_name,
                            reason="Journey Clan Category Setup"
                        )
                    
                    clan.discord_category_id = category.id
                    
                    # Define overrides
                    overwrites = {
                        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        member_d_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                        leader_d_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True),
                        interaction.guild.me.top_role: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True)
                    }
                    
                    if create_text:
                        text_channel = await interaction.guild.create_text_channel(
                            name=f"💬-{clan.name.lower().replace(' ', '-')}",
                            category=category,
                            overwrites=overwrites,
                            topic=f"Official private channel for {clan.name}.",
                            reason=f"Journey Clan Approval: Initialize private text channel."
                        )
                        clan.discord_text_channel_id = text_channel.id
                        
                    if create_voice:
                        voice_channel = await interaction.guild.create_voice_channel(
                            name=f"🔊-{clan.name.lower().replace(' ', '-')}",
                            category=category,
                            overwrites=overwrites,
                            reason=f"Journey Clan Approval: Initialize private voice channel."
                        )
                        clan.discord_voice_channel_id = voice_channel.id
                except Exception as e:
                    logger.warning(f"Failed to create Discord channels during clan approval: {e}")
            
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
                    
        await interaction.response.send_message(f"✅ Clan **{clan.name}** has been approved! Ranks have been initialized and visual roles/channels registered in Discord.")

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
            if not membership.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
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

    @app_commands.command(name="reorder_roles", description="Reorders all clan roles directly above a specified server level role (Staff/Admins only).")
    @app_commands.describe(
        highest_level_role="The highest level/rank role on the server below which level roles reside.",
        clan_name="Optional clan name to target (Defaults to your clan if you are a leader)."
    )
    @app_commands.default_permissions(administrator=True)
    async def clan_reorder_roles(self, interaction: discord.Interaction, highest_level_role: discord.Role, clan_name: str | None = None) -> None:
        """Reorders all clan roles to sit directly above a specified server level role (Staff/Admins only)."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        # Check if user is Staff/Admin with Manage Roles or Administrator permissions
        member = interaction.guild.get_member(interaction.user.id)
        is_staff = member and (member.guild_permissions.administrator or member.guild_permissions.manage_roles)
        
        if not is_staff:
            await interaction.response.send_message("❌ Only Server Staff & Administrators with 'Manage Roles' permission can reorder clan roles.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        async with get_db_session() as session:
            if clan_name:
                clan_res = await session.execute(
                    select(Clan).filter_by(guild_id=interaction.guild_id).filter(Clan.name.ilike(clan_name.strip()))
                )
                clan = clan_res.scalar_one_or_none()
            else:
                membership = await get_member_membership(session, interaction.guild_id, interaction.user.id)
                clan = membership.clan if membership else None
                if not clan:
                    clan_res = await session.execute(
                        select(Clan).filter_by(guild_id=interaction.guild_id, approved=True)
                    )
                    clan = clan_res.scalars().first()

            if not clan:
                await interaction.followup.send("❌ No matching approved clan found in this server. Please specify `clan_name`.", ephemeral=True)
                return

            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id).order_by(ClanRole.hierarchy_level.asc())
            )
            clan_roles = list(roles_result.scalars())
            if not clan_roles:
                await interaction.followup.send(f"❌ No clan roles found for clan **{clan.name}**.", ephemeral=True)
                return

            target_pos = highest_level_role.position + 1
            reordered_summary = []
            
            for crole in clan_roles:
                d_role = interaction.guild.get_role(crole.discord_role_id) if crole.discord_role_id else None
                if not d_role:
                    d_role = discord.utils.get(interaction.guild.roles, name=crole.role_name)
                if d_role:
                    try:
                        await d_role.edit(position=target_pos, reason="Journey Staff Clan Role Reorder above level role")
                        reordered_summary.append(f"• **{crole.role_name}** ({d_role.mention}) ➔ Position `{target_pos}`")
                        target_pos += 1
                    except discord.Forbidden:
                        reordered_summary.append(f"• **{crole.role_name}** (⚠️ Bot role is placed below this role in Server Settings -> Roles)")

            embed = discord.Embed(
                title=f"📶 Clan Roles Reordered ({clan.name})",
                description=f"Staff successfully placed all **{clan.name}** clan roles directly above **{highest_level_role.mention}**:\n\n" + "\n".join(reversed(reordered_summary)),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="election", description="Initiates a Clan Leader election for clans whose leader left (Staff/Admins only).")
    @app_commands.describe(clan_name="The name of the clan to hold an election for.")
    @app_commands.default_permissions(administrator=True)
    async def clan_election(self, interaction: discord.Interaction, clan_name: str) -> None:
        """Starts an interactive leader election panel for a clan (Staff only)."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        is_staff = member and (member.guild_permissions.administrator or member.guild_permissions.manage_roles)
        if not is_staff:
            await interaction.response.send_message("❌ Only Server Staff & Administrators can initiate clan leader elections.", ephemeral=True)
            return

        async with get_db_session() as session:
            clan_res = await session.execute(
                select(Clan).filter_by(guild_id=interaction.guild_id).filter(Clan.name.ilike(clan_name.strip()))
            )
            clan = clan_res.scalar_one_or_none()
            if not clan:
                await interaction.response.send_message(f"❌ Clan '{clan_name}' not found in this server.", ephemeral=True)
                return

            leader_member = interaction.guild.get_member(clan.owner_id)
            leader_status = "⚠️ (Previous leader left the server)" if not leader_member else "ℹ️ (Leader is still present)"

            ACTIVE_ELECTIONS[clan.id] = {
                "started_by": interaction.user.id,
                "candidates": {},
                "votes": {}
            }

            view = ClanElectionView(clan.id, clan.name)
            embed = view.get_embed(interaction.guild)

            await interaction.response.send_message(
                f"📢 **Staff Notice**: Clan Leader election initiated for **{clan.name}** {leader_status}!",
                embed=embed,
                view=view
            )

    @app_commands.command(name="pin", description="Pins a message in the clan's private text channel.")
    @app_commands.describe(message_id="The ID or link of the message to pin.")
    async def clan_pin(self, interaction: discord.Interaction, message_id: str) -> None:
        """Pins a message in the clan's private text channel."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        try:
            msg_id_int = int(message_id.strip().split('/')[-1])
        except ValueError:
            await interaction.response.send_message("❌ Invalid Message ID or Link. Please copy the numeric Message ID or Message Link.", ephemeral=True)
            return

        async with get_db_session() as session:
            membership = await get_member_membership(session, interaction.guild_id, interaction.user.id)
            if not membership:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return

            clan = membership.clan
            if not clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are locked.", ephemeral=True)
                return

            member_obj = interaction.guild.get_member(interaction.user.id)
            is_leader = clan.owner_id == interaction.user.id
            is_staff = member_obj and (member_obj.guild_permissions.administrator or member_obj.guild_permissions.manage_messages)

            if not is_leader and not is_staff:
                if not (membership.role and membership.role.permissions and getattr(membership.role.permissions, "can_manage_messages", False)):
                    await interaction.response.send_message("❌ Only Clan Leaders, Officers, or Staff can pin messages.", ephemeral=True)
                    return

            if not clan.discord_text_channel_id:
                await interaction.response.send_message("❌ Your clan does not have a private text channel configured.", ephemeral=True)
                return

            text_channel = interaction.guild.get_channel(clan.discord_text_channel_id)
            if not isinstance(text_channel, discord.TextChannel):
                await interaction.response.send_message("❌ Could not find your clan's private text channel.", ephemeral=True)
                return

            try:
                msg = await text_channel.fetch_message(msg_id_int)
                await msg.pin(reason=f"Journey Clan Pin requested by {interaction.user.display_name}")
            except discord.NotFound:
                await interaction.response.send_message(f"❌ Message with ID `{msg_id_int}` was not found in {text_channel.mention}.", ephemeral=True)
                return
            except discord.Forbidden:
                await interaction.response.send_message("❌ The bot lacks 'Manage Messages' permission to pin this message.", ephemeral=True)
                return

        await interaction.response.send_message(f"📌 Successfully pinned message ([Jump to Message]({msg.jump_url})) in {text_channel.mention}!")

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
            if not membership.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
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
            if not exec_member.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
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

    @app_commands.command(name="demote", description="Demotes a member (or leader) to the previous rank in the hierarchy.")
    @app_commands.describe(member="The member to demote.")
    async def clan_demote(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Demotes a user inside the clan. Leaders demoting themselves transfer leadership to the next highest member."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
            if not exec_member.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            is_self_demotion = member.id == interaction.user.id

            if is_self_demotion and not is_leader:
                await interaction.response.send_message("❌ Only the Clan Leader can demote themselves.", ephemeral=True)
                return

            can_demote = exec_member.role.permissions.can_demote if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_demote:
                await interaction.response.send_message("❌ You lack permission to demote members.", ephemeral=True)
                return
                
            target_member = await get_member_membership(session, guild_id, member.id)
            if not target_member or target_member.clan_id != exec_member.clan_id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            # If target is Leader but command executed by someone else
            if target_member.clan.owner_id == member.id and not is_self_demotion:
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

            leadership_transferred_to = None
            leader_role = roles[-1]

            # Handle Leader Self-Demotion
            if is_self_demotion and is_leader:
                other_members_res = await session.execute(
                    select(ClanMember)
                    .options(selectinload(ClanMember.role))
                    .filter_by(clan_id=exec_member.clan_id)
                    .filter(ClanMember.user_id != interaction.user.id)
                )
                other_members = list(other_members_res.scalars())
                if other_members:
                    other_members.sort(key=lambda m: m.role.hierarchy_level if m.role else 0, reverse=True)
                    successor = other_members[0]
                    successor.role_id = leader_role.id
                    exec_member.clan.owner_id = successor.user_id
                    leadership_transferred_to = interaction.guild.get_member(successor.user_id)
                    
                    await sync_discord_roles(interaction.guild, successor.user_id, leader_role.id, roles)

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
            
            # Synchronize roles (stripping old role and adding new one)
            await sync_discord_roles(interaction.guild, member.id, prev_role.id, roles)
            
        if leadership_transferred_to:
            await interaction.response.send_message(
                f"📉 **{member.display_name}** has demoted themselves to **{prev_role.role_name}**!\n"
                f"👑 Clan Leadership has been transferred to **{leadership_transferred_to.display_name}** ({leader_role.role_name})!"
            )
        else:
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
            if not exec_member.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
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

    @app_commands.command(name="apply", description="Applies to join an approved clan.")
    @app_commands.describe(clan_name="The name of the clan to apply to.")
    async def clan_apply(self, interaction: discord.Interaction, clan_name: str) -> None:
        """Submits a membership application to a clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            # Fetch clan
            clan_result = await session.execute(
                select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(clan_name))
            )
            clan = clan_result.scalar_one_or_none()
            if not clan:
                await interaction.followup.send(f"❌ Clan '{clan_name}' not found.", ephemeral=True)
                return
                
            success, error_msg = await validate_and_submit_application(
                session, interaction.guild, interaction.user.id, clan.id, "manual"
            )
            if not success:
                await interaction.followup.send(f"❌ {error_msg}", ephemeral=True)
                return
                
            await session.commit()
            
        await interaction.followup.send(f"✅ Your application to join **{clan.name}** has been submitted successfully!", ephemeral=True)

    @app_commands.command(name="applications", description="View and manage pending applications (Officers with permission only).")
    async def clan_applications(self, interaction: discord.Interaction) -> None:
        """View applications queue."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.followup.send("❌ You are not in a clan.", ephemeral=True)
                return
            if not exec_member.clan.approved:
                await interaction.followup.send("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
                return
                
            is_leader = exec_member.clan.owner_id == interaction.user.id
            can_review = exec_member.role.permissions.can_review_applications if (exec_member.role and exec_member.role.permissions) else False
            
            if not is_leader and not can_review:
                await interaction.followup.send("❌ You lack permission to manage applications.", ephemeral=True)
                return
                
            # Fetch pending applications
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            apps_result = await session.execute(
                select(ClanApplication)
                .filter_by(clan_id=exec_member.clan_id, status="pending")
                .filter(ClanApplication.created_at >= cutoff.replace(tzinfo=None))
                .order_by(ClanApplication.created_at.asc())
            )
            applications = list(apps_result.scalars())
            clan_id = exec_member.clan_id

        if not applications:
            await interaction.followup.send("📋 There are no pending applications for your clan.", ephemeral=True)
            return
            
        user_ids = [app.user_id for app in applications]
        user_names = await fetch_usernames(interaction.client, user_ids)
        
        view = ApplicationsView(clan_id, applications, interaction.user.id, user_names)
        embed = await view.get_current_app_embed(interaction)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ==============================================================================
    # CLAN CHANNEL ADMINISTRATION GROUP
    # ==============================================================================
    channel_group = app_commands.Group(name="channel", description="Clan channel settings (Leader only).")

    @channel_group.command(name="access", description="Configures channel permission overrides for a custom clan role.")
    @app_commands.describe(
        role_name="The name of your custom clan role.",
        can_view="Allow members with this role to view the private clan channels?",
        can_message="Allow members with this role to send messages in the private text channel?"
    )
    async def channel_access(
        self,
        interaction: discord.Interaction,
        role_name: str,
        can_view: bool,
        can_message: bool
    ) -> None:
        """Sets custom channel permission overrides for a clan role."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            # 1. Fetch user's membership to verify leadership
            exec_member = await get_member_membership(session, guild_id, interaction.user.id)
            if not exec_member:
                await interaction.followup.send("❌ You are not in a clan.", ephemeral=True)
                return
                
            clan = exec_member.clan
            if clan.owner_id != interaction.user.id:
                await interaction.followup.send("❌ Only the Clan Leader can configure channel access overrides.", ephemeral=True)
                return
                
            # 2. Fetch the target role inside the clan
            role_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id).filter(ClanRole.role_name.ilike(role_name))
            )
            target_role = role_result.scalar_one_or_none()
            if not target_role:
                await interaction.followup.send(f"❌ Role '{role_name}' not found in your clan.", ephemeral=True)
                return
                
            discord_role = interaction.guild.get_role(target_role.discord_role_id) if target_role.discord_role_id else None
            if not discord_role:
                # Dynamic self-healing role lookup & creation
                discord_role = discord.utils.get(interaction.guild.roles, name=target_role.role_name)
                if not discord_role:
                    discord_role = discord.utils.get(interaction.guild.roles, name=f"{clan.name} {target_role.role_name}")
                if not discord_role and target_role.hierarchy_level == 100:
                    discord_role = discord.utils.get(interaction.guild.roles, name=f"{clan.name} Leader")
                if not discord_role and target_role.hierarchy_level == 1:
                    discord_role = discord.utils.get(interaction.guild.roles, name=f"{clan.name} Member")

                if not discord_role:
                    role_color = parse_color(target_role.color) if target_role.color else discord.Color.blue()
                    try:
                        discord_role = await interaction.guild.create_role(
                            name=target_role.role_name,
                            color=role_color,
                            reason="Journey Clan Channel Access: Created missing role on Discord"
                        )
                    except discord.Forbidden:
                        await interaction.followup.send("❌ Journey Bot lacks 'Manage Roles' permission to create/link Discord roles.", ephemeral=True)
                        return

                target_role.discord_role_id = discord_role.id
                if discord_role.name != target_role.role_name:
                    try:
                        await discord_role.edit(name=target_role.role_name, reason="Journey Clan Channel Access: Rename role to match exact name.")
                    except discord.Forbidden:
                        pass
                await session.commit()
                
            # 3. Fetch text & voice channels with dynamic fallback
            text_channel = interaction.guild.get_channel(clan.discord_text_channel_id) if clan.discord_text_channel_id else None
            if not text_channel:
                expected_tname = f"💬-{clan.name.lower().replace(' ', '-')}"
                text_channel = discord.utils.get(interaction.guild.text_channels, name=expected_tname)
                if not text_channel:
                    text_channel = next((c for c in interaction.guild.text_channels if clan.name.lower() in c.name.lower()), None)
                if text_channel:
                    clan.discord_text_channel_id = text_channel.id

            voice_channel = interaction.guild.get_channel(clan.discord_voice_channel_id) if clan.discord_voice_channel_id else None
            if not voice_channel:
                expected_vname = f"🔊-{clan.name.lower().replace(' ', '-')}"
                voice_channel = discord.utils.get(interaction.guild.voice_channels, name=expected_vname)
                if not voice_channel:
                    voice_channel = next((c for c in interaction.guild.voice_channels if clan.name.lower() in c.name.lower()), None)
                if voice_channel:
                    clan.discord_voice_channel_id = voice_channel.id

            if text_channel or voice_channel:
                await session.commit()
            
            if not text_channel and not voice_channel:
                await interaction.followup.send(
                    "❌ No private channels are currently set up or linked to your clan.\n"
                    "💡 **Fix:** Ask a server administrator or staff member to run `/clan repair` to generate or link your clan channels!",
                    ephemeral=True
                )
                return
                
            # Apply overrides
            try:
                # Text Channel overrides
                if text_channel:
                    await text_channel.set_permissions(
                        discord_role,
                        view_channel=can_view,
                        send_messages=can_message,
                        read_message_history=can_view,
                        reason=f"Journey Clan Channel Access Config: Modified by Clan Leader."
                    )
                    
                # Voice Channel overrides
                if voice_channel:
                    await voice_channel.set_permissions(
                        discord_role,
                        view_channel=can_view,
                        connect=can_view,
                        reason=f"Journey Clan Channel Access Config: Modified by Clan Leader."
                    )
            except discord.Forbidden:
                await interaction.followup.send("❌ Journey Bot lacks 'Manage Channels' or 'Manage Roles' permissions to modify overrides.", ephemeral=True)
                return
                
            # Log action
            await write_audit_log(
                session,
                clan.id,
                interaction.user.id,
                "channel_access_updated",
                target_role.role_name,
                f"View: {can_view}, Message: {can_message}"
            )
            await session.commit()
            
        status_msg = (
            f"✅ **Permissions Updated!**\n"
            f"Role: **{target_role.role_name}**\n"
            f"👁️ **Can View:** {'🟢 Yes' if can_view else '🔴 No'}\n"
            f"💬 **Can Message:** {'🟢 Yes' if can_message else '🔴 No'}\n"
            f"Applied changes to the private channels."
        )
        await interaction.followup.send(status_msg, ephemeral=True)

    @channel_group.command(name="permissions", description="Displays role and member access permissions for your clan's channels.")
    async def channel_permissions(self, interaction: discord.Interaction) -> None:
        """Displays who can view and send messages in the clan's private channels."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild.id, interaction.user.id)
            if not exec_member:
                await interaction.followup.send("❌ You are not in a clan.", ephemeral=True)
                return

            clan = exec_member.clan

            # Fetch clan roles from DB
            roles_res = await session.execute(select(ClanRole).filter_by(clan_id=clan.id).order_by(ClanRole.hierarchy_level.desc()))
            clan_roles = list(roles_res.scalars())

            # Dynamic channel lookup with fallback & self-healing
            text_channel = guild.get_channel(clan.discord_text_channel_id) if clan.discord_text_channel_id else None
            if not text_channel:
                expected_tname = f"💬-{clan.name.lower().replace(' ', '-')}"
                text_channel = discord.utils.get(guild.text_channels, name=expected_tname)
                if not text_channel:
                    text_channel = next((c for c in guild.text_channels if clan.name.lower() in c.name.lower()), None)
                if text_channel:
                    clan.discord_text_channel_id = text_channel.id

            voice_channel = guild.get_channel(clan.discord_voice_channel_id) if clan.discord_voice_channel_id else None
            if not voice_channel:
                expected_vname = f"🔊-{clan.name.lower().replace(' ', '-')}"
                voice_channel = discord.utils.get(guild.voice_channels, name=expected_vname)
                if not voice_channel:
                    voice_channel = next((c for c in guild.voice_channels if clan.name.lower() in c.name.lower()), None)
                if voice_channel:
                    clan.discord_voice_channel_id = voice_channel.id

            if text_channel or voice_channel:
                await session.commit()

            if not text_channel and not voice_channel:
                await interaction.followup.send(
                    "❌ No private channels are currently set up or linked to your clan.\n"
                    "💡 **Fix:** Ask a server administrator or staff member to run `/clan repair` to generate or link your clan channels!",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title=f"🔒 Channel Access & Permissions — {clan.name}",
                description="Overview of roles and members with access to your clan's private channels.",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )

            # Text Channel Analysis
            if text_channel:
                text_view_allowed = []
                text_msg_allowed = []
                text_denied = []

                # Default role
                def_overwrite = text_channel.overwrites_for(guild.default_role)
                if def_overwrite.view_channel is False:
                    text_denied.append("• `@everyone` (Server Default)")

                for crole in clan_roles:
                    if not crole.discord_role_id:
                        continue
                    d_role = guild.get_role(crole.discord_role_id)
                    if not d_role:
                        continue

                    ow = text_channel.overwrites_for(d_role)
                    can_v = ow.view_channel is True or (ow.view_channel is None and d_role.permissions.administrator)
                    can_m = ow.send_messages is True or (ow.send_messages is None and d_role.permissions.administrator)

                    if can_v:
                        text_view_allowed.append(f"• **{crole.role_name}** ({d_role.mention})")
                    else:
                        text_denied.append(f"• **{crole.role_name}** ({d_role.mention})")

                    if can_m:
                        text_msg_allowed.append(f"• **{crole.role_name}** ({d_role.mention})")

                text_val = (
                    f"**Channel:** {text_channel.mention}\n\n"
                    f"👁️ **Can View & Read Messages:**\n" + ("\n".join(text_view_allowed) if text_view_allowed else "None") + "\n\n"
                    f"💬 **Can Send Messages:**\n" + ("\n".join(text_msg_allowed) if text_msg_allowed else "None") + "\n\n"
                    f"🚫 **Access Denied / Hidden:**\n" + ("\n".join(text_denied) if text_denied else "None")
                )
                embed.add_field(name="💬 Text Channel Access", value=text_val, inline=False)

            # Voice Channel Analysis
            if voice_channel:
                voice_connect_allowed = []
                voice_denied = []

                def_v_overwrite = voice_channel.overwrites_for(guild.default_role)
                if def_v_overwrite.view_channel is False or def_v_overwrite.connect is False:
                    voice_denied.append("• `@everyone` (Server Default)")

                for crole in clan_roles:
                    if not crole.discord_role_id:
                        continue
                    d_role = guild.get_role(crole.discord_role_id)
                    if not d_role:
                        continue

                    ow = voice_channel.overwrites_for(d_role)
                    can_c = (ow.view_channel is True and ow.connect is not False) or d_role.permissions.administrator

                    if can_c:
                        voice_connect_allowed.append(f"• **{crole.role_name}** ({d_role.mention})")
                    else:
                        voice_denied.append(f"• **{crole.role_name}** ({d_role.mention})")

                voice_val = (
                    f"**Channel:** {voice_channel.mention}\n\n"
                    f"🔊 **Can View & Connect:**\n" + ("\n".join(voice_connect_allowed) if voice_connect_allowed else "None") + "\n\n"
                    f"🚫 **Access Denied:**\n" + ("\n".join(voice_denied) if voice_denied else "None")
                )
                embed.add_field(name="🔊 Voice Channel Access", value=voice_val, inline=False)

            embed.set_footer(text="Leaders can modify permissions for custom roles using /clan channel access.")
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ==============================================================================
    # ONBOARDING SETUP COMMANDS GROUP
    # ==============================================================================
    onboarding_group = app_commands.Group(name="onboarding", description="Administrators only: manages Discord Server Onboarding integration.")

    @onboarding_group.command(name="setup", description="Sets up onboarding roles and mapping configurations.")
    @app_commands.default_permissions(administrator=True)
    async def onboarding_setup(self, interaction: discord.Interaction) -> None:
        """Sets up onboarding roles and configuration."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            # Find approved clans
            clans_res = await session.execute(
                select(Clan).filter_by(guild_id=guild_id, approved=True)
            )
            clans = list(clans_res.scalars())
            if not clans:
                await interaction.followup.send("❌ No approved clans found. Please approve clans first.", ephemeral=True)
                return
                
            mapped_count = 0
            for idx, clan in enumerate(clans):
                # Check mapping
                mapping_res = await session.execute(
                    select(ClanOnboarding).filter_by(guild_id=guild_id, clan_id=clan.id)
                )
                mapping = mapping_res.scalar_one_or_none()
                
                if not mapping:
                    role_name = f"{clan.name} Applicant"
                    # Try to find existing role in guild to avoid duplicates
                    role = discord.utils.get(interaction.guild.roles, name=role_name)
                    if not role:
                        try:
                            role = await interaction.guild.create_role(
                                name=role_name,
                                color=discord.Color.light_grey(),
                                reason=f"Journey Clan Onboarding Setup: Applicant trigger role."
                            )
                        except discord.Forbidden:
                            await interaction.followup.send("❌ Journey Bot lacks 'Manage Roles' permission to create onboarding applicant roles.", ephemeral=True)
                            return
                            
                    mapping = ClanOnboarding(
                        guild_id=guild_id,
                        clan_id=clan.id,
                        discord_role_id=role.id,
                        display_order=idx,
                        enabled=True
                    )
                    session.add(mapping)
                    mapped_count += 1
                else:
                    role = interaction.guild.get_role(mapping.discord_role_id)
                    if not role:
                        role_name = f"{clan.name} Applicant"
                        role = discord.utils.get(interaction.guild.roles, name=role_name)
                        if not role:
                            try:
                                role = await interaction.guild.create_role(
                                    name=role_name,
                                    color=discord.Color.light_grey(),
                                    reason=f"Journey Clan Onboarding Setup: Applicant trigger role."
                                )
                            except discord.Forbidden:
                                await interaction.followup.send("❌ Journey Bot lacks 'Manage Roles' permission to create onboarding applicant roles.", ephemeral=True)
                                return
                        mapping.discord_role_id = role.id
                    mapping.enabled = True
                    
            await session.commit()
            
        msg = (
            f"✅ **Onboarding Integration Configured!**\n"
            f"Setup/Synced applicant trigger roles for approved clans.\n\n"
            f"📋 **Next Steps for Server Admins:**\n"
            f"1. Go to **Server Settings** -> **Onboarding** -> **Questions**.\n"
            f"2. Add a question: *'What clan would you like to apply to?'*\n"
            f"3. For each option, link it to the respective **Applicant** role created by the bot:\n"
            + "\n".join([f"   - **Choice:** `{c.name}` ➡️ **Role:** `{c.name} Applicant`" for c in clans]) +
            f"\n   - **Choice:** `No Clan` ➡️ (No role linked)\n"
            f"4. The bot will automatically listen to onboarding selections and create pending applications!"
        )
        await interaction.followup.send(msg, ephemeral=True)

    @onboarding_group.command(name="refresh", description="Refreshes onboarding roles mapping for newly approved clans.")
    @app_commands.default_permissions(administrator=True)
    async def onboarding_refresh(self, interaction: discord.Interaction) -> None:
        """Alias to onboarding setup to sync mappings."""
        await self.onboarding_setup.callback(self, interaction)

    @onboarding_group.command(name="disable", description="Disables onboarding integration mapping.")
    @app_commands.default_permissions(administrator=True)
    async def onboarding_disable(self, interaction: discord.Interaction) -> None:
        """Disables the onboarding trigger mappings."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            await session.execute(
                update(ClanOnboarding).filter_by(guild_id=guild_id).values(enabled=False)
            )
            await session.commit()
        await interaction.followup.send("✅ Onboarding integration has been disabled. User selections will no longer trigger applications.", ephemeral=True)

    @onboarding_group.command(name="status", description="Displays status of onboarding roles mapping configuration.")
    @app_commands.default_permissions(administrator=True)
    async def onboarding_status(self, interaction: discord.Interaction) -> None:
        """Displays mapped roles status."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with get_db_session() as session:
            mappings_res = await session.execute(
                select(ClanOnboarding).filter_by(guild_id=guild_id)
            )
            mappings = list(mappings_res.scalars())
            
            clans_res = await session.execute(
                select(Clan).filter_by(guild_id=guild_id, approved=True)
            )
            clans = list(clans_res.scalars())
            
        if not mappings:
            await interaction.followup.send("ℹ️ Onboarding integration is not set up yet. Run `/clan onboarding setup` to configure.", ephemeral=True)
            return
            
        enabled = any(m.enabled for m in mappings)
        linked = 0
        missing = 0
        mapping_details = []
        for m in mappings:
            clan = next((c for c in clans if c.id == m.clan_id), None)
            clan_name = clan.name if clan else f"Unknown Clan (ID: {m.clan_id})"
            role = interaction.guild.get_role(m.discord_role_id)
            if role:
                linked += 1
                role_status = f"✅ `{role.name}`"
            else:
                missing += 1
                role_status = "❌ Missing Discord Role"
                
            mapping_details.append(f"- **{clan_name}**: {role_status} (Order: {m.display_order})")
            
        msg = (
            f"📊 **Onboarding Integration Status**\n"
            f"**Enabled:** {'🟢 Yes' if enabled else '🔴 No'}\n"
            f"**Approved Clans:** {len(clans)}\n"
            f"**Linked Mapping Roles:** {linked}\n"
            f"**Missing Discord Roles:** {missing}\n\n"
            f"📋 **Mappings:**\n" + "\n".join(mapping_details)
        )
        await interaction.followup.send(msg, ephemeral=True)

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
            if not exec_member.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
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

        await interaction.response.defer()
        guild_id = interaction.guild_id
        
        async with get_db_session() as session:
            clan = None
            if target is None:
                # Fetch caller stats and their clan
                exec_member = await get_member_membership(session, guild_id, interaction.user.id)
                if exec_member:
                    clan = exec_member.clan
                else:
                    await interaction.followup.send("❌ You are not currently in a clan. Use `/clan create` to register one!", ephemeral=True)
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
                        await interaction.followup.send("❌ That user is not in a clan.", ephemeral=True)
                        return
                else:
                    # Search by name
                    clan_result = await session.execute(
                        select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(target))
                    )
                    clan = clan_result.scalars().first()
                    if not clan:
                        await interaction.followup.send(f"❌ No clan found with name '{target}'.", ephemeral=True)
                        return

            if not clan:
                await interaction.followup.send("❌ No clan found or you are not currently in a clan. Use `/clan create` to register one!", ephemeral=True)
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
            
        # Resolve names using gateway queries to avoid HTTP REST API rate limit bans
        import asyncio
        resolved_members = []
        name_map = {}
        try:
            if interaction.guild:
                uncached_ids = [uid for uid in [clan.owner_id] + [m.user_id for m in members] if not interaction.guild.get_member(uid)]
                if uncached_ids:
                    try:
                        resolved_members = await asyncio.wait_for(
                            interaction.guild.query_members(user_ids=uncached_ids, cache=True),
                            timeout=1.5
                        )
                    except Exception:
                        pass

                for uid in [clan.owner_id] + [m.user_id for m in members]:
                    m_obj = interaction.guild.get_member(uid)
                    if m_obj:
                        name_map[uid] = m_obj.display_name
                for m_obj in resolved_members:
                    name_map[m_obj.id] = m_obj.display_name

            uncached_remaining = [uid for uid in [clan.owner_id] + [m.user_id for m in members] if uid not in name_map]
            if uncached_remaining and len(uncached_remaining) <= 5:
                async def fetch_and_cache(uid: int):
                    try:
                        user_obj = await interaction.client.fetch_user(uid)
                        name_map[uid] = user_obj.display_name
                    except Exception:
                        pass
                await asyncio.gather(*[fetch_and_cache(uid) for uid in uncached_remaining])
        except Exception as e:
            logger.warning(f"Member name resolution non-fatal warning in clan_info: {e}")

        leader_name = name_map.get(clan.owner_id)
        if not leader_name:
            leader_user = interaction.client.get_user(clan.owner_id) if interaction.client else None
            if leader_user:
                leader_name = leader_user.display_name
            else:
                leader_name = f"ID: {clan.owner_id}"

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
            name = name_map.get(m.user_id)
            if not name and interaction.client:
                user_obj = interaction.client.get_user(m.user_id)
                if user_obj:
                    name = user_obj.display_name
            
            role_suffix = ""
            if m.role:
                if m.role.hierarchy_level == 100:
                    role_suffix = f" ({m.role.role_name}) 👑"
                else:
                    role_suffix = f" ({m.role.role_name})"
                    
            if name:
                members_list.append(f"{idx+1}. <@{m.user_id}> ({name}){role_suffix}")
            else:
                members_list.append(f"{idx+1}. <@{m.user_id}>{role_suffix}")
            
        displayed_lines = []
        current_len = 0
        total_members = len(members_list)
        for idx, entry in enumerate(members_list):
            remaining = total_members - idx
            suffix = f"\n*...and {remaining} more members*"
            if current_len + len(entry) + len(suffix) > 950:
                displayed_lines.append(f"*...and {remaining} more members*")
                break
            displayed_lines.append(entry)
            current_len += len(entry) + 1

        members_str = "\n".join(displayed_lines) if displayed_lines else "*No members.*"
        embed.add_field(name=f"👥 Members ({len(members)})", value=members_str, inline=False)
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="description", description="Updates your clan's description.")
    @app_commands.describe(text="The new description (max 256 characters, or leave blank to clear).")
    async def clan_description(self, interaction: discord.Interaction, text: str | None = None) -> None:
        """Updates the description of the caller's clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        if text and len(text) > 256:
            await interaction.response.send_message("❌ Description cannot exceed 256 characters.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            membership = await get_member_membership(session, guild_id, interaction.user.id)
            if not membership:
                await interaction.response.send_message("❌ You are not currently in a clan.", ephemeral=True)
                return
            if not membership.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
                return

            clan = membership.clan
            
            # Check permissions
            is_owner = clan.owner_id == interaction.user.id
            has_perm = membership.role and membership.role.permissions and getattr(membership.role.permissions, "can_edit_clan_description", False)
            
            if not (is_owner or has_perm):
                await interaction.response.send_message("❌ You do not have permission to edit the clan description.", ephemeral=True)
                return

            old_desc = clan.description
            clan.description = text
            
            # Log action
            await write_audit_log(
                session,
                clan.id,
                interaction.user.id,
                "description_updated",
                old_desc,
                text
            )
            await session.commit()

        desc_msg = f"updated to: **{text}**" if text else "cleared."
        await interaction.response.send_message(f"✅ Clan description has been {desc_msg}")

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
            if not exec_member.clan.approved:
                await interaction.response.send_message("❌ This clan is pending Staff Approval and its features are currently locked.", ephemeral=True)
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
            
            # Fetch channels before deleting DB records
            text_chan = interaction.guild.get_channel(clan.discord_text_channel_id) if clan.discord_text_channel_id else None
            voice_chan = interaction.guild.get_channel(clan.discord_voice_channel_id) if clan.discord_voice_channel_id else None
            
            # Fetch all members to strip roles later
            members_result = await session.execute(
                select(ClanMember).filter_by(clan_id=clan.id)
            )
            members_list = list(members_result.scalars())
            
            await session.execute(delete(ClanMember).filter_by(clan_id=clan.id))
            await session.execute(delete(Clan).filter_by(id=clan.id))
            await session.commit()
            
            # Delete private channels
            if text_chan:
                try:
                    await text_chan.delete(reason="Journey Clan Disband")
                except discord.Forbidden:
                    pass
            if voice_chan:
                try:
                    await voice_chan.delete(reason="Journey Clan Disband")
                except discord.Forbidden:
                    pass
            
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

    @app_commands.command(name="force_disband", description="Forcibly disbands any clan (Staff Only).")
    @app_commands.describe(name="The name of the clan to forcibly disband.")
    async def clan_force_disband(self, interaction: discord.Interaction, name: str) -> None:
        """Forcibly disbands any clan in the server."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return
            
        # Verify Staff permissions
        perms = interaction.user.guild_permissions
        is_staff = perms.administrator or perms.manage_guild or perms.manage_roles or (interaction.guild.owner_id == interaction.user.id)
        if not is_staff:
            await interaction.response.send_message("❌ Only server administrators or staff can force disband clans.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        async with get_db_session() as session:
            clan_result = await session.execute(
                select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(name))
            )
            clan = clan_result.scalars().first()
            if not clan:
                await interaction.response.send_message(f"❌ No clan found with name '{name}'.", ephemeral=True)
                return
                
            clan_name = clan.name
            
            roles_result = await session.execute(
                select(ClanRole).filter_by(clan_id=clan.id)
            )
            clan_roles = list(roles_result.scalars())
            
            # Fetch channels before deleting DB records
            text_chan = interaction.guild.get_channel(clan.discord_text_channel_id) if clan.discord_text_channel_id else None
            voice_chan = interaction.guild.get_channel(clan.discord_voice_channel_id) if clan.discord_voice_channel_id else None
            
            # Fetch all members to strip roles later
            members_result = await session.execute(
                select(ClanMember).filter_by(clan_id=clan.id)
            )
            members_list = list(members_result.scalars())
            
            await session.execute(delete(ClanMember).filter_by(clan_id=clan.id))
            await session.execute(delete(Clan).filter_by(id=clan.id))
            await session.commit()
            
            # Delete private channels
            if text_chan:
                try:
                    await text_chan.delete(reason="Journey Clan Force Disband by Staff")
                except discord.Forbidden:
                    pass
            if voice_chan:
                try:
                    await voice_chan.delete(reason="Journey Clan Force Disband by Staff")
                except discord.Forbidden:
                    pass
            
            # Delete discord roles
            for r in clan_roles:
                if r.discord_role_id:
                    d_role = interaction.guild.get_role(r.discord_role_id)
                    if d_role:
                        try:
                            await d_role.delete(reason="Journey Clan Force Disband by Staff")
                        except discord.Forbidden:
                            pass
                            
        await interaction.response.send_message(f"💥 Clan **{clan_name}** has been forcibly disbanded by staff.")

    @app_commands.command(name="repair", description="Audits & repairs missing roles/channels for approved clans.")
    @app_commands.describe(name="Optional: Target a specific clan to repair (leave blank for your clan or all approved clans for staff).")
    async def clan_repair(self, interaction: discord.Interaction, name: str | None = None) -> None:
        """Audits and repairs missing roles/channels for approved clans (usable by Staff and Clan Leaders)."""
        if interaction.guild_id is None or interaction.guild is None:
            await interaction.response.send_message("❌ This command must be run inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        async with get_db_session() as session:
            exec_member = await get_member_membership(session, guild.id, interaction.user.id)
            is_clan_leader = bool(exec_member and exec_member.clan and exec_member.clan.owner_id == interaction.user.id)

            perms = interaction.user.guild_permissions
            is_staff = perms.administrator or perms.manage_guild or perms.manage_roles or (guild.owner_id == interaction.user.id)

            if not is_staff and not is_clan_leader:
                await interaction.followup.send("❌ Only Server Staff or Clan Leaders can run clan repairs.", ephemeral=True)
                return

            query = select(Clan).filter_by(guild_id=guild.id, approved=True)

            if not is_staff and is_clan_leader:
                query = query.filter_by(id=exec_member.clan.id)
            elif name:
                query = query.filter(Clan.name.ilike(name))

            clans_res = await session.execute(query)
            clans = list(clans_res.scalars())

            if not clans:
                msg = f"❌ No approved clan found matching '{name}'." if name else "❌ No approved clans found for repair."
                await interaction.followup.send(msg, ephemeral=True)
                return

            audit_results = []
            for clan in clans:
                audit_info = await audit_clan_health(session, guild, clan)
                audit_results.append(audit_info)

            embed = build_repair_audit_embed(guild, audit_results)
            view = ClanRepairView(interaction.user.id, audit_results)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ==============================================================================
# CLAN REPAIR AUDIT & EXECUTION HELPERS
# ==============================================================================

async def audit_clan_health(session: AsyncSession, guild: discord.Guild, clan: Clan) -> dict:
    """Audits the Discord and DB health of an approved clan."""
    roles_res = await session.execute(select(ClanRole).filter_by(clan_id=clan.id))
    db_roles = list(roles_res.scalars())
    leader_db_role = next((r for r in db_roles if r.hierarchy_level == 100), None)
    member_db_role = next((r for r in db_roles if r.hierarchy_level == 1), None)

    leader_d_role = guild.get_role(leader_db_role.discord_role_id) if (leader_db_role and leader_db_role.discord_role_id) else None
    if not leader_d_role and leader_db_role:
        leader_d_role = discord.utils.get(guild.roles, name=leader_db_role.role_name)
        if not leader_d_role:
            leader_d_role = discord.utils.get(guild.roles, name=f"{clan.name} {leader_db_role.role_name}")
        if not leader_d_role:
            leader_d_role = discord.utils.get(guild.roles, name=f"{clan.name} Leader")

    member_d_role = guild.get_role(member_db_role.discord_role_id) if (member_db_role and member_db_role.discord_role_id) else None
    if not member_d_role and member_db_role:
        member_d_role = discord.utils.get(guild.roles, name=member_db_role.role_name)
        if not member_d_role:
            member_d_role = discord.utils.get(guild.roles, name=f"{clan.name} {member_db_role.role_name}")
        if not member_d_role:
            member_d_role = discord.utils.get(guild.roles, name=f"{clan.name} Member")

    role_mismatches = False
    for r in db_roles:
        d_r = guild.get_role(r.discord_role_id) if r.discord_role_id else None
        if not d_r:
            d_r = discord.utils.get(guild.roles, name=r.role_name)
        if not d_r or d_r.name != r.role_name:
            role_mismatches = True
            break

    # Onboarding Applicant Role
    from bot.models.clan import ClanOnboarding
    onboarding_res = await session.execute(
        select(ClanOnboarding).filter_by(guild_id=guild.id, clan_id=clan.id)
    )
    onboarding_mapping = onboarding_res.scalar_one_or_none()
    onboarding_d_role = None
    if onboarding_mapping and onboarding_mapping.discord_role_id:
        onboarding_d_role = guild.get_role(onboarding_mapping.discord_role_id)
    if not onboarding_d_role:
        onboarding_d_role = discord.utils.get(guild.roles, name=f"{clan.name} Applicant")

    owner_member = guild.get_member(clan.owner_id)
    owner_has_role = bool(owner_member and leader_d_role and leader_d_role in owner_member.roles)

    text_chan = guild.get_channel(clan.discord_text_channel_id) if clan.discord_text_channel_id else None
    if not text_chan:
        text_chan = discord.utils.get(guild.text_channels, name=f"💬-{clan.name.lower().replace(' ', '-')}")

    voice_chan = guild.get_channel(clan.discord_voice_channel_id) if clan.discord_voice_channel_id else None
    if not voice_chan:
        voice_chan = discord.utils.get(guild.voice_channels, name=f"🔊-{clan.name.lower().replace(' ', '-')}")

    category = guild.get_channel(clan.discord_category_id) if clan.discord_category_id else None

    needs_repair = (
        not leader_d_role or
        not member_d_role or
        not onboarding_d_role or
        not owner_has_role or
        not text_chan or
        not voice_chan or
        role_mismatches
    )

    return {
        "clan": clan,
        "db_roles": db_roles,
        "leader_db_role": leader_db_role,
        "member_db_role": member_db_role,
        "leader_d_role": leader_d_role,
        "member_d_role": member_d_role,
        "onboarding_mapping": onboarding_mapping,
        "onboarding_d_role": onboarding_d_role,
        "owner_has_role": owner_has_role,
        "text_chan": text_chan,
        "voice_chan": voice_chan,
        "category": category,
        "needs_repair": needs_repair
    }


async def execute_clan_repair(
    session: AsyncSession,
    guild: discord.Guild,
    audit_data: dict,
    category_name: str = "🏆 CLAN CATEGORY"
) -> dict:
    """Repairs missing roles, permissions, channels, and owner assignments for a clan."""
    clan = audit_data["clan"]
    db_roles = audit_data.get("db_roles", [])
    if not db_roles:
        roles_res = await session.execute(select(ClanRole).filter_by(clan_id=clan.id))
        db_roles = list(roles_res.scalars())

    summary = {"roles_created": 0, "text_created": False, "voice_created": False, "owner_assigned": False}

    # 1. Sync & repair all custom clan roles on Discord (exact role name matching)
    active_discord_role_ids = []
    leader_d_role = None
    member_d_role = None

    for r in db_roles:
        expected_name = r.role_name
        d_role = guild.get_role(r.discord_role_id) if r.discord_role_id else None
        if not d_role:
            d_role = discord.utils.get(guild.roles, name=expected_name)
        if not d_role:
            d_role = discord.utils.get(guild.roles, name=f"{clan.name} {r.role_name}")
        if not d_role and r.hierarchy_level == 100:
            d_role = discord.utils.get(guild.roles, name=f"{clan.name} Leader")
        if not d_role and r.hierarchy_level == 1:
            d_role = discord.utils.get(guild.roles, name=f"{clan.name} Member")

        if not d_role:
            role_color = parse_color(r.color) if r.color else discord.Color.blue()
            d_role = await guild.create_role(
                name=expected_name,
                color=role_color,
                mentionable=True,
                reason=f"Journey Clan Repair: Created missing role '{r.role_name}'."
            )
            summary["roles_created"] += 1
        else:
            try:
                await d_role.edit(name=expected_name, mentionable=True, reason="Journey Clan Repair: Ensure role is mentionable and matches custom name.")
            except discord.Forbidden:
                pass

        r.discord_role_id = d_role.id
        active_discord_role_ids.append(d_role.id)

        if r.hierarchy_level == 100:
            leader_d_role = d_role
        elif r.hierarchy_level == 1:
            member_d_role = d_role

    # Onboarding Applicant Role
    from bot.models.clan import ClanOnboarding
    onboarding_d_role = audit_data["onboarding_d_role"]
    if not onboarding_d_role:
        onboarding_d_role = await guild.create_role(
            name=f"{clan.name} Applicant",
            color=discord.Color.light_grey(),
            reason="Journey Clan Repair: Missing Onboarding Applicant role."
        )
        summary["roles_created"] += 1

    onboarding_mapping = audit_data["onboarding_mapping"]
    if not onboarding_mapping:
        onboarding_mapping = ClanOnboarding(
            guild_id=guild.id,
            clan_id=clan.id,
            discord_role_id=onboarding_d_role.id,
            enabled=True
        )
        session.add(onboarding_mapping)
    else:
        onboarding_mapping.discord_role_id = onboarding_d_role.id
        onboarding_mapping.enabled = True

    # Clean up old duplicate/legacy Discord roles for this clan
    legacy_role_names = [f"{clan.name} Leader", f"{clan.name} Member"] + [f"{clan.name} {r.role_name}" for r in db_roles]
    for g_role in guild.roles:
        if g_role.id not in active_discord_role_ids and g_role.id != onboarding_d_role.id:
            if g_role.name in legacy_role_names:
                try:
                    await g_role.delete(reason="Journey Clan Repair: Clean up duplicate/legacy clan role.")
                except discord.Forbidden:
                    pass

    # Assign correct Discord roles to ALL clan members
    members_res = await session.execute(select(ClanMember).filter_by(clan_id=clan.id))
    clan_members = list(members_res.scalars())
    for m in clan_members:
        try:
            await sync_discord_roles(guild, m.user_id, m.role_id, db_roles)
        except Exception:
            pass

    owner_discord_member = guild.get_member(clan.owner_id)
    if owner_discord_member and leader_d_role and leader_d_role not in owner_discord_member.roles:
        try:
            await owner_discord_member.add_roles(leader_d_role, reason="Journey Clan Repair: Owner role assignment.")
            summary["owner_assigned"] = True
        except discord.Forbidden:
            pass

    # 2. Category & Private Channels
    category = audit_data["category"]
    if not category:
        category = discord.utils.get(guild.categories, name=category_name)
        if not category:
            category = await guild.create_category(name=category_name, reason="Journey Clan Category Setup")
    clan.discord_category_id = category.id

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me.top_role: discord.PermissionOverwrite(view_channel=True, manage_channels=True, send_messages=True)
    }

    text_chan = audit_data["text_chan"]
    if not text_chan:
        text_chan = await guild.create_text_channel(
            name=f"💬-{clan.name.lower().replace(' ', '-')}",
            category=category,
            overwrites=overwrites,
            topic=f"Official private channel for {clan.name}.",
            reason="Journey Clan Repair: Create missing text channel."
        )
        summary["text_created"] = True
    clan.discord_text_channel_id = text_chan.id

    voice_chan = audit_data["voice_chan"]
    if not voice_chan:
        voice_chan = await guild.create_voice_channel(
            name=f"🔊-{clan.name.lower().replace(' ', '-')}",
            category=category,
            overwrites=overwrites,
            reason="Journey Clan Repair: Create missing voice channel."
        )
        summary["voice_created"] = True
    clan.discord_voice_channel_id = voice_chan.id

    # Grant text & voice channel access and ping permissions to ALL custom roles in the clan, and repair role gradients
    for r in db_roles:
        d_r = guild.get_role(r.discord_role_id) if r.discord_role_id else None
        if not d_r:
            d_r = discord.utils.get(guild.roles, name=r.role_name)
        if d_r:
            is_leader = r.hierarchy_level == 100
            try:
                if text_chan:
                    await text_chan.set_permissions(
                        d_r,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        mention_everyone=True,
                        manage_messages=is_leader,
                        reason="Journey Clan Repair: Grant text channel access & pings to clan role"
                    )
                if voice_chan:
                    await voice_chan.set_permissions(
                        d_r,
                        view_channel=True,
                        connect=True,
                        speak=True,
                        reason="Journey Clan Repair: Grant voice channel access to clan role"
                    )
            except discord.Forbidden:
                pass

            # Inspect and repair role gradients via raw REST API
            c1_val = r.color
            c2_val = getattr(r, "color2", None)
            c1_int = None
            c2_int = None
            if c1_val:
                try:
                    c1_int = int(c1_val.strip("#"), 16)
                except ValueError:
                    c1_int = None
            if c2_val:
                try:
                    c2_int = int(c2_val.strip("#"), 16)
                except ValueError:
                    c2_int = None

            if c1_int is not None:
                route = Route('PATCH', '/guilds/{guild_id}/roles/{role_id}', guild_id=guild.id, role_id=d_r.id)
                json_payload = {
                    "name": r.role_name,
                    "mentionable": getattr(r, "is_mentionable", True)
                }
                if c1_int is not None and c2_int is not None:
                    json_payload["color"] = c1_int
                    json_payload["secondary_color"] = c2_int
                    json_payload["colors"] = {
                        "primary_color": c1_int,
                        "secondary_color": c2_int,
                        "tertiary_color": None
                    }
                    json_payload["role_colors"] = {
                        "primary_color": c1_int,
                        "secondary_color": c2_int,
                        "tertiary_color": None
                    }
                else:
                    json_payload["color"] = c1_int

                try:
                    await guild.client.http.request(route, json=json_payload, reason="Journey Clan Repair: Repair role gradient and pings")
                except Exception:
                    pass

    await write_audit_log(session, clan.id, guild.owner_id, "clan_repaired")
    await session.commit()
    return summary


def build_repair_audit_embed(guild: discord.Guild, audit_results: list[dict]) -> discord.Embed:
    """Builds a formatted Embed summarizing clan health audit results."""
    degraded_count = sum(1 for res in audit_results if res["needs_repair"])
    total_count = len(audit_results)

    embed = discord.Embed(
        title="🛠️ Clan System Diagnostic & Repair Dashboard",
        description=f"Scanned **{total_count}** approved clan(s). Found **{degraded_count}** clan(s) needing role/channel repairs.",
        color=discord.Color.orange() if degraded_count > 0 else discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    for item in audit_results[:10]: # cap at 10 to avoid field length limit
        clan = item["clan"]
        leader_r = "🟢" if item["leader_d_role"] else "🔴 Missing"
        member_r = "🟢" if item["member_d_role"] else "🔴 Missing"
        applicant_r = "🟢" if item["onboarding_d_role"] else "🔴 Missing"
        owner_r = "🟢" if item["owner_has_role"] else "🔴 Missing"
        text_c = "🟢" if item["text_chan"] else "🔴 Missing"
        voice_c = "🟢" if item["voice_chan"] else "🔴 Missing"

        status = "🟢 Healthy" if not item["needs_repair"] else "⚠️ Degraded (Needs Repair)"

        val = (
            f"**Status:** {status}\n"
            f"• **Leader Role:** {leader_r} | **Member Role:** {member_r} | **Applicant Role:** {applicant_r}\n"
            f"• **Owner Assigned:** {owner_r}\n"
            f"• **Text Channel:** {text_c} | **Voice Channel:** {voice_c}"
        )
        embed.add_field(name=f"🛡️ {clan.name}", value=val, inline=False)

    if total_count > 10:
        embed.set_footer(text=f"And {total_count - 10} more clans...")

    return embed


class ClanRepairView(discord.ui.View):
    def __init__(self, staff_id: int, audit_results: list[dict]):
        super().__init__(timeout=180.0)
        self.staff_id = staff_id
        self.audit_results = audit_results
        self.degraded = [r for r in audit_results if r["needs_repair"]]

        # Disable repair button if everything is already healthy
        if not self.degraded:
            self.repair_button.disabled = True
            self.repair_button.label = "All Clans Healthy ✅"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.staff_id:
            await interaction.response.send_message("❌ Only the staff member who initiated this audit can interact.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔧 Repair Missing Roles & Channels", style=discord.ButtonStyle.primary, custom_id="clan_repair_execute")
    async def repair_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        repaired_summaries = []
        async with get_db_session() as session:
            for item in self.degraded:
                try:
                    summary = await execute_clan_repair(session, guild, item)
                    repaired_summaries.append((item["clan"].name, summary))
                except Exception as e:
                    logger.error(f"Failed to repair clan {item['clan'].name}: {e}")

            # Re-audit to update embed
            updated_audit = []
            for item in self.audit_results:
                fresh_audit = await audit_clan_health(session, guild, item["clan"])
                updated_audit.append(fresh_audit)

        # Update view
        self.repair_button.disabled = True
        self.repair_button.label = "Repaired ✅"
        for child in self.children:
            child.disabled = True

        updated_embed = build_repair_audit_embed(guild, updated_audit)
        await interaction.edit_original_response(embed=updated_embed, view=self)

        # Report detailed repair log
        lines = []
        for cname, s in repaired_summaries:
            lines.append(f"• **{cname}**: Created {s['roles_created']} Role(s), Text: {'✅' if s['text_created'] else 'Already exists'}, Voice: {'✅' if s['voice_created'] else 'Already exists'}, Owner Assigned: {'✅' if s['owner_assigned'] else 'OK'}")

        report = "🎉 **Clan Repair Complete!**\n" + ("\n".join(lines) if lines else "No changes required.")
        await interaction.followup.send(report, ephemeral=True)

    @discord.ui.button(label="❌ Dismiss", style=discord.ButtonStyle.secondary, custom_id="clan_repair_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)



class Clans(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(ClanGroup())

    async def cog_unload(self):
        # Remove tree group command when reloading
        self.bot.tree.remove_command("clan")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Clans(bot))
