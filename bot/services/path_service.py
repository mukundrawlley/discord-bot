from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
import discord
import logging

from bot.models.guild import GuildSettings
from bot.models.path import MasterPath
from bot.models.rank import PathRank
from bot.models.user import UserGuildStats
from bot.services.database_service import DatabaseService

logger = logging.getLogger("Journey.PathService")

class PathService:
    @staticmethod
    async def get_paths(session: AsyncSession, guild_id: int) -> list[MasterPath]:
        """Retrieves all master paths in a guild."""
        result = await session.execute(
            select(MasterPath).filter_by(guild_id=guild_id, enabled=True)
        )
        return list(result.scalars())

    @staticmethod
    async def get_path_by_id(session: AsyncSession, path_id: int) -> MasterPath | None:
        """Retrieves a master path by its primary key ID."""
        result = await session.execute(select(MasterPath).filter_by(id=path_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_path_by_name(session: AsyncSession, guild_id: int, name: str) -> MasterPath | None:
        """Retrieves a master path by name within a guild (case-insensitive)."""
        result = await session.execute(
            select(MasterPath).filter_by(guild_id=guild_id)
        )
        for path in result.scalars():
            if path.name.lower() == name.lower():
                return path
        return None

    @staticmethod
    async def evaluate_roles_for_level(
        session: AsyncSession,
        stats: UserGuildStats,
        target_level: int,
        settings: GuildSettings
    ) -> tuple[list[int], list[int]]:
        """Calculates which role IDs should be added or removed for a user's current path at a target level."""
        if not stats.master_path_id:
            return [], []
            
        path_result = await session.execute(
            select(MasterPath).filter_by(id=stats.master_path_id)
        )
        path = path_result.scalar_one_or_none()
        if not path:
            return [], []
            
        ranks_result = await session.execute(
            select(PathRank)
            .filter_by(path_id=stats.master_path_id)
            .order_by(PathRank.required_level.asc())
        )
        all_ranks = list(ranks_result.scalars())
        
        earned_ranks = [r for r in all_ranks if r.required_level <= target_level]
        unearned_ranks = [r for r in all_ranks if r.required_level > target_level]
        
        roles_to_add = []
        roles_to_remove = []
        
        # Check base Master Path role
        if len(earned_ranks) == 0 or settings.keep_master_path_role:
            roles_to_add.append(path.discord_role_id)
        else:
            roles_to_remove.append(path.discord_role_id)
            
        # Check rank roles
        if earned_ranks:
            if settings.rank_role_mode == "replace":
                # In replace mode, only the highest earned rank role is kept
                highest_rank = earned_ranks[-1]
                roles_to_add.append(highest_rank.discord_role_id)
                # All other lower rank roles are removed
                for r in earned_ranks[:-1]:
                    roles_to_remove.append(r.discord_role_id)
            else:
                # In stack mode, all earned rank roles are kept
                for r in earned_ranks:
                    roles_to_add.append(r.discord_role_id)
                    
        # Always remove any unearned rank roles (useful if level decreases)
        for r in unearned_ranks:
            roles_to_remove.append(r.discord_role_id)
            
        return list(set(roles_to_add)), list(set(roles_to_remove))

    @staticmethod
    async def assign_path(
        session: AsyncSession,
        guild_settings: GuildSettings,
        user_id: int,
        path_id: int | None
    ) -> tuple[list[int], list[int]]:
        """Updates user's Master Path selection in database.
        
        Returns tuple of (roles_to_add, roles_to_remove) representing the roles changes needed on Discord.
        """
        guild_id = guild_settings.guild_id
        stats = await DatabaseService.get_or_create_stats(session, guild_id, user_id)
        old_path_id = stats.master_path_id
        
        if old_path_id == path_id:
            return [], []
            
        roles_to_remove = []
        roles_to_add = []
        
        # 1. Purge all role associations of the old path
        if old_path_id:
            old_path = await PathService.get_path_by_id(session, old_path_id)
            if old_path:
                roles_to_remove.append(old_path.discord_role_id)
                
                # Fetch all ranks of the old path
                ranks_res = await session.execute(
                    select(PathRank).filter_by(path_id=old_path_id)
                )
                for rank in ranks_res.scalars():
                    roles_to_remove.append(rank.discord_role_id)
                    
        # 2. Assign the new path in the database
        stats.master_path_id = path_id
        await session.flush()
        
        # 3. Calculate additions and removals for the new path
        if path_id:
            roles_to_add, new_removes = await PathService.evaluate_roles_for_level(
                session, stats, stats.level, guild_settings
            )
            roles_to_remove.extend(new_removes)
            
        return list(set(roles_to_add)), list(set(roles_to_remove))
