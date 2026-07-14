from datetime import datetime, timezone
from sqlalchemy import BigInteger, ForeignKey, Integer, String, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.database.base import Base

class PathRank(Base):
    __tablename__ = "path_ranks"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path_id: Mapped[int] = mapped_column(Integer, ForeignKey("master_paths.id", ondelete="CASCADE"))
    
    required_level: Mapped[int] = mapped_column(Integer)
    discord_role_id: Mapped[int] = mapped_column(BigInteger)
    display_name: Mapped[str] = mapped_column(String(64))
    icon_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    path = relationship("MasterPath", back_populates="ranks")
    
    __table_args__ = (
        UniqueConstraint("path_id", "required_level", name="uq_path_rank_level"),
        UniqueConstraint("path_id", "discord_role_id", name="uq_path_rank_role"),
    )

class LeaderboardSnapshot(Base):
    __tablename__ = "leaderboard_snapshots"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id", ondelete="CASCADE"))
    
    snapshot_type: Mapped[str] = mapped_column(String(10)) # 'daily', 'weekly', 'monthly'
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    data: Mapped[dict] = mapped_column(JSON) # Snapshot data e.g. [{"user_id": 123, "xp": 1000, "rank": 1}]
    
    guild = relationship("Guild", back_populates="snapshots")
