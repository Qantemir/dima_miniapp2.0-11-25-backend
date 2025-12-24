import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import ServerSelectionTimeoutError, ConnectionFailure

from ..auth import verify_admin
from ..database import get_db
from ..schemas import StoreSleepRequest, StoreStatus, PaymentLinkRequest

router = APIRouter(tags=["store"])


class StoreStatusBroadcaster:
  def __init__(self):
    self._listeners: set[asyncio.Queue] = set()

  def register(self) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    self._listeners.add(queue)
    return queue

  def unregister(self, queue: asyncio.Queue):
    self._listeners.discard(queue)

  async def broadcast(self, payload: dict):
    stale_listeners: list[asyncio.Queue] = []
    for queue in list(self._listeners):
      try:
        queue.put_nowait(payload)
      except asyncio.QueueFull:
        stale_listeners.append(queue)
    for queue in stale_listeners:
      self.unregister(queue)


store_status_broadcaster = StoreStatusBroadcaster()

# Простое in-memory кеширование для статуса магазина
_cache: Optional[dict] = None
_cache_expires_at: Optional[datetime] = None
_cache_ttl_seconds = 30  # Увеличено до 30 секунд для максимальной производительности


async def get_or_create_store_status(db: Optional[AsyncIOMotorDatabase], use_cache: bool = True):
  """
  Получает или создает статус магазина с опциональным кешированием.
  
  Args:
    db: Подключение к БД (может быть None если БД недоступна)
    use_cache: Использовать ли кеш (по умолчанию True)
  """
  global _cache, _cache_expires_at
  
  # Если БД недоступна, возвращаем fallback из кеша или дефолтные значения
  if db is None:
    if use_cache and _cache is not None:
      return _cache.copy()
    return {
      "is_sleep_mode": False,
      "sleep_message": None,
      "sleep_until": None,
      "payment_link": None,
      "updated_at": datetime.utcnow(),
    }
  
  # Проверяем кеш, если он включен
  if use_cache and _cache is not None and _cache_expires_at is not None:
    if datetime.utcnow() < _cache_expires_at:
      return _cache.copy()
  
  try:
    doc = await db.store_status.find_one({})
    if not doc:
      status_doc = {
        "is_sleep_mode": False,
        "sleep_message": None,
        "sleep_until": None,
        "payment_link": None,
        "updated_at": datetime.utcnow(),
      }
      result = await db.store_status.insert_one(status_doc)
      status_doc["_id"] = result.inserted_id
      # Обновляем кеш
      if use_cache:
        _cache = status_doc.copy()
        _cache_expires_at = datetime.utcnow() + timedelta(seconds=_cache_ttl_seconds)
      return status_doc
    if "payment_link" not in doc:
      await db.store_status.update_one(
        {"_id": doc["_id"]},
        {"$set": {"payment_link": None}},
      )
      doc["payment_link"] = None
    
    # Проверяем, нужно ли обновить статус (только если магазин в режиме сна)
    # Это оптимизация: не проверяем каждый раз, если магазин не спит
    if doc.get("is_sleep_mode") and doc.get("sleep_until"):
      doc = await _ensure_awake_if_needed(db, doc)
    
    # Обновляем кеш
    if use_cache:
      _cache = doc.copy()
      _cache_expires_at = datetime.utcnow() + timedelta(seconds=_cache_ttl_seconds)
    
    return doc
  except (ServerSelectionTimeoutError, ConnectionFailure) as e:
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"База данных недоступна: {e}, возвращаем fallback")
    # Возвращаем fallback вместо исключения
    fallback = {
      "is_sleep_mode": False,
      "sleep_message": None,
      "sleep_until": None,
      "payment_link": None,
      "updated_at": datetime.utcnow(),
    }
    if use_cache:
      _cache = fallback.copy()
      _cache_expires_at = datetime.utcnow() + timedelta(seconds=_cache_ttl_seconds)
    return fallback
  except Exception as e:
    import logging
    logger = logging.getLogger(__name__)
    logger.error(f"Неожиданная ошибка при получении статуса магазина: {e}", exc_info=True)
    # Возвращаем fallback вместо исключения
    fallback = {
      "is_sleep_mode": False,
      "sleep_message": None,
      "sleep_until": None,
      "payment_link": None,
      "updated_at": datetime.utcnow(),
    }
    if use_cache:
      _cache = fallback.copy()
      _cache_expires_at = datetime.utcnow() + timedelta(seconds=_cache_ttl_seconds)
    return fallback


def _invalidate_cache():
  """Инвалидирует кеш статуса магазина."""
  global _cache, _cache_expires_at
  _cache = None
  _cache_expires_at = None


