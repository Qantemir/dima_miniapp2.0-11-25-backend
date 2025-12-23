import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from .config import settings

logger = logging.getLogger(__name__)

client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None
_indexes_initialized = False


async def connect_to_mongo():
  """Подключается к MongoDB. Вызывается лениво при первом использовании."""
  global client, db
  if client is None:
    try:
      # Оптимизация connection pool для быстрой работы с увеличенными таймаутами для Atlas
      # Определяем, нужен ли SSL (если URI содержит mongodb.net или ssl=true)
      use_ssl = "mongodb.net" in settings.mongo_uri or "ssl=true" in settings.mongo_uri.lower()
      
      client_config = {
        "serverSelectionTimeoutMS": 30000,  # Увеличено до 30 секунд для SSL handshake
        "maxPoolSize": 50,  # Больше соединений для параллельных запросов
        "minPoolSize": 10,  # Минимум соединений всегда готовы
        "maxIdleTimeMS": 45000,  # Время жизни неактивных соединений
        "connectTimeoutMS": 20000,  # Увеличено до 20 секунд для SSL handshake
        "socketTimeoutMS": 60000,  # Увеличено до 60 секунд для операций чтения
        "retryWrites": True,  # Автоматические повторы записи
        "retryReads": True,  # Автоматические повторы чтения
        "heartbeatFrequencyMS": 10000,  # Проверка соединения каждые 10 секунд
        "waitQueueTimeoutMS": 30000,  # Таймаут ожидания в очереди соединений
      }
      
      # Для MongoDB Atlas явно включаем SSL
      if use_ssl:
        client_config["ssl"] = True
      
      client = AsyncIOMotorClient(settings.mongo_uri, **client_config)
      db = client[settings.mongo_db]
      await ensure_indexes(db)
      # Проверяем подключение
      await client.admin.command('ping')
      logger.info(f"Connected to MongoDB at {settings.mongo_uri}")
    except Exception as e:
      logger.error(f"Failed to connect to MongoDB: {e}")
      logger.error("Server will start but database operations will fail. Please start MongoDB.")
      # Не пытаемся продолжать инициализацию, оставляем db=None, чтобы приложение могло подняться
      client = None
      db = None
      return


async def ensure_db_connection():
  """Убеждается, что подключение к БД установлено."""
  if client is None:
    await connect_to_mongo()


async def close_mongo_connection():
  global client
  if client:
    client.close()
    client = None


async def get_db() -> AsyncIOMotorDatabase:
  """Получает подключение к БД, подключаясь при необходимости."""
  if client is None or db is None:
    await ensure_db_connection()
  if db is None:
    raise RuntimeError("Database is not initialized")
  return db


async def ensure_indexes(database: AsyncIOMotorDatabase):
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
  await database.carts.create_index("updated_at")  # Для очистки просроченных корзин
  
  # Заказы - составные индексы для разных запросов
  await database.orders.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
  await database.orders.create_index([("created_at", DESCENDING)])
  await database.orders.create_index("status")
  await database.orders.create_index("deleted_at")  # Для фоновой задачи очистки
  await database.orders.create_index([("status", ASCENDING), ("created_at", DESCENDING)])  # Для админки
  
  # Клиенты
  await database.customers.create_index("telegram_id", unique=True)
  
  # Статус магазина
  await database.store_status.create_index("updated_at")
  
  _indexes_initialized = True

