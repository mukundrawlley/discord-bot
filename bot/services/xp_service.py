import random
import hashlib
import logging
from sqlalchemy.ext.asyncio import AsyncSession
import discord

from bot.config.settings import settings as bot_settings
from bot.models.guild import GuildSettings
from bot.models.user import UserGuildStats
from bot.services.cache_service import cache
from bot.services.database_service import DatabaseService
from bot.utils.curves import get_curve
from bot.utils.validators import is_emoji_only

logger = logging.getLogger("Journey.XPService")

class XPService:
    BASE_XP = 100.0 # Base XP for leveling curve calculations

    @staticmethod
    def is_on_cooldown(guild_id: int, user_id: int) -> bool:
        """Checks if a user is currently on XP cooldown in a specific guild."""
        cooldown_key = f"xp_cooldown:{guild_id}:{user_id}"
        return cache.get(cooldown_key) is not None

    @staticmethod
    def set_cooldown(guild_id: int, user_id: int, cooldown_seconds: int) -> None:
        """Sets the XP cooldown for a user in a specific guild."""
        if cooldown_seconds <= 0:
            return
        cooldown_key = f"xp_cooldown:{guild_id}:{user_id}"
        cache.set(cooldown_key, "1", ttl=cooldown_seconds)

    @staticmethod
    def is_duplicate_message(guild_id: int, user_id: int, content: str) -> bool:
        """Checks if the message content matches the last message sent by this user."""
        if not content:
            return False
        
        # Calculate SHA256 of message content to store efficiently
        msg_hash = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
        cache_key = f"xp_last_msg:{guild_id}:{user_id}"
        
        last_hash = cache.get(cache_key)
        if last_hash == msg_hash:
            return True
            
        # Update last message hash (TTL of 1 hour)
        cache.set(cache_key, msg_hash, ttl=3600)
        return False

    @staticmethod
    def should_give_xp(
        message: discord.Message, 
        guild_settings: GuildSettings
    ) -> tuple[bool, str | None]:
        """Evaluates anti-spam filters to decide if the message qualifies for XP."""
        # 1. Check if XP is disabled globally for the guild
        if not guild_settings.xp_enabled:
            return False, "disabled"
            
        # 2. Check bots and webhooks
        if message.author.bot or message.webhook_id:
            return False, "bot_or_webhook"
            
        # 3. Check message length
        content = message.content or ""
        clean_len = len(content.strip())
        if clean_len < guild_settings.anti_spam_min_length:
            return False, "too_short"
            
        # 4. Check for emoji-only messages
        if guild_settings.anti_spam_block_emojis and is_emoji_only(content):
            return False, "emoji_only"
            
        # 5. Check for sticker-only messages
        if (
            guild_settings.anti_spam_block_stickers 
            and len(message.stickers) > 0 
            and clean_len == 0
        ):
            return False, "sticker_only"
            
        # 6. Check for attachment-only messages
        if (
            guild_settings.anti_spam_block_attachments 
            and len(message.attachments) > 0 
            and clean_len == 0
        ):
            return False, "attachment_only"
            
        # 7. Check cooldowns
        if XPService.is_on_cooldown(message.guild.id, message.author.id):
            return False, "cooldown"
            
        # 8. Check for duplicate messages
        if (
            guild_settings.anti_spam_block_duplicates 
            and XPService.is_duplicate_message(
                message.guild.id, message.author.id, content
            )
        ):
            return False, "duplicate"
            
        return True, None

    @staticmethod
    def calculate_xp_gain(
        message: discord.Message, 
        guild_settings: GuildSettings
    ) -> int:
        """Calculates XP earned based on the configured XP Mode."""
        if guild_settings.xp_mode == "per_word":
            words = (message.content or "").split()
            word_count = len(words)
            xp_gain = int(word_count * float(guild_settings.xp_per_word_val))
            # Bound within min/max boundaries as a safety guard
            return max(1, xp_gain)
        else:
            # Default is random mode
            return random.randint(guild_settings.xp_min, guild_settings.xp_max)

    @staticmethod
    async def add_xp(
        session: AsyncSession,
        guild_settings: GuildSettings,
        user_id: int,
        amount: int
    ) -> tuple[int, int, bool]:
        """Awards XP to a user. Returns (old_level, new_level, level_up_triggered)."""
        guild_id = guild_settings.guild_id
        
        # Fetch stats record
        stats = await DatabaseService.get_or_create_stats(session, guild_id, user_id)
        
        old_level = stats.level
        old_xp = stats.xp
        
        # Ensure we do not add negative XP resulting in total XP < 0
        new_xp = max(0, old_xp + amount)
        stats.xp = new_xp
        
        # Update periodic counters (prevent values below 0)
        stats.xp_daily = max(0, stats.xp_daily + amount)
        stats.xp_weekly = max(0, stats.xp_weekly + amount)
        stats.xp_monthly = max(0, stats.xp_monthly + amount)
        
        # Calculate level based on curve and new total XP
        curve = get_curve(guild_settings.xp_curve)
        new_level = curve.level_for_xp(
            xp=new_xp,
            base_xp=XPService.BASE_XP,
            multiplier=float(guild_settings.xp_multiplier),
            max_level=guild_settings.xp_max_level
        )
        
        level_up = False
        if new_level > old_level:
            stats.level = new_level
            level_up = True
        elif new_level < old_level:
            # Handle XP reduction where level decreases
            stats.level = new_level
            
        await session.flush()
        return old_level, new_level, level_up