def _normalize_store_status_doc(doc: dict) -> dict:
  """
  Нормализует документ из БД для создания StoreStatus модели.
  Преобразует типы полей и удаляет лишние поля.
  """
  import logging
  logger = logging.getLogger(__name__)
  
  try:
    normalized = {
      "is_sleep_mode": bool(doc.get("is_sleep_mode", False)),
      "sleep_message": doc.get("sleep_message") if doc.get("sleep_message") else None,
      "payment_link": doc.get("payment_link") if doc.get("payment_link") else None,
    }
    
    # Преобразуем sleep_until
    sleep_until = doc.get("sleep_until")
    if sleep_until is not None:
      if isinstance(sleep_until, str):
        try:
          # Пробуем разные форматы
          if 'Z' in sleep_until:
            normalized["sleep_until"] = datetime.fromisoformat(sleep_until.replace('Z', '+00:00'))
          else:
            normalized["sleep_until"] = datetime.fromisoformat(sleep_until)
        except (ValueError, AttributeError) as e:
          logger.warning(f"Не удалось преобразовать sleep_until из строки: {sleep_until}, ошибка: {e}")
          normalized["sleep_until"] = None
      elif isinstance(sleep_until, datetime):
        normalized["sleep_until"] = sleep_until
      else:
        logger.warning(f"Неожиданный тип sleep_until: {type(sleep_until)}")
        normalized["sleep_until"] = None
    else:
      normalized["sleep_until"] = None
    
    # Преобразуем updated_at
    updated_at = doc.get("updated_at")
    if updated_at is not None:
      if isinstance(updated_at, str):
        try:
          if 'Z' in updated_at:
            normalized["updated_at"] = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
          else:
            normalized["updated_at"] = datetime.fromisoformat(updated_at)
        except (ValueError, AttributeError) as e:
          logger.warning(f"Не удалось преобразовать updated_at из строки: {updated_at}, ошибка: {e}, используем текущее время")
          normalized["updated_at"] = datetime.utcnow()
      elif isinstance(updated_at, datetime):
        normalized["updated_at"] = updated_at
      else:
        logger.warning(f"Неожиданный тип updated_at: {type(updated_at)}, используем текущее время")
        normalized["updated_at"] = datetime.utcnow()
    else:
      normalized["updated_at"] = datetime.utcnow()
    
    return normalized
  except Exception as e:
    logger.error(f"Ошибка при нормализации документа: {e}, doc: {doc}", exc_info=True)
    # Возвращаем безопасные значения по умолчанию
    return {
      "is_sleep_mode": False,
      "sleep_message": None,
      "sleep_until": None,
      "payment_link": None,
      "updated_at": datetime.utcnow(),
    }


@router.get("/store/status", response_model=StoreStatus)
async def get_store_status(db: Optional[AsyncIOMotorDatabase] = Depends(get_db)):
    import logging
    from typing import Optional
    logger = logging.getLogger(__name__)
    
    # Если БД недоступна, сразу возвращаем fallback
    if db is None:
        logger.warning("БД недоступна, возвращаем fallback статус")
        return StoreStatus(
            is_sleep_mode=False,
            sleep_message=None,
            sleep_until=None,
            payment_link=None,
            updated_at=datetime.utcnow(),
        )
    
    try:
        logger.debug("Получение статуса магазина...")
        doc = await get_or_create_store_status(db)
        logger.debug(f"Документ получен: {doc}")
        normalized_doc = _normalize_store_status_doc(doc)
        logger.debug(f"Документ нормализован: {normalized_doc}")
        result = StoreStatus(**normalized_doc)
        logger.debug("StoreStatus модель создана успешно")
        return result
    except Exception as e:
        logger.error(f"Ошибка при получении статуса магазина: {type(e).__name__}: {e}", exc_info=True)
        # Возвращаем fallback вместо 500, чтобы фронтенд не падал
        return StoreStatus(
            is_sleep_mode=False,
            sleep_message=None,
            sleep_until=None,
            payment_link=None,
            updated_at=datetime.utcnow(),
        )


@router.patch("/admin/store/sleep", response_model=StoreStatus)
async def toggle_store_sleep(
  payload: StoreSleepRequest,
  db: AsyncIOMotorDatabase = Depends(get_db),
  _admin_id: int = Depends(verify_admin),
):
  doc = await get_or_create_store_status(db, use_cache=False)
  await db.store_status.update_one(
    {"_id": doc["_id"]},
    {
      "$set": {
        "is_sleep_mode": payload.sleep,
        "sleep_message": payload.message,
        "sleep_until": payload.sleep_until if payload.sleep else None,
        "updated_at": datetime.utcnow(),
      }
    },
  )
  updated = await db.store_status.find_one({"_id": doc["_id"]})
  updated = await _ensure_awake_if_needed(db, updated)
  normalized_doc = _normalize_store_status_doc(updated)
  status_model = StoreStatus(**normalized_doc)
  _invalidate_cache()  # Инвалидируем кеш после изменения
  await store_status_broadcaster.broadcast(_serialize_store_status(status_model))
  return status_model


