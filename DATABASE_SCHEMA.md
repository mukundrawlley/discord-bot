# Database Schema - Journey Bot

This document outlines the PostgreSQL database schema for the **Journey Bot**. All schemas are fully normalized, utilize appropriate indices, and are designed for high performance under multi-guild scale.

---

## Entity Relationship Diagram (Conceptual)
```
  [guilds] <--- 1:1 ---> [guild_settings]
     |
     | (1:N)
     +---> [master_paths] <--- 1:N ---> [path_ranks]
     |         ^
     |         | (0/1:N)
     | (1:N)   |
     +--------------> [user_guild_stats] <--- N:1 ---> [users]
     |
     +---> [leaderboard_snapshots]
```

---

## Tables Reference

### 1. `guilds`
Tracks active Discord guilds where the bot is installed.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PRIMARY KEY | Discord Guild ID |
| `joined_at` | `TIMESTAMP WITH TIME ZONE` | DEFAULT `NOW()` | When the bot joined the guild |

---

### 2. `guild_settings`
Configurable variables for each guild. By separating configuration from the core `guilds` index table, we keep joins clean.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `guild_id` | `BIGINT` | PRIMARY KEY, FK -> `guilds(id)` ON DELETE CASCADE | Target Guild ID |
| `xp_enabled` | `BOOLEAN` | DEFAULT `TRUE` | Enable/Disable XP gain |
| `xp_min` | `INTEGER` | DEFAULT `10` | Minimum random XP |
| `xp_max` | `INTEGER` | DEFAULT `20` | Maximum random XP |
| `xp_cooldown` | `INTEGER` | DEFAULT `60` | Cooldown in seconds |
| `xp_mode` | `VARCHAR(20)` | DEFAULT `'random'` | `'random'` or `'per_word'` |
| `xp_per_word_val` | `NUMERIC(5, 2)` | DEFAULT `2.0` | XP given per word if mode is per_word |
| `xp_curve` | `VARCHAR(20)` | DEFAULT `'quadratic'` | `'linear'`, `'quadratic'`, or `'exponential'` |
| `xp_multiplier` | `NUMERIC(5, 2)` | DEFAULT `1.0` | Global multiplier |
| `xp_max_level` | `INTEGER` | DEFAULT `100` | Hard cap on leveling |
| `rank_role_mode` | `VARCHAR(10)` | DEFAULT `'stack'` | `'stack'` or `'replace'` |
| `keep_master_path_role` | `BOOLEAN` | DEFAULT `TRUE` | Keep/Remove Master Path role when ranks change |
| `level_msg_enabled` | `BOOLEAN` | DEFAULT `TRUE` | Toggle level up announcements |
| `level_msg_template` | `TEXT` | DEFAULT `'Congrats {user}, you leveled up to level {level}!'` | Message formatting string |
| `level_msg_channel_id`| `BIGINT` | NULLABLE | Channel to send level up message (fallback to current channel) |
| `level_msg_embed` | `BOOLEAN` | DEFAULT `FALSE` | Toggle embed format |
| `level_msg_image_url` | `VARCHAR(256)` | NULLABLE | Attachment/Embed image URL |
| `level_msg_mention_user`| `BOOLEAN` | DEFAULT `TRUE` | Ping user on level up |
| `level_msg_mention_role_id`| `BIGINT`| NULLABLE | Role to ping on level up |
| `anti_spam_min_length` | `INTEGER` | DEFAULT `1` | Min message length for XP |
| `anti_spam_block_emojis`| `BOOLEAN`| DEFAULT `TRUE` | Deny XP for emoji-only messages |
| `anti_spam_block_attachments`| `BOOLEAN`| DEFAULT `TRUE` | Deny XP for attachment-only messages |
| `anti_spam_block_stickers`| `BOOLEAN`| DEFAULT `TRUE` | Deny XP for sticker-only messages |
| `anti_spam_block_duplicates`| `BOOLEAN`| DEFAULT `TRUE` | Deny XP for duplicate messages |

---

### 3. `users`
Global users table.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PRIMARY KEY | Discord User ID |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | DEFAULT `NOW()` | When the user was first registered |

---

