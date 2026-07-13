# API Reference - Journey Bot

This document outlines the interfaces, class services, cogs structure, and Discord slash commands.

---

## 1. Discord Slash Commands

### User Commands
| Command | Options | Description |
| :--- | :--- | :--- |
| `/profile` | `[member: Member]` | Displays user card (Level, XP, Path, Rank, placeholder fields). |
| `/path choose` | `path: String (Autocomplete)` | Select a Master Path. Removes previous roles if stack configuration requires. |
| `/xp` | `[member: Member]` | Quick check of XP status and level progression. |
| `/rank` | `[member: Member]` | Synonym/shortcut for viewing a user's rank status. |
| `/leaderboard` | `type: String (XP/Coins/Spins/Characters)`, `filter: String (All/Monthly/Weekly/Daily)` | Display ranking list. |
| `/help` | None | Lists available commands based on user permissions. |

### Admin Commands
Administrators must have `Manage Guild` or `Administrator` permissions.
| Command | Options | Description |
| :--- | :--- | :--- |
| `/xp settings` | `[enabled: Bool] [min_xp: Int] [max_xp: Int] [cooldown: Int] [mode: String] [curve: String] [multiplier: Float] [max_level: Int]` | Configure XP parameters. |
| `/xp add` | `member: Member`, `amount: Int` | Adds XP to a user. Recalculates levels. |
| `/xp remove` | `member: Member`, `amount: Int` | Removes XP from a user. |
| `/xp set` | `member: Member`, `amount: Int` | Set XP directly. |
| `/xp reset` | `member: Member` | Reset user's XP in the guild. |
| `/level set` | `member: Member`, `level: Int` | Set level directly. Sets XP to baseline cumulative required. |
| `/level reset` | `member: Member` | Reset user's level in the guild. |
| `/path create` | `name: String`, `discord_role: Role`, `[description: String] [color: String] [icon_url: String]` | Create a new Master Path. |
| `/path edit` | `path: String (Autocomplete)` `[name: String] [discord_role: Role] [description: String] [color: String] [icon_url: String] [enabled: Bool]` | Edit Path parameters. |
| `/path delete` | `path: String (Autocomplete)` | Deletes a Master Path and resets affected users. |
| `/path list` | None | Lists all created paths in this guild. |
| `/path role` | `path: String (Autocomplete)`, `discord_role: Role` | Re-map or query the associated Discord Onboarding role. |
| `/path ranks` | `path: String (Autocomplete)` | List all ranks defined under a path. |
| `/rank add` | `path: String (Autocomplete)`, `level: Int`, `discord_role: Role`, `name: String`, `[icon_url: String]` | Add a rank reward to a path. |
| `/rank edit` | `rank_id: Int`, `[level: Int] [discord_role: Role] [name: String] [icon_url: String]` | Edit an existing rank tier. |
| `/rank remove` | `rank_id: Int` | Remove a rank tier. |
| `/recalculatexp` | None | Loops through all members in the guild, recalculating levels based on current curve. |
| `/reload` | `[cog_name: String]` | Admin utility to reload cogs on-the-fly. |
| `/profile reset` | `member: Member` | Resets profile back to unselected path and 0 XP. |

---

## 2. Core Service Interfaces (Business Logic)

### `DatabaseService`
Manages pool connections and sessions.
- `get_session() -> AsyncSession`: Returns an asynchronous session context.
- `init_db()`: Triggers initial database table generation if alembic is bypassed, and initializes connection pools.

### `XPService`
Handles mathematical curve evaluations, cooldown checking, database logging, and levels.
- `calculate_xp_gain(message: Message, settings: GuildSettings) -> int`: Evaluates message text size, word counts, or random ranges.
- `is_on_cooldown(guild_id: int, user_id: int) -> bool`: Redis/Cache query checking cooldown status.
- `is_duplicate_message(guild_id: int, user_id: int, message_content: str) -> bool`: Checks MD5/SHA256 message signatures.
- `add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]`: Modifies XP in database, returns `(old_level, new_level, level_up_triggered)`.
- `recalculate_guild_xp(guild_id: int)`: Triggers full guild recalculation.

### `PathService`
Manages paths, rank rewards, onboarding, and Discord role assignment triggers.
- `assign_path(guild_id: int, user_id: int, path_id: int) -> list[int]`: Assigns path, returns roles to add/remove.
- `get_roles_for_level(guild_id: int, user_id: int, level: int) -> tuple[list[int], list[int]]`: Evaluates Stack vs. Replace settings, returning `(roles_to_add, roles_to_remove)`.
- `synchronize_onboarding_roles(member: Member)`: Syncs member path if onboarding roles were added/removed.

### `LeaderboardService`
Handles queries and periodic scheduler operations.
- `get_leaderboard(guild_id: int, type: str, filter: str, limit: int = 10) -> list`: Retrieves sorted list.
- `take_snapshot(guild_id: int, snapshot_type: str)`: Commits daily/weekly/monthly statistics into snapshots JSON.
- `reset_periodic_xp(snapshot_type: str)`: Triggers reset loops for daily, weekly, or monthly periods.
