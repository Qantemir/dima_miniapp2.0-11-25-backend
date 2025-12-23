"""
Redis кэширование для высокопроизводительного распределенного кэша
"""

import json
import logging
from typing import Optional, Any
import redis.asyncio as aioredis
from .config import settings

logger = logging.getLogger(__name__)

_redis_client: Optional[aioredis.Redis] = None


async def get_redis() -> Optional[aioredis.Redis]:
    """Получить Redis клиент, создавая при необходимости"""
    global _redis_client
    
    if _redis_client is None:
        try:
            redis_url = getattr(settings, 'redis_url', 'redis://localhost:6379/0')
            _redis_client = await aioredis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=False,  # Работаем с bytes для производительности
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=True,
                health_check_interval=30,
                # Connection pooling для лучшей производительности
                max_connections=50,
                socket_keepalive=True,
                socket_keepalive_options={},
            )
            # Проверяем подключение
            await _redis_client.ping()
            logger.info("✅ Redis подключен успешно")
        except Exception as e:
            logger.warning(f"⚠️ Redis недоступен, работаем без кэша: {e}")
            _redis_client = None
    
    return _redis_client


async def close_redis():
    """Закрыть соединение с Redis"""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis соединение закрыто")


async def cache_get(key: str) -> Optional[bytes]:
    """Получить значение из кэша"""
    try:
        redis = await get_redis()
        if redis:
            return await redis.get(key)
    except Exception as e:
        # Убираем debug логи в production
        from .config import settings
        if settings.environment != "production":
        logger.debug(f"Ошибка получения из кэша {key}: {e}")
    return None


async def cache_set(key: str, value: bytes, ttl: int = 300) -> bool:
    """Сохранить значение в кэш с TTL"""
    try:
        redis = await get_redis()
        if redis:
            await redis.setex(key, ttl, value)
            return True
    except Exception as e:
        # Убираем debug логи в production
        from .config import settings
        if settings.environment != "production":
        logger.debug(f"Ошибка сохранения в кэш {key}: {e}")
    return False


async def cache_delete(key: str) -> bool:
    """Удалить ключ из кэша"""
    try:
        redis = await get_redis()
        if redis:
            await redis.delete(key)
            return True
    except Exception as e:
        # Убираем debug логи в production
        from .config import settings
        if settings.environment != "production":
        logger.debug(f"Ошибка удаления из кэша {key}: {e}")
    return False


async def cache_delete_pattern(pattern: str) -> int:
    """Удалить все ключи по паттерну"""
    try:
        redis = await get_redis()
        if redis:
            keys = []
            async for key in redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                return await redis.delete(*keys)
    except Exception as e:
        # Убираем debug логи в production
        from .config import settings
        if settings.environment != "production":
        logger.debug(f"Ошибка удаления паттерна {pattern}: {e}")
    return 0


def make_cache_key(prefix: str, *args, **kwargs) -> str:
    """Создать ключ кэша из префикса и параметров"""
    parts = [prefix]
    if args:
        parts.extend(str(arg) for arg in args)
    if kwargs:
        sorted_kwargs = sorted(kwargs.items())
        parts.extend(f"{k}:{v}" for k, v in sorted_kwargs)
    return ":".join(parts)