### 4. `user_guild_stats`
Main table tracking leveling, accumulated XP, and chosen Master Path for each user inside a guild.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `user_id` | `BIGINT` | PK, FK -> `users(id)` ON DELETE CASCADE | Target User |
| `guild_id` | `BIGINT` | PK, FK -> `guilds(id)` ON DELETE CASCADE | Target Guild |
| `xp` | `BIGINT` | DEFAULT `0`, NOT NULL | Cumulative XP earned |
| `level` | `INTEGER` | DEFAULT `1`, NOT NULL | Calculated Level |
| `master_path_id`| `INTEGER` | NULLABLE, FK -> `master_paths(id)` ON DELETE SET NULL | Chosen Master Path |
| `xp_daily` | `BIGINT` | DEFAULT `0`, NOT NULL | Daily accumulated XP (resets daily) |
| `xp_weekly` | `BIGINT` | DEFAULT `0`, NOT NULL | Weekly accumulated XP (resets weekly) |
| `xp_monthly`| `BIGINT` | DEFAULT `0`, NOT NULL | Monthly accumulated XP (resets monthly) |

*Index: Composite index on `(guild_id, xp DESC)` for leaderboard query optimization. Composite indices on `(guild_id, xp_daily DESC)`, `(guild_id, xp_weekly DESC)`, and `(guild_id, xp_monthly DESC)`.*

---

### 5. `master_paths`
Defines the Master Paths configured for each guild.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `SERIAL` | PRIMARY KEY | Autoincrement PK |
| `guild_id` | `BIGINT` | FK -> `guilds(id)` ON DELETE CASCADE | Parent Guild |
| `name` | `VARCHAR(64)` | NOT NULL | Path name (e.g. 'Shinobi') |
| `description` | `TEXT` | NULLABLE | General info about path |
| `discord_role_id`| `BIGINT` | NOT NULL | Base role representing this path |
| `icon_url` | `VARCHAR(256)` | NULLABLE | Custom icon URL |
| `color` | `INTEGER` | NULLABLE | Custom embed/display color (Hex) |
| `enabled` | `BOOLEAN` | DEFAULT `TRUE` | Activation toggle |

*Unique constraint: `(guild_id, name)`*
*Unique constraint: `(guild_id, discord_role_id)`*

---

### 6. `path_ranks`
Defines the rank progression tiers configured for a specific path.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `SERIAL` | PRIMARY KEY | Autoincrement PK |
| `path_id` | `INTEGER` | FK -> `master_paths(id)` ON DELETE CASCADE | Parent Path |
| `required_level`| `INTEGER` | NOT NULL | Level needed to earn this rank |
| `discord_role_id`| `BIGINT` | NOT NULL | Role assigned at this rank |
| `display_name` | `VARCHAR(64)` | NOT NULL | Clean name for display |
| `icon_url` | `VARCHAR(256)` | NULLABLE | Custom icon URL |

*Unique constraint: `(path_id, required_level)`*
*Unique constraint: `(path_id, discord_role_id)`*

---

### 7. `leaderboard_snapshots`
Daily/Weekly/Monthly snapshots for archiving past results.
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | `SERIAL` | PRIMARY KEY | Autoincrement PK |
| `guild_id` | `BIGINT` | FK -> `guilds(id)` ON DELETE CASCADE | Guild ID |
| `snapshot_type`| `VARCHAR(10)` | NOT NULL | `'daily'`, `'weekly'`, `'monthly'` |
| `created_at` | `TIMESTAMP WITH TIME ZONE` | DEFAULT `NOW()` | Timestamp |
| `data` | `JSONB` | NOT NULL | Snapshot data `[{user_id, xp, level, rank_position}]` |

---

## Future Extensibility Integration

To add the future systems without mutating this base structure, we will link new tables to the `users` and `guilds` keys:
- **Bias Coins / Economy**:
  - `user_guild_economy`: `(user_id, guild_id)` PK, `balance` BIGINT, `bank` BIGINT.
- **Characters / Gacha**:
  - `gacha_characters`: `id` SERIAL PK, `name` VARCHAR, `image_url` VARCHAR, `rarity` INT.
  - `user_guild_characters`: `(user_id, guild_id, character_id)` PK, `obtained_at` TIMESTAMP.
  - `user_guild_spins`: `(user_id, guild_id)` PK, `spins_remaining` INT, `last_spin_time` TIMESTAMP.
- **Marriage**:
  - `user_marriages`: `(guild_id, partner_1_id, partner_2_id)` PK, `married_at` TIMESTAMP.
- **Titles**:
  - `titles`: `id` SERIAL PK, `name` VARCHAR, `guild_id` BIGINT.
  - `user_active_titles`: `(user_id, guild_id)` PK, `title_id` INT FK.
