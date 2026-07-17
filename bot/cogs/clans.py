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
from bot.models.clan import Clan
from bot.models.user import UserGuildStats

logger = logging.getLogger("Journey.Clans")

class JoinView(discord.ui.View):
    def __init__(self, target_member: discord.Member, clan_id: int, clan_name: str):
        super().__init__(timeout=60)
        self.target_member = target_member
        self.clan_id = clan_id
        self.clan_name = clan_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_member.id:
            await interaction.response.send_message("❌ This invitation is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Join Clan", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Ensure target member and their stats exist safely
            target_stats = await DatabaseService.get_or_create_stats(session, interaction.guild_id, self.target_member.id)
            
            if target_stats.clan_id is not None:
                await interaction.response.send_message("❌ You are already in a clan! Leave your current clan first.", ephemeral=True)
                return
                
            # Verify clan still exists
            clan_result = await session.execute(select(Clan).filter_by(id=self.clan_id))
            clan = clan_result.scalar_one_or_none()
            if not clan:
                await interaction.response.send_message("❌ This clan no longer exists.", ephemeral=True)
                return
                
            # Join clan
            target_stats.clan_id = self.clan_id
            await session.commit()
            
        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"✅ **{self.target_member.display_name}** has joined the clan **{self.clan_name}**!", view=self)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"❌ Invitation to **{self.clan_name}** was declined.", view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()

