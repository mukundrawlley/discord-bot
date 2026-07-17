from datetime import datetime, timezone
from sqlalchemy import BigInteger, ForeignKey, String, Text, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.database.base import Base

class Clan(Base):
    __tablename__ = "clans"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id", ondelete="CASCADE"))
    owner_id: Mapped[int] = mapped_column(BigInteger)
    
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    members = relationship("UserGuildStats", back_populates="clan")
    guild = relationship("Guild", back_populates="clans")
