from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
import logging
from datetime import datetime, timezone

from bot.models.user import UserGuildStats
from bot.models.rank import LeaderboardSnapshot

logger = logging.getLogger("Journey.LeaderboardService")

class LeaderboardService:
    # In-memory cache dictionaries
    # Key: (guild_id, filter_type, limit, offset) -> (timestamp, list[dict])
    _leaderboard_cache = {}
    
    # Key: (guild_id, filter_type) -> (timestamp, count)
    _count_cache = {}
    
    # Key: (guild_id, user_id, filter_type) -> (timestamp, rank, dict)
    _user_rank_cache = {}

    @staticmethod
    async def get_leaderboard(
        session: AsyncSession,
        guild_id: int,
        filter_type: str = "all_time",
        limit: int = 10,
        offset: int = 0,
        force_refresh: bool = False
    ) -> list[dict]:
        """Fetches top users sorted by the requested time filter, supporting offset and 30s cache."""
        current_time = datetime.now(timezone.utc).timestamp()
        filter_type = filter_type.lower()
        cache_key = (guild_id, filter_type, limit, offset)
        
        if not force_refresh and cache_key in LeaderboardService._leaderboard_cache:
            ts, cached_data = LeaderboardService._leaderboard_cache[cache_key]
            if current_time - ts < 30:
                return cached_data
                
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
            .filter(order_col > 0)
            .order_by(order_col.desc())
            .offset(offset)
            .limit(limit)
        )
        users = list(result.scalars())
        
        # Serialize to dict to prevent detached session errors
        data = []
        for u in users:
            data.append({
                "user_id": u.user_id,
                "xp": u.xp,
                "level": u.level,
                "xp_daily": u.xp_daily,
                "xp_weekly": u.xp_weekly,
                "xp_monthly": u.xp_monthly
            })
            
        LeaderboardService._leaderboard_cache[cache_key] = (current_time, data)
        return data

    @staticmethod
    async def get_ranked_users_count(
        session: AsyncSession,
        guild_id: int,
        filter_type: str = "all_time",
        force_refresh: bool = False
    ) -> int:
        """Counts how many users have a score greater than 0 for the selected timeframe (30s cache)."""
        current_time = datetime.now(timezone.utc).timestamp()
        filter_type = filter_type.lower()
        cache_key = (guild_id, filter_type)
        
        if not force_refresh and cache_key in LeaderboardService._count_cache:
            ts, cached_count = LeaderboardService._count_cache[cache_key]
            if current_time - ts < 30:
                return cached_count
                
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
        count = result.scalar_one()
        
        LeaderboardService._count_cache[cache_key] = (current_time, count)
        return count

    @staticmethod
    async def get_user_rank(
        session: AsyncSession,
        guild_id: int,
        user_id: int,
        filter_type: str = "all_time",
        force_refresh: bool = False
    ) -> tuple[int, dict | None]:
        """Gets a specific user's leaderboard position and stats (30s cache)."""
        current_time = datetime.now(timezone.utc).timestamp()
        filter_type = filter_type.lower()
        cache_key = (guild_id, user_id, filter_type)
        
        if not force_refresh and cache_key in LeaderboardService._user_rank_cache:
            ts, rank, cached_stats = LeaderboardService._user_rank_cache[cache_key]
            if current_time - ts < 30:
                return rank, cached_stats
                
        if filter_type == "daily":
            order_col = UserGuildStats.xp_daily
        elif filter_type == "weekly":
            order_col = UserGuildStats.xp_weekly
        elif filter_type == "monthly":
            order_col = UserGuildStats.xp_monthly
        else:
            order_col = UserGuildStats.xp
            
        stats_result = await session.execute(
            select(UserGuildStats).filter_by(guild_id=guild_id, user_id=user_id)
        )
        user_stats = stats_result.scalar_one_or_none()
        if not user_stats:
            LeaderboardService._user_rank_cache[cache_key] = (current_time, 0, None)
            return 0, None
            
        user_score = getattr(user_stats, order_col.name)
        if user_score == 0:
            stats_dict = {
                "user_id": user_stats.user_id,
                "xp": user_stats.xp,
                "level": user_stats.level,
                "xp_daily": user_stats.xp_daily,
                "xp_weekly": user_stats.xp_weekly,
                "xp_monthly": user_stats.xp_monthly
            }
            LeaderboardService._user_rank_cache[cache_key] = (current_time, 0, stats_dict)
            return 0, stats_dict
            
        # Count how many users have a higher score than this user
        count_result = await session.execute(
            select(UserGuildStats)
            .filter_by(guild_id=guild_id)
            .filter(order_col > user_score)
        )
        higher_count = len(list(count_result.scalars()))
        rank = higher_count + 1
        
        stats_dict = {
            "user_id": user_stats.user_id,
            "xp": user_stats.xp,
            "level": user_stats.level,
            "xp_daily": user_stats.xp_daily,
            "xp_weekly": user_stats.xp_weekly,
            "xp_monthly": user_stats.xp_monthly
        }
        LeaderboardService._user_rank_cache[cache_key] = (current_time, rank, stats_dict)
        return rank, stats_dict

    @staticmethod
    async def take_snapshot_and_reset(
        session: AsyncSession,
        guild_id: int,
        snapshot_type: str
    ) -> None:
        """Saves a snapshot of the current leaderboard period, then resets the respective running XP column."""
        snapshot_type = snapshot_type.lower()
        
        # Query database directly for snapshot raw model objects
        order_col = getattr(UserGuildStats, f"xp_{snapshot_type}" if snapshot_type != "all_time" else "xp")
        result = await session.execute(
            select(UserGuildStats)
            .filter_by(guild_id=guild_id)
            .filter(order_col > 0)
            .order_by(order_col.desc())
            .limit(100)
        )
        top_stats = list(result.scalars())
        
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
            
        # Reset the respective columns in database
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