def _serialize_store_status(model: StoreStatus) -> dict:
  return {
    "is_sleep_mode": model.is_sleep_mode,
    "sleep_message": model.sleep_message,
    "sleep_until": model.sleep_until.isoformat() if model.sleep_until else None,
    "payment_link": model.payment_link,
    "updated_at": model.updated_at.isoformat(),
  }


@router.get("/store/status/stream")
async def stream_store_status(
  request: Request,
  db: Optional[AsyncIOMotorDatabase] = Depends(get_db),
):
  import logging
  logger = logging.getLogger(__name__)
  
  queue = store_status_broadcaster.register()
  
  # Если БД недоступна, отправляем fallback
  if db is None:
    logger.warning("БД недоступна для стрима, отправляем fallback статус")
    fallback_status = StoreStatus(
      is_sleep_mode=False,
      sleep_message=None,
      sleep_until=None,
      payment_link=None,
      updated_at=datetime.utcnow(),
    )
    await queue.put(_serialize_store_status(fallback_status))
  else:
    try:
      current_doc = await get_or_create_store_status(db)
      normalized_doc = _normalize_store_status_doc(current_doc)
      status_model = StoreStatus(**normalized_doc)
      await queue.put(_serialize_store_status(status_model))
    except Exception as init_error:
      logger.warning(f"Ошибка при инициализации стрима, отправляем fallback: {init_error}")
      # Отправляем fallback статус
      fallback_status = StoreStatus(
        is_sleep_mode=False,
        sleep_message=None,
        sleep_until=None,
        payment_link=None,
        updated_at=datetime.utcnow(),
      )
      await queue.put(_serialize_store_status(fallback_status))

    async def event_generator():
      try:
        while True:
          data = await queue.get()
          yield f"event: status\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
      except asyncio.CancelledError:
        pass
      finally:
        store_status_broadcaster.unregister(queue)

    response = StreamingResponse(event_generator(), media_type="text/event-stream")
    # Явно отключаем gzip для SSE, чтобы избежать ошибок с закрытыми файлами
    response.headers["Content-Encoding"] = "identity"
    return response


@router.patch("/admin/store/payment-link", response_model=StoreStatus)
async def update_payment_link(
  payload: PaymentLinkRequest,
  db: AsyncIOMotorDatabase = Depends(get_db),
  _admin_id: int = Depends(verify_admin),
):
  doc = await get_or_create_store_status(db, use_cache=False)
  payment_link = str(payload.url) if payload.url else None
  await db.store_status.update_one(
    {"_id": doc["_id"]},
    {
      "$set": {
        "payment_link": payment_link,
        "updated_at": datetime.utcnow(),
      }
    },
  )
  updated = await db.store_status.find_one({"_id": doc["_id"]})
  updated = await _ensure_awake_if_needed(db, updated)
  normalized_doc = _normalize_store_status_doc(updated)
  status_model = StoreStatus(**normalized_doc)
  _invalidate_cache()  # Инвалидируем кеш после изменения
  await store_status_broadcaster.broadcast(_serialize_store_status(status_model))
  return status_model


async def _ensure_awake_if_needed(db: Optional[AsyncIOMotorDatabase], doc: dict):
  """
  Проверяет, нужно ли автоматически вывести магазин из режима сна
  (если указано время sleep_until и оно уже прошло).
  """
  if db is None or not doc:
    return doc

  try:
    sleep_until = doc.get("sleep_until")
    is_sleep_mode = doc.get("is_sleep_mode")

    if is_sleep_mode and sleep_until:
      # Преобразуем sleep_until к datetime, если это строка
      if isinstance(sleep_until, str):
        sleep_until_dt = datetime.fromisoformat(sleep_until)
      else:
        sleep_until_dt = sleep_until

      if sleep_until_dt <= datetime.utcnow():
        await db.store_status.update_one(
          {"_id": doc["_id"]},
          {
            "$set": {
              "is_sleep_mode": False,
              "sleep_message": None,
              "sleep_until": None,
              "updated_at": datetime.utcnow(),
            }
          }
        )
        doc["is_sleep_mode"] = False
        doc["sleep_message"] = None
        doc["sleep_until"] = None
        doc["updated_at"] = datetime.utcnow()
        _invalidate_cache()  # Инвалидируем кеш после изменения
        # Рассылаем обновление клиентам
        normalized_doc = _normalize_store_status_doc(doc)
        await store_status_broadcaster.broadcast(
          _serialize_store_status(StoreStatus(**normalized_doc))
        )
  except Exception:
    pass

  return doc

