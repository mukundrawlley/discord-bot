from datetime import datetime, timezone
from sqlalchemy import BigInteger, Boolean, Integer, Numeric, String, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.database.base import Base

class Guild(Base):
    __tablename__ = "guilds"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    settings = relationship(
        "GuildSettings", 
        back_populates="guild", 
        uselist=False, 
        cascade="all, delete-orphan"
    )
    paths = relationship(
        "MasterPath", 
        back_populates="guild", 
        cascade="all, delete-orphan"
    )
    stats = relationship(
        "UserGuildStats", 
        back_populates="guild", 
        cascade="all, delete-orphan"
    )
    snapshots = relationship(
        "LeaderboardSnapshot", 
        back_populates="guild", 
        cascade="all, delete-orphan"
    )
    clans = relationship(
        "Clan", 
        back_populates="guild", 
        cascade="all, delete-orphan"
    )

class GuildSettings(Base):
    __tablename__ = "guild_settings"
    
    guild_id: Mapped[int] = mapped_column(
        BigInteger, 
        ForeignKey("guilds.id", ondelete="CASCADE"), 
        primary_key=True
    )
    
    # XP Configurations
    xp_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    xp_min: Mapped[int] = mapped_column(Integer, default=10)
    xp_max: Mapped[int] = mapped_column(Integer, default=20)
    xp_cooldown: Mapped[int] = mapped_column(Integer, default=60) # Cooldown in seconds
    xp_mode: Mapped[str] = mapped_column(String(20), default="random") # 'random' or 'per_word'
    xp_per_word_val: Mapped[float] = mapped_column(Numeric(5, 2), default=2.0)
    xp_curve: Mapped[str] = mapped_column(String(20), default="quadratic") # 'linear', 'quadratic', 'exponential'
    xp_multiplier: Mapped[float] = mapped_column(Numeric(5, 2), default=1.0)
    xp_max_level: Mapped[int] = mapped_column(Integer, default=100)
    
    # Role configurations
    rank_role_mode: Mapped[str] = mapped_column(String(10), default="stack") # 'stack' or 'replace'
    keep_master_path_role: Mapped[bool] = mapped_column(Boolean, default=True)
    
    # Level message configuration
    level_msg_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    level_msg_template: Mapped[str] = mapped_column(
        Text, 
        default="Congratulations {user}, you leveled up to level {level}!"
    )
    level_msg_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    level_msg_embed: Mapped[bool] = mapped_column(Boolean, default=False)
    level_msg_image_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    level_msg_mention_user: Mapped[bool] = mapped_column(Boolean, default=True)
    level_msg_mention_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    
    # Rank message configuration
    rank_msg_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rank_msg_template: Mapped[str] = mapped_column(
        Text, 
        default="Congratulations {user}, you achieved the rank of {rank} on the {path} path!"
    )
    rank_msg_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rank_msg_embed: Mapped[bool] = mapped_column(Boolean, default=False)
    rank_msg_image_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    rank_msg_mention_user: Mapped[bool] = mapped_column(Boolean, default=True)
    rank_msg_mention_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    
    # Anti-spam configuration
    anti_spam_min_length: Mapped[int] = mapped_column(Integer, default=1)
    anti_spam_block_emojis: Mapped[bool] = mapped_column(Boolean, default=True)
    anti_spam_block_attachments: Mapped[bool] = mapped_column(Boolean, default=True)
    anti_spam_block_stickers: Mapped[bool] = mapped_column(Boolean, default=True)
    anti_spam_block_duplicates: Mapped[bool] = mapped_column(Boolean, default=True)
    
    guild = relationship("Guild", back_populates="settings")
