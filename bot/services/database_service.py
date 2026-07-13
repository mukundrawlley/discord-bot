from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from bot.models.guild import Guild, GuildSettings
from bot.models.user import User, UserGuildStats

class DatabaseService:
    @staticmethod
    async def get_or_create_guild(session: AsyncSession, guild_id: int) -> Guild:
        """Retrieves a guild record, or creates it along with default GuildSettings if missing."""
        result = await session.execute(
            select(Guild)
            .options(selectinload(Guild.settings))
            .filter_by(id=guild_id)
        )
        guild = result.scalar_one_or_none()
        
        if not guild:
            guild = Guild(id=guild_id)
            session.add(guild)
            await session.flush()
            
            settings = GuildSettings(guild_id=guild_id)
            session.add(settings)
            await session.flush()
            
            # Refresh settings relation
            result = await session.execute(
                select(Guild)
                .options(selectinload(Guild.settings))
                .filter_by(id=guild_id)
            )
            guild = result.scalar_one()
            
        return guild

    @staticmethod
    async def get_or_create_user(session: AsyncSession, user_id: int) -> User:
        """Retrieves a user record, or creates it if missing."""
        result = await session.execute(select(User).filter_by(id=user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            user = User(id=user_id)
            session.add(user)
            await session.flush()
            
        return user

    @staticmethod
    async def get_or_create_stats(
        session: AsyncSession, 
        guild_id: int, 
        user_id: int
    ) -> UserGuildStats:
        """Ensures the guild and user exist, then fetches or creates their guild statistics record."""
        # Ensure parent entities exist
        await DatabaseService.get_or_create_guild(session, guild_id)
        await DatabaseService.get_or_create_user(session, user_id)
        
        result = await session.execute(
            select(UserGuildStats)
            .options(
                selectinload(UserGuildStats.master_path)
            )
            .filter_by(guild_id=guild_id, user_id=user_id)
        )
        stats = result.scalar_one_or_none()
        
        if not stats:
            stats = UserGuildStats(guild_id=guild_id, user_id=user_id)
            session.add(stats)
            await session.flush()
            
        return stats
