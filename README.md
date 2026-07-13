# Journey Bot

Journey is a modular, scalable, database-driven Discord bot built with `discord.py` 2.x, PostgreSQL, SQLAlchemy ORM, Alembic, and APScheduler. It features a complete XP/leveling system, Master Paths selection, rank role progression rewards, and admin configurations.

## 🚀 Deployment & Start Command

### Production Start Command
To start the bot as a module from the root directory:
```bash
python -m bot.main
```

### Deployment Configuration
The repository includes pre-configured deployment settings for platforms like Railway:
- **`Procfile`**: Specifies `worker: python -m bot.main`
- **`railway.json`**: Specifies `"startCommand": "python -m bot.main"`

---

## 🛠️ Local Setup Instructions

1. **Clone the Repository** and navigate to the project directory.
2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
   *(For local SQLite development, ensure you also run `pip install aiosqlite`)*
3. **Configure Environment**:
   Copy `.env.example` to `.env` and fill in your Discord Bot Token and database URLs.
   ```bash
   cp .env.example .env
   ```
4. **Database Migration**:
   Run Alembic migrations to initialize the database:
   ```bash
   alembic upgrade head
   ```
5. **Run the Bot**:
   ```bash
   python -m bot.main
   ```

---

## 🕹️ Available Slash Commands

- `/profile [member]` — View leveling stats, path, and rank.
- `/xp view [member]` — View level and XP progress.
- `/rank view [member]` — View path rank title.
- `/path list` / `/path choose [path]` — View and select Master Paths.
- `/leaderboard <type> [timeframe]` — View ranking standings.
