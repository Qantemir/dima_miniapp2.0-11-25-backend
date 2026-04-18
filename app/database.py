"""Модуль для работы с базой данных MongoDB."""

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from .config import settings

logger = logging.getLogger(__name__)

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None
_indexes_initialized = False
_connect_lock: Optional["asyncio.Lock"] = None


def _get_lock():
    import asyncio
    global _connect_lock
    if _connect_lock is None:
        _connect_lock = asyncio.Lock()
    return _connect_lock


async def connect_to_mongo():
    """Подключается к MongoDB один раз за процесс. Безопасно вызывать многократно."""
    global client, db
    if client is not None and db is not None:
        return

    async with _get_lock():
        if client is not None and db is not None:
            return

        try:
            uri_lower = settings.mongo_uri.lower()
            use_ssl = "mongodb.net" in uri_lower or "ssl=true" in uri_lower or "tls=true" in uri_lower

            client_config = {
                "serverSelectionTimeoutMS": 30000,
                "maxPoolSize": 50,
                "minPoolSize": 5,
                # Atlas по умолчанию закрывает простаивающие коннекты через ~10 мин.
                # 30 мин здесь даёт пулу реально переиспользовать соединения и
                # убирает постоянный churn "Connection accepted/ended" в логах.
                "maxIdleTimeMS": 1800000,
                "connectTimeoutMS": 20000,
                "socketTimeoutMS": 60000,
                "retryWrites": True,
                "retryReads": True,
                # 10s был слишком агрессивным для Atlas — заметно нагружал auth.
                "heartbeatFrequencyMS": 30000,
                "waitQueueTimeoutMS": 30000,
                "appname": "dima-miniapp-backend",
            }

            if use_ssl:
                client_config["tls"] = True

            new_client = AsyncIOMotorClient(settings.mongo_uri, **client_config)
            # Один ping при старте, чтобы сразу увидеть проблему, а не ловить её позже.
            await new_client.admin.command("ping")

            client = new_client
            db = client[settings.mongo_db]
            await ensure_indexes(db)
            logger.info("MongoDB connected (pool min=5 max=50, idle=30m, heartbeat=30s)")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            client = None
            db = None
            return


async def ensure_db_connection():
    """Убеждается, что подключение к БД установлено."""
    if client is None or db is None:
        await connect_to_mongo()


async def close_mongo_connection():
    """Закрывает соединение с MongoDB."""
    global client, db
    if client is not None:
        client.close()
    client = None
    db = None


async def get_db() -> Optional[AsyncIOMotorDatabase]:
    """Возвращает singleton-хэндл БД. Motor сам управляет пулом и health-check,
    поэтому мы НЕ пингуем Mongo на каждом запросе — это и был источник спама в логах."""
    if db is None or client is None:
        await ensure_db_connection()
    return db


async def ensure_indexes(database: AsyncIOMotorDatabase):
    """Создает необходимые индексы в базе данных."""
    global _indexes_initialized
    if _indexes_initialized:
        return

    # Оптимизированные индексы для быстрых запросов
    # Категории
    await database.categories.create_index("name", unique=True)

    # Товары - составной индекс для фильтрации по категории и доступности
    await database.products.create_index([("category_id", ASCENDING), ("available", ASCENDING)])
    await database.products.create_index("available")  # Для быстрой фильтрации доступных товаров

    # Корзины - уникальный индекс для быстрого поиска
    await database.carts.create_index("user_id", unique=True)
    await database.carts.create_index("updated_at")  # Для очистки просроченных корзин (основной запрос)
    await database.carts.create_index("created_at")  # Для корзин без updated_at (редкий случай)

    # Заказы - составные индексы для разных запросов
    await database.orders.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await database.orders.create_index([("created_at", DESCENDING)])
    await database.orders.create_index("status")
    await database.orders.create_index("deleted_at")  # Для фоновой задачи очистки
    await database.orders.create_index([("status", ASCENDING), ("created_at", DESCENDING)])  # Для админки
    await database.orders.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])  # Для автоудаления выполненных/отмененных заказов

    # Клиенты
    await database.customers.create_index("telegram_id", unique=True)

    # Статус магазина
    await database.store_status.create_index("updated_at")

    _indexes_initialized = True
