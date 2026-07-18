from datetime import datetime, timezone
from sqlalchemy import BigInteger, ForeignKey, String, Text, Integer, DateTime, Boolean, ForeignKeyConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from bot.database.base import Base

from bot.permissions.defaults import get_default_permission_values, PERMISSIONS_REGISTRY

async def create_default_permissions(session, role_id: int, is_leader: bool = False) -> "ClanRolePermission":
    """Safely creates default permissions for a role, ensuring idempotency."""
    from sqlalchemy.future import select
    result = await session.execute(
        select(ClanRolePermission).filter_by(role_id=role_id)
    )
    perms = result.scalar_one_or_none()
    if perms is None:
        perms_dict = get_default_permission_values(is_leader)
        perms = ClanRolePermission(role_id=role_id, **perms_dict)
        session.add(perms)
        await session.flush()
    return perms

class Clan(Base):
    __tablename__ = "clans"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.id", ondelete="CASCADE"))
    owner_id: Mapped[int] = mapped_column(BigInteger)
    
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    guild = relationship("Guild", back_populates="clans")
    members = relationship("ClanMember", back_populates="clan", cascade="all, delete-orphan")
    roles = relationship("ClanRole", back_populates="clan", cascade="all, delete-orphan")
    audit_logs = relationship("ClanAuditLog", back_populates="clan", cascade="all, delete-orphan")
    applications = relationship("ClanApplication", back_populates="clan", cascade="all, delete-orphan")
    invites = relationship("ClanInvite", back_populates="clan", cascade="all, delete-orphan")
    settings = relationship("ClanSettings", back_populates="clan", uselist=False, cascade="all, delete-orphan")
    onboarding = relationship("ClanOnboarding", back_populates="clan", cascade="all, delete-orphan")

class ClanMember(Base):
    __tablename__ = "clan_members"
    
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    role_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clan_roles.id", ondelete="SET NULL"), nullable=True)
    join_date: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["user_guild_stats.guild_id", "user_guild_stats.user_id"],
            ondelete="CASCADE"
        ),
    )
    
    clan = relationship("Clan", back_populates="members")
    role = relationship("ClanRole", back_populates="members")
    
    stats = relationship(
        "UserGuildStats",
        primaryjoin="and_(ClanMember.guild_id == UserGuildStats.guild_id, ClanMember.user_id == UserGuildStats.user_id)",
        back_populates="clan_member",
        uselist=False
    )

class ClanRole(Base):
    __tablename__ = "clan_roles"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    discord_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    
    role_name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    emoji: Mapped[str | None] = mapped_column(String(64), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(256), nullable=True)
    
    hierarchy_level: Mapped[int] = mapped_column(Integer) # Higher = more authority (Leader = 100)
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True) # None = unlimited
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_system_role: Mapped[bool] = mapped_column(Boolean, default=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    clan = relationship("Clan", back_populates="roles")
    members = relationship("ClanMember", back_populates="role")
    permissions = relationship("ClanRolePermission", back_populates="role", uselist=False, cascade="all, delete-orphan")

class ClanRolePermission(Base):
    __tablename__ = "clan_role_permissions"
    
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("clan_roles.id", ondelete="CASCADE"), primary_key=True)
    
    role = relationship("ClanRole", back_populates="permissions")

# Dynamically attach permission columns from registry
for key, config in PERMISSIONS_REGISTRY.items():
    default_val = config["default"]
    server_def_str = "true" if default_val else "false"
    setattr(
        ClanRolePermission,
        key,
        mapped_column(Boolean, default=default_val, server_default=text(server_def_str))
    )

class ClanAuditLog(Base):
    __tablename__ = "clan_audit_logs"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    clan = relationship("Clan", back_populates="audit_logs")

class ClanApplication(Base):
    __tablename__ = "clan_applications"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="pending") # "pending", "approved", "rejected"
    application_source: Mapped[str] = mapped_column(String(32), default="manual", server_default=text("'manual'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    clan = relationship("Clan", back_populates="applications")

class ClanInvite(Base):
    __tablename__ = "clan_invites"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    invited_by: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="pending") # "pending", "accepted", "declined"
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    clan = relationship("Clan", back_populates="invites")

class ClanSettings(Base):
    __tablename__ = "clan_settings"
    
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"), primary_key=True)
    join_type: Mapped[str] = mapped_column(String(32), default="invite_only") # "open", "invite_only", "apply"
    min_level: Mapped[int] = mapped_column(Integer, default=1)
    min_xp: Mapped[int] = mapped_column(Integer, default=0)
    
    clan = relationship("Clan", back_populates="settings")

class ClanOnboarding(Base):
    __tablename__ = "clan_onboarding"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    discord_role_id: Mapped[int] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    
    clan = relationship("Clan", back_populates="onboarding")
