from datetime import datetime, timezone
from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.database.base import Base

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    stats = relationship(
        "UserGuildStats", 
        back_populates="user", 
        cascade="all, delete-orphan"
    )

class UserGuildStats(Base):
    __tablename__ = "user_guild_stats"
    
    user_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("users.id", ondelete="CASCADE"), 
        primary_key=True
    )
    guild_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("guilds.id", ondelete="CASCADE"), 
        primary_key=True
    )
    
    xp: Mapped[int] = mapped_column(BigInteger, default=0)
    level: Mapped[int] = mapped_column(default=1)
    
    master_path_id: Mapped[int | None] = mapped_column(
        ForeignKey("master_paths.id", ondelete="SET NULL"), 
        nullable=True
    )
    
    clan_id: Mapped[int | None] = mapped_column(
        ForeignKey("clans.id", ondelete="SET NULL"), 
        nullable=True
    )
    
    xp_daily: Mapped[int] = mapped_column(BigInteger, default=0)
    xp_weekly: Mapped[int] = mapped_column(BigInteger, default=0)
    xp_monthly: Mapped[int] = mapped_column(BigInteger, default=0)
    
    user = relationship("User", back_populates="stats")
    guild = relationship("Guild", back_populates="stats")
    master_path = relationship("MasterPath", back_populates="user_stats")
    clan = relationship("Clan", back_populates="members")
