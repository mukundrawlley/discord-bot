from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.database.base import Base

class MasterPath(Base):
    __tablename__ = "master_paths"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id", ondelete="CASCADE"))
    
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord_role_id: Mapped[int] = mapped_column(BigInteger)
    icon_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    color: Mapped[int | None] = mapped_column(Integer, nullable=True) # Hex color as int
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    
    guild = relationship("Guild", back_populates="paths")
    ranks = relationship("PathRank", back_populates="path", cascade="all, delete-orphan")
    user_stats = relationship("UserGuildStats", back_populates="master_path")
    
    __table_args__ = (
        UniqueConstraint("guild_id", "name", name="uq_guild_path_name"),
        UniqueConstraint("guild_id", "discord_role_id", name="uq_guild_path_role"),
    )
