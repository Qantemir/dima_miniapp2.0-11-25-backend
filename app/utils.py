import asyncio
from bson import ObjectId
from fastapi import HTTPException, status
from gridfs import GridFS
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import MongoClient

from .config import settings

_sync_client: MongoClient | None = None
_sync_db = None


def get_gridfs() -> GridFS:
  """
  Возвращает синхронный GridFS клиент для сохранения/удаления файлов чеков.
  Настроен с теми же параметрами SSL и таймаутов, что и асинхронный клиент.
  """
  global _sync_client, _sync_db
  if _sync_client is None:
    _sync_client = MongoClient(
      settings.mongo_uri,
      serverSelectionTimeoutMS=30000,  # Увеличено для SSL handshake
      maxPoolSize=50,
      minPoolSize=5,  # Меньше для синхронного клиента
      maxIdleTimeMS=45000,
      connectTimeoutMS=20000,  # Увеличено для SSL handshake
      socketTimeoutMS=60000,
      retryWrites=True,
      retryReads=True,
      heartbeatFrequencyMS=10000,
      waitQueueTimeoutMS=30000,
      # Дополнительные параметры для стабильности SSL соединения
      ssl=True,  # Явно включаем SSL для Atlas
      # ssl_cert_reqs удален - в новых версиях PyMongo эта опция не поддерживается
    )
    _sync_db = _sync_client[settings.mongo_db]
  return GridFS(_sync_db)


def serialize_doc(doc):
  """Оптимизированная сериализация документа"""
  if not doc:
    return doc
  result = {}
  for key, value in doc.items():
    if isinstance(value, ObjectId):
      result[key] = str(value)
    elif isinstance(value, list):
      # Оптимизированная обработка списков
      result[key] = [serialize_doc(item) if isinstance(item, dict) else (str(item) if isinstance(item, ObjectId) else item) for item in value]
    elif isinstance(value, dict):
      result[key] = serialize_doc(value)
    else:
      result[key] = value
  return result


def as_object_id(value: str) -> ObjectId:
  if not ObjectId.is_valid(value):
    raise ValueError("Invalid ObjectId")
  return ObjectId(value)


async def _update_variant_quantity(
  db: AsyncIOMotorDatabase,
  product_id: str,
  variant_id: str,
  quantity_diff: int,
  require_available: bool = False,
) -> bool:
  if quantity_diff == 0:
    return True

  try:
    product_oid = as_object_id(product_id)
  except ValueError:
    return False

  base_filter = {
    "_id": product_oid,
    "variants": {
      "$elemMatch": {
        "id": variant_id,
      }
    }
  }

  if quantity_diff < 0 and require_available:
    base_filter["variants"]["$elemMatch"]["quantity"] = {"$gte": abs(quantity_diff)}

  result = await db.products.update_one(
    base_filter,
    {"$inc": {"variants.$.quantity": quantity_diff}},
  )
  return result.modified_count == 1


async def decrement_variant_quantity(
  db: AsyncIOMotorDatabase,
  product_id: str,
  variant_id: str,
  quantity: int,
) -> bool:
  """Списывает товары со склада с проверкой достаточного количества."""
  if quantity <= 0:
    return True
  return await _update_variant_quantity(
    db,
    product_id,
    variant_id,
    quantity_diff=-quantity,
    require_available=True,
  )


async def restore_variant_quantity(
  db: AsyncIOMotorDatabase,
  product_id: str,
  variant_id: str,
  quantity: int
):
  """Возвращает количество товара на склад"""
  if quantity <= 0:
    return
  await _update_variant_quantity(
    db,
    product_id,
    variant_id,
    quantity_diff=quantity,
    require_available=False,
  )


async def mark_order_as_deleted(
  db: AsyncIOMotorDatabase,
  order_doc: dict,
) -> None:
  """
  Помечает заказ как удаленный (soft delete) с временной меткой.
  Заказ можно восстановить в течение 10 минут.
  """
  from datetime import datetime, timedelta
  order_id = order_doc.get("_id")
  if order_id:
    await db.orders.update_one(
      {"_id": order_id},
      {
        "$set": {
          "deleted_at": datetime.utcnow(),
        }
      }
    )


async def restore_order_entry(
  db: AsyncIOMotorDatabase,
  order_id: ObjectId,
) -> bool:
  """
  Восстанавливает удаленный заказ (убирает метку deleted_at).
  Возвращает True если заказ был восстановлен, False если не найден или уже окончательно удален.
  """
  result = await db.orders.update_one(
    {"_id": order_id, "deleted_at": {"$exists": True}},
    {
      "$unset": {
        "deleted_at": "",
      }
    }
  )
  return result.modified_count > 0


async def permanently_delete_order_entry(
  db: AsyncIOMotorDatabase,
  order_doc: dict,
) -> None:
  """
  Окончательно удаляет заказ из базы и очищает связанные ресурсы (например, чек в GridFS).
  Используется после истечения 10 минут с момента soft delete.
  """
  order_id = order_doc.get("_id")
  if order_id:
    await db.orders.delete_one({"_id": order_id})

  receipt_file_id = order_doc.get("payment_receipt_file_id")
  if not receipt_file_id:
    return

  try:
    receipt_object_id = ObjectId(receipt_file_id)
  except Exception:
    return

  fs = get_gridfs()
  loop = asyncio.get_event_loop()
  try:
    await loop.run_in_executor(None, lambda: fs.delete(receipt_object_id))
  except Exception:
    # Игнорируем ошибки удаления файла, чтобы не мешать основному потоку
    pass


# Кэш статуса магазина для быстрой проверки
_store_status_cache: dict | None = None
_store_status_cache_time: float = 0
_STORE_STATUS_CACHE_TTL = 5.0  # 5 секунд кэш

async def ensure_store_is_awake(
  db: AsyncIOMotorDatabase,
) -> None:
  """Оптимизированная проверка статуса магазина с кэшированием"""
  import time
  global _store_status_cache, _store_status_cache_time
  
  now = time.time()
  # Используем кэш если он свежий
  if _store_status_cache and (now - _store_status_cache_time) < _STORE_STATUS_CACHE_TTL:
    if _store_status_cache.get("is_sleep_mode"):
      raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=_store_status_cache.get("sleep_message") or "Магазин временно не принимает заказы",
      )
    return

  # Обновляем кэш
  doc = await db.store_status.find_one({}, {"is_sleep_mode": 1, "sleep_message": 1})
  if doc:
    _store_status_cache = doc
    _store_status_cache_time = now
  if doc.get("is_sleep_mode"):
    raise HTTPException(
      status_code=status.HTTP_423_LOCKED,
        detail=doc.get("sleep_message") or "Магазин временно не принимает заказы",
    )
