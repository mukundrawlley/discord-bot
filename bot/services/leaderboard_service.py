from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
import logging
from datetime import datetime, timezone

from bot.models.user import UserGuildStats
from bot.models.rank import LeaderboardSnapshot

logger = logging.getLogger("Journey.LeaderboardService")

class LeaderboardService:
    @staticmethod
    async def get_leaderboard(
        session: AsyncSession,
        guild_id: int,
        filter_type: str = "all_time",
        limit: int = 10,
        offset: int = 0
    ) -> list[UserGuildStats]:
        """Fetches top users sorted by the requested time filter, supporting offset."""
        filter_type = filter_type.lower()
        if filter_type == "daily":
            order_col = UserGuildStats.xp_daily
        elif filter_type == "weekly":
            order_col = UserGuildStats.xp_weekly
        elif filter_type == "monthly":
            order_col = UserGuildStats.xp_monthly
        else:
            order_col = UserGuildStats.xp
            
        result = await session.execute(
            select(UserGuildStats)
            .filter_by(guild_id=guild_id)
            .filter(order_col > 0) # Only include members with XP in the current filter
            .order_by(order_col.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars())

    @staticmethod
    async def get_ranked_users_count(
        session: AsyncSession,
        guild_id: int,
        filter_type: str = "all_time"
    ) -> int:
        """Counts how many users have a score greater than 0 for the selected timeframe."""
        filter_type = filter_type.lower()
        if filter_type == "daily":
            order_col = UserGuildStats.xp_daily
        elif filter_type == "weekly":
            order_col = UserGuildStats.xp_weekly
        elif filter_type == "monthly":
            order_col = UserGuildStats.xp_monthly
        else:
            order_col = UserGuildStats.xp
            
        from sqlalchemy import func
        result = await session.execute(
            select(func.count())
            .select_from(UserGuildStats)
            .filter_by(guild_id=guild_id)
            .filter(order_col > 0)
        )
        return result.scalar_one()

    @staticmethod
    async def get_user_rank(
        session: AsyncSession,
        guild_id: int,
        user_id: int,
        filter_type: str = "all_time"
    ) -> tuple[int, UserGuildStats | None]:
        """Gets a specific user's leaderboard position and stats. Returns (rank_position, stats).
        
        Rank positions are 1-indexed. Returns (0, None) if the user has no stats.
        """
        filter_type = filter_type.lower()
        if filter_type == "daily":
            order_col = UserGuildStats.xp_daily
        elif filter_type == "weekly":
            order_col = UserGuildStats.xp_weekly
        elif filter_type == "monthly":
            order_col = UserGuildStats.xp_monthly
        else:
            order_col = UserGuildStats.xp
            
        # Get user's own stats first
        stats_result = await session.execute(
            select(UserGuildStats).filter_by(guild_id=guild_id, user_id=user_id)
        )
        user_stats = stats_result.scalar_one_or_none()
        if not user_stats:
            return 0, None
            
        # Get their score
        user_score = getattr(user_stats, order_col.name)
        if user_score == 0:
            # User has no score, they are at the bottom (unranked)
            return 0, user_stats
            
        # Count how many users have a higher score than this user
        count_result = await session.execute(
            select(UserGuildStats)
            .filter_by(guild_id=guild_id)
            .filter(order_col > user_score)
        )
        higher_count = len(list(count_result.scalars()))
        
        return higher_count + 1, user_stats

    @staticmethod
    async def take_snapshot_and_reset(
        session: AsyncSession,
        guild_id: int,
        snapshot_type: str
    ) -> None:
        """Saves a snapshot of the current leaderboard period, then resets the respective running XP column."""
        snapshot_type = snapshot_type.lower()
        
        # 1. Fetch top 100 members in the category
        top_stats = await LeaderboardService.get_leaderboard(
            session=session,
            guild_id=guild_id,
            filter_type=snapshot_type,
            limit=100
        )
        
        if top_stats:
            # Build snapshot payload
            snapshot_data = [
                {
                    "user_id": stats.user_id,
                    "xp": getattr(stats, f"xp_{snapshot_type}" if snapshot_type != "all_time" else "xp"),
                    "level": stats.level
                }
                for stats in top_stats
            ]
            
            # Save to snapshots table
            snapshot = LeaderboardSnapshot(
                guild_id=guild_id,
                snapshot_type=snapshot_type,
                data={"rankings": snapshot_data}
            )
            session.add(snapshot)
            logger.info(f"Captured {snapshot_type} snapshot for guild {guild_id}.")
            
        # 2. Reset the respective columns in database
        if snapshot_type == "daily":
            await session.execute(
                update(UserGuildStats)
                .filter_by(guild_id=guild_id)
                .values(xp_daily=0)
            )
        elif snapshot_type == "weekly":
            await session.execute(
                update(UserGuildStats)
                .filter_by(guild_id=guild_id)
                .values(xp_weekly=0)
            )
        elif snapshot_type == "monthly":
            await session.execute(
                update(UserGuildStats)
                .filter_by(guild_id=guild_id)
                .values(xp_monthly=0)
            )
            
        await session.flush()
        logger.info(f"Reset {snapshot_type} XP statistics for guild {guild_id}.")
