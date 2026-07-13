# Project Plan - Journey Bot

This document outlines the development lifecycle, phases, and schedule for the **Journey Bot**.

## Phase 1: Core Systems & Leveling (Active)
The goal of Phase 1 is to build the baseline leveling, XP system, Master Paths, role rewards, and administration commands.

### Phase 1.1: Database Setup and Core Models
- Setup SQLAlchemy ORM engine and models.
- Configure Alembic migrations.
- Set up initial PostgreSQL schemas matching `DATABASE_SCHEMA.md`.

### Phase 1.2: Core Services
- **Database Service**: Handles generic DB connections, sessions, and transaction blocks.
- **XP Service**: Manages XP increments, cooldown checks, duplicate tracking, and level curves.
- **Path Service**: Manages Master Paths, ranks, and user-path associations.
- **Cache Service**: Interface for Redis caching and memory fallback.

### Phase 1.3: Anti-Spam & Discord Listener
- Intercept incoming messages (`on_message`).
- Validate anti-spam criteria (bot, webhook, duplicate, emoji-only, sticker-only, attachment-only, length, cooldown).
- Calculate XP gain (random vs. per-word) and curves.
- Distribute XP and handle level-ups.

### Phase 1.4: Automatic Role Management
- Handle level-up role checks.
- Support `stack` vs. `replace` rank roles.
- Handle `keep` vs. `remove` Master Path role logic.
- Process onboarding role updates (`on_member_update`).

### Phase 1.5: Commands Implementation
- **User Commands**: `/profile`, `/path`, `/path choose`, `/xp`, `/rank`, `/leaderboard`, `/help`.
- **Admin Commands**: `/xp settings`, `/xp add/remove/set/reset`, `/level set/reset`, `/path create/edit/delete/list/role/ranks`, `/rank add/edit/remove`, `/recalculatexp`, `/reload`, `/profile reset`.

### Phase 1.6: Scheduler & Resets
- APScheduler jobs for daily, weekly, and monthly running XP resets.
- Save daily/weekly/monthly leaderboard snapshots to `leaderboard_snapshots`.

### Phase 1.7: Verification & Testing
- Unit testing database transactions.
- Testing curves mathematically.
- Integration tests via mock Discord client or manual server testing.

---

## Future Phases (Out of Scope for Phase 1)

### Phase 2: Economy & Bias Coins
- Base currency system, daily rewards, bank transactions, and active multipliers.

### Phase 3: Character Gacha & Collection
- Characters table, user inventory, gacha rolls, rarity tiers, and character images.

### Phase 4: Marriage System
- Marriage proposals, relationships, shared profiles, and bonuses.

### Phase 5: Titles, Trading & Marketplace
- Title unlocks, character/coin trades, and guild marketplaces.

### Phase 6: Guild Events & Dashboard
- Dynamic server events, web dashboard integrations, and live stats.