# Configure the Clan group command to be user-installable (personal command)
@app_commands.allowed_installs(guilds=False, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
class ClanGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="clan", description="Clan management commands.")

    @app_commands.command(name="create", description="Creates a new clan.")
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
        """Creates a new clan in the guild."""
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
        
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Safely create user stats
            user_stats = await DatabaseService.get_or_create_stats(session, guild_id, user_id)
            
            if user_stats.clan_id is not None:
                await interaction.response.send_message("❌ You are already in a clan! Leave your current clan first.", ephemeral=True)
                return
                
            # Check if name is unique
            name_check = await session.execute(
                select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(name))
            )
            if name_check.scalar_one_or_none():
                await interaction.response.send_message("❌ A clan with that name already exists in this server.", ephemeral=True)
                return
                
            # Create clan
            clan = Clan(
                guild_id=guild_id,
                owner_id=user_id,
                name=name,
                description=description
            )
            session.add(clan)
            await session.flush()
            
            user_stats.clan_id = clan.id
            await session.commit()
            
        await interaction.response.send_message(f"🎉 Clan **{name}** has been successfully created! You are the leader.")

    @app_commands.command(name="add", description="Invites a member to join your clan.")
    @app_commands.describe(member="The member you want to add.")
    async def clan_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ) -> None:
        """Invites a user to your clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        if member.bot:
            await interaction.response.send_message("❌ You cannot add bots to a clan.", ephemeral=True)
            return
            
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You are already in your clan.", ephemeral=True)
            return

        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Ensure caller exists in DB
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You must be in a clan to invite members.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            
            is_leader = clan.owner_id == interaction.user.id
            is_vice = caller_stats.is_vice_captain
            
            if not is_leader and not is_vice:
                await interaction.response.send_message("❌ Only the clan leader or a vice-captain can invite members.", ephemeral=True)
                return
                
            # Ensure target exists in DB
            target_stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            if target_stats.clan_id is not None:
                await interaction.response.send_message(f"❌ **{member.display_name}** is already in a clan.", ephemeral=True)
                return
                
            clan_id = clan.id
            clan_name = clan.name

        # Send invitation view
        view = JoinView(target_member=member, clan_id=clan_id, clan_name=clan_name)
        await interaction.response.send_message(
            content=f"✉️ {member.mention}, you have been invited to join the clan **{clan_name}** by **{interaction.user.display_name}**!",
            view=view
        )

    @app_commands.command(name="name", description="Renames your clan.")
    @app_commands.describe(new_name="The new name for your clan.")
    async def clan_name(
        self,
        interaction: discord.Interaction,
        new_name: str
    ) -> None:
        """Renames the caller's clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        if len(new_name) > 64:
            await interaction.response.send_message("❌ Clan name cannot exceed 64 characters.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Ensure caller exists
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            if clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan leader can rename the clan.", ephemeral=True)
                return
                
            # Check if name is unique
            name_check = await session.execute(
                select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(new_name)).filter(Clan.id != clan.id)
            )
            if name_check.scalar_one_or_none():
                await interaction.response.send_message("❌ A clan with that name already exists.", ephemeral=True)
                return
                
            old_name = clan.name
            clan.name = new_name
            await session.commit()
            
        await interaction.response.send_message(f"✅ Clan **{old_name}** has been renamed to **{new_name}**.")

    @app_commands.command(name="description", description="Changes your clan's description.")
    @app_commands.describe(new_description="The new description.")
    async def clan_description(
        self,
        interaction: discord.Interaction,
        new_description: str
    ) -> None:
        """Updates the description of the caller's clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        if len(new_description) > 256:
            await interaction.response.send_message("❌ Description cannot exceed 256 characters.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Ensure caller exists
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            if clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan leader can change the description.", ephemeral=True)
                return
                
            clan.description = new_description
            await session.commit()
            
        await interaction.response.send_message(f"✅ Clan description updated successfully.")

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
        from bot.services.database_service import DatabaseService
        
        async with get_db_session() as session:
            clan = None
            if target is None:
                # Ensure caller exists safely
                user_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
                if user_stats.clan_id is not None:
                    clan_result = await session.execute(select(Clan).filter_by(id=user_stats.clan_id))
                    clan = clan_result.scalar_one_or_none()
                if not clan:
                    await interaction.response.send_message("❌ You are not currently in a clan. Use `/clan create` to start one!", ephemeral=True)
                    return
            else:
                # 1. Check if target is a mention or User ID
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
                    user_stats = await DatabaseService.get_or_create_stats(session, guild_id, user_id)
                    if user_stats.clan_id is not None:
                        clan_result = await session.execute(select(Clan).filter_by(id=user_stats.clan_id))
                        clan = clan_result.scalar_one_or_none()
                    if not clan:
                        await interaction.response.send_message("❌ That user is not in a clan.", ephemeral=True)
                        return
                else:
                    # 2. Search by clan name
                    clan_result = await session.execute(
                        select(Clan).filter_by(guild_id=guild_id).filter(Clan.name.ilike(target))
                    )
                    clan = clan_result.scalar_one_or_none()
                    if not clan:
                        await interaction.response.send_message(f"❌ No clan found with name '{target}'.", ephemeral=True)
                        return

            # Eager load members
            members_result = await session.execute(
                select(UserGuildStats).filter_by(clan_id=clan.id)
            )
            members = list(members_result.scalars())

        # Build Embed Info Sheet
        leader_member = interaction.guild.get_member(clan.owner_id) if interaction.guild else None
        if not leader_member:
            try:
                leader_member = await interaction.client.fetch_user(clan.owner_id)
            except Exception:
                pass
        leader_name = leader_member.display_name if leader_member else f"ID: {clan.owner_id}"
        
        embed = discord.Embed(
            title=f"🛡️ Clan: {clan.name}",
            description=clan.description or "*No description set.*",
            color=discord.Color.blue()
        )
        embed.add_field(name="👑 Leader", value=f"<@{clan.owner_id}> ({leader_name})", inline=True)
        embed.add_field(name="📅 Created", value=clan.created_at.strftime("%Y-%m-%d"), inline=True)
        
        members_list = []
        for idx, m in enumerate(members):
            member_obj = interaction.guild.get_member(m.user_id) if interaction.guild else None
            if not member_obj:
                try:
                    member_obj = await interaction.client.fetch_user(m.user_id)
                except Exception:
                    pass
            name = member_obj.display_name if member_obj else f"User {m.user_id}"
            if m.user_id == clan.owner_id:
                role_suffix = " (Leader) 👑"
            elif m.is_vice_captain:
                role_suffix = " (Vice-Captain) 🛡️"
            else:
                role_suffix = ""
            members_list.append(f"{idx+1}. <@{m.user_id}> ({name}){role_suffix}")
            
        members_str = "\n".join(members_list) if members_list else "*No members.*"
        embed.add_field(name=f"👥 Members ({len(members)})", value=members_str, inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Leaves your current clan.")
    async def clan_leave(self, interaction: discord.Interaction) -> None:
        """Leaves the clan. Disbands it if the caller is the leader."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You are not in a clan.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            
            if clan.owner_id == interaction.user.id:
                # Owner leaving disbands the clan
                clan_name = clan.name
                await session.execute(
                    update(UserGuildStats).filter_by(clan_id=clan.id).values(clan_id=None, is_vice_captain=False)
                )
                await session.execute(delete(Clan).filter_by(id=clan.id))
                await session.commit()
                await interaction.response.send_message(f"💥 Clan **{clan_name}** has been disbanded because the leader left.")
            else:
                caller_stats.clan_id = None
                caller_stats.is_vice_captain = False
                await session.commit()
                await interaction.response.send_message(f"👋 You have left the clan **{clan.name}**.")

    @app_commands.command(name="kick", description="Kicks a member from your clan.")
    @app_commands.describe(member="The member to kick.")
    async def clan_kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ) -> None:
        """Kicks a member from the caller's clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot kick yourself. Use `/clan leave` to leave.", ephemeral=True)
            return

        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Ensure caller exists
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You must be a clan leader to kick members.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            if clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan leader can kick members.", ephemeral=True)
                return
                
            # Ensure target exists and check clan membership
            target_stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            if target_stats.clan_id != clan.id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            target_stats.clan_id = None
            target_stats.is_vice_captain = False
            await session.commit()
            
        await interaction.response.send_message(f"👢 **{member.display_name}** has been kicked from the clan **{clan.name}**.")

    @app_commands.command(name="disband", description="Disbands your clan.")
    async def clan_disband(self, interaction: discord.Interaction) -> None:
        """Disbands the caller's clan."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Ensure caller exists
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You must be a clan leader to disband it.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            if clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan leader can disband the clan.", ephemeral=True)
                return
                
            clan_name = clan.name
            # Nullify clan_id for all members
            await session.execute(
                update(UserGuildStats).filter_by(clan_id=clan.id).values(clan_id=None, is_vice_captain=False)
            )
            await session.execute(delete(Clan).filter_by(id=clan.id))
            await session.commit()
            
        await interaction.response.send_message(f"💥 Clan **{clan_name}** has been successfully disbanded.")

    @app_commands.command(name="promote", description="Promotes a member to Vice-Captain.")
    @app_commands.describe(member="The member to promote.")
    async def clan_promote(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ) -> None:
        """Promotes a member to Vice-Captain."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You are already the clan leader.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Check if caller is leader of a clan
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You must be a clan leader to promote members.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            if clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan leader can promote members.", ephemeral=True)
                return
                
            # Check if target is in the same clan
            target_stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            if target_stats.clan_id != clan.id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            if target_stats.is_vice_captain:
                await interaction.response.send_message(f"❌ **{member.display_name}** is already a vice-captain.", ephemeral=True)
                return
                
            target_stats.is_vice_captain = True
            await session.commit()
            
        await interaction.response.send_message(f"🛡️ **{member.display_name}** has been promoted to **Vice-Captain** of the clan **{clan.name}**!")

    @app_commands.command(name="demote", description="Demotes a Vice-Captain back to normal member.")
    @app_commands.describe(member="The Vice-Captain to demote.")
    async def clan_demote(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ) -> None:
        """Demotes a Vice-Captain back to normal member."""
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Clan commands can only be used inside a server context.", ephemeral=True)
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot demote yourself.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        from bot.services.database_service import DatabaseService
        async with get_db_session() as session:
            # Check if caller is leader of a clan
            caller_stats = await DatabaseService.get_or_create_stats(session, guild_id, interaction.user.id)
            if caller_stats.clan_id is None:
                await interaction.response.send_message("❌ You must be a clan leader to demote members.", ephemeral=True)
                return
                
            clan_result = await session.execute(select(Clan).filter_by(id=caller_stats.clan_id))
            clan = clan_result.scalar_one()
            if clan.owner_id != interaction.user.id:
                await interaction.response.send_message("❌ Only the clan leader can demote members.", ephemeral=True)
                return
                
            # Check if target is in the same clan
            target_stats = await DatabaseService.get_or_create_stats(session, guild_id, member.id)
            if target_stats.clan_id != clan.id:
                await interaction.response.send_message("❌ That member is not in your clan.", ephemeral=True)
                return
                
            if not target_stats.is_vice_captain:
                await interaction.response.send_message(f"❌ **{member.display_name}** is not a vice-captain.", ephemeral=True)
                return
                
            target_stats.is_vice_captain = False
            await session.commit()
            
        await interaction.response.send_message(f"🛡️ **{member.display_name}** has been demoted back to a normal member in the clan **{clan.name}**.")

class Clans(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(ClanGroup())

    async def cog_unload(self):
        # Remove tree group command when reloading
        self.bot.tree.remove_command("clan")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Clans(bot))
