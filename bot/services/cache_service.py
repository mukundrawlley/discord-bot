import time
import logging
from abc import ABC, abstractmethod
from bot.config.settings import settings

logger = logging.getLogger("Journey.Cache")

class BaseCache(ABC):
    @abstractmethod
    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        pass
    
    @abstractmethod
    def get(self, key: str) -> str | None:
        pass
    
    @abstractmethod
    def delete(self, key: str) -> None:
        pass

class MemoryCache(BaseCache):
    def __init__(self):
        self._data: dict[str, tuple[str, float | None]] = {} # key -> (value, expire_timestamp)
        logger.info("Initialized In-Memory Cache service.")

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        expire = time.time() + ttl if ttl else None
        self._data[key] = (value, expire)
        self._cleanup()

    def get(self, key: str) -> str | None:
        self._cleanup()
        if key in self._data:
            value, expire = self._data[key]
            if expire is None or expire > time.time():
                return value
            else:
                del self._data[key]
        return None

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def _cleanup(self) -> None:
        now = time.time()
        expired = [
            k for k, (_, exp) in self._data.items() 
            if exp is not None and exp < now
        ]
        for k in expired:
            self._data.pop(k, None)

class RedisCache(BaseCache):
    def __init__(self, url: str):
        import redis
        self._client = redis.from_url(url, decode_responses=True)
        # Test connection
        self._client.ping()
        logger.info("Successfully connected to Redis Cache service.")

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._client.set(key, value, ex=ttl)

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def delete(self, key: str) -> None:
        self._client.delete(key)

# Instantiate the cache singleton with fallback logic
cache: BaseCache

if settings.REDIS_URL:
    try:
        cache = RedisCache(settings.REDIS_URL)
    except Exception as e:
        logger.warning(
            f"Could not connect to Redis at {settings.REDIS_URL} ({e}). "
            "Falling back to In-Memory Cache."
        )
        cache = MemoryCache()
else:
    logger.info("No REDIS_URL configured. Using In-Memory Cache.")
    cache = MemoryCache()
