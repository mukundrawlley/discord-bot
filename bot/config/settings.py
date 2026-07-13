import os
from dotenv import load_dotenv

# Load environmental variables
load_dotenv()

class Settings:
    # Discord Settings
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    TEST_GUILD_ID: int | None = (
        int(os.getenv("TEST_GUILD_ID")) if os.getenv("TEST_GUILD_ID") else None
    )

    # Database Settings
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/journey_db"
    )
    SYNC_DATABASE_URL: str = os.getenv(
        "SYNC_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/journey_db"
    )

    # Cache/Redis Settings
    REDIS_URL: str | None = os.getenv("REDIS_URL")

    # Logging Settings
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

settings = Settings()
