"""Модуль для работы со статусом магазина."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from ..auth import verify_admin
from ..cache import cache_get, cache_set, make_cache_key
from ..database import get_db
from ..schemas import StoreSleepRequest, StoreStatus

router = APIRouter(tags=["store"])

# Простое in-memory кеширование для статуса магазина
_cache: Optional[dict] = None
_cache_expires_at: Optional[datetime] = None
_cache_ttl_seconds = 60  # Синхронизировано с HTTP Cache-Control max-age=60


async def get_or_create_store_status(db: Optional[AsyncIOMotorDatabase], use_cache: bool = True):
    """
    Получает или создает статус магазина с опциональным кешированием.
    Использует двухуровневое кэширование: Redis (распределенный) + in-memory (локальный).

    Args:
        db: Подключение к БД (может быть None если БД недоступна)
        use_cache: Использовать ли кеш (по умолчанию True)
    """
    global _cache, _cache_expires_at

    # Если БД недоступна, возвращаем fallback из кеша или дефолтные значения
    if db is None:
        if use_cache:
            # Сначала проверяем Redis
            try:
                cache_key = make_cache_key("store:status")
                cached_data = await cache_get(cache_key)
                if cached_data:
                    import json
                    return json.loads(cached_data.decode("utf-8") if isinstance(cached_data, bytes) else cached_data)
            except Exception:
                pass
            # Потом in-memory кэш
            if _cache is not None:
                return _cache.copy()
        return {
            "is_sleep_mode": False,
            "sleep_message": None,
            "updated_at": datetime.utcnow(),
        }

    # Проверяем кеш, если он включен
    if use_cache:
        # Сначала проверяем Redis (распределенный кэш)
        try:
            cache_key = make_cache_key("store:status")
            cached_data = await cache_get(cache_key)
            if cached_data:
                import json
                doc = json.loads(cached_data.decode("utf-8") if isinstance(cached_data, bytes) else cached_data)
                # Обновляем in-memory кэш для быстрого доступа
                _cache = doc.copy()
                _cache_expires_at = datetime.utcnow() + timedelta(seconds=_cache_ttl_seconds)
                return doc
        except Exception:
            pass
        
        # Потом проверяем in-memory кэш (локальный)
        if _cache is not None and _cache_expires_at is not None:
            if datetime.utcnow() < _cache_expires_at:
                return _cache.copy()

    try:
        # Используем projection для оптимизации - загружаем только нужные поля
        doc = await db.store_status.find_one(
            {},
            {
                "is_sleep_mode": 1,
                "sleep_message": 1,
                "updated_at": 1,
            },
        )
        if not doc:
            status_doc = {
                "is_sleep_mode": False,
                "sleep_message": None,
                "updated_at": datetime.utcnow(),
            }
            result = await db.store_status.insert_one(status_doc)
            status_doc["_id"] = result.inserted_id
            # Обновляем кеш (Redis + in-memory)
            if use_cache:
                _update_cache(status_doc)
            return status_doc
        
        # Очищаем старые неиспользуемые поля при чтении (если они есть)
        if "payment_link" in doc or "sleep_until" in doc:
            await db.store_status.update_one(
                {"_id": doc["_id"]},
                {
                    "$unset": {
                        "payment_link": "",
                        "sleep_until": "",
                    }
                }
            )
            # Удаляем поля из документа в памяти
            doc.pop("payment_link", None)
            doc.pop("sleep_until", None)
        
        # Логика автоматического пробуждения и payment_link убрана

        # Обновляем кеш (Redis + in-memory)
        if use_cache:
            _update_cache(doc)

        return doc
    except (ServerSelectionTimeoutError, ConnectionFailure) as e:
        import logging

        # Возвращаем fallback вместо исключения
        fallback = {
            "is_sleep_mode": False,
            "sleep_message": None,
            "updated_at": datetime.utcnow(),
        }
        if use_cache:
            _update_cache(fallback)
        return fallback
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Неожиданная ошибка при получении статуса магазина: {e}", exc_info=True)
        # Возвращаем fallback вместо исключения
        fallback = {
            "is_sleep_mode": False,
            "sleep_message": None,
            "updated_at": datetime.utcnow(),
        }
        if use_cache:
            _update_cache(fallback)
        return fallback


def _update_cache(doc: dict):
    """Обновляет кеш статуса магазина (Redis + in-memory)."""
    global _cache, _cache_expires_at
    
    # Обновляем in-memory кэш
    _cache = doc.copy()
    _cache_expires_at = datetime.utcnow() + timedelta(seconds=_cache_ttl_seconds)
    
    # Сохраняем в Redis асинхронно (не блокируем ответ)
    try:
        cache_key = make_cache_key("store:status")
        # Сериализуем datetime для JSON
        doc_for_cache = doc.copy()
        if "updated_at" in doc_for_cache and isinstance(doc_for_cache["updated_at"], datetime):
            doc_for_cache["updated_at"] = doc_for_cache["updated_at"].isoformat()
        cache_data = json.dumps(doc_for_cache, ensure_ascii=False).encode("utf-8")
        asyncio.create_task(cache_set(cache_key, cache_data, ttl=_cache_ttl_seconds))
    except Exception:
        pass  # Игнорируем ошибки Redis для скорости


def _invalidate_cache():
    """Инвалидирует кеш статуса магазина."""
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = None
    # Redis кэш истечет автоматически по TTL, но можно и удалить явно
    try:
        from ..cache import cache_delete, make_cache_key
        cache_key = make_cache_key("store:status")
        asyncio.create_task(cache_delete(cache_key))
    except Exception:
        pass


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
        }
        # sleep_until и payment_link убраны, т.к. не используются
        # updated_at убран из ответа API, т.к. не используется на фронтенде
        # но остается в БД для внутренних нужд

        return normalized
    except Exception as e:
        logger.error(f"Ошибка при нормализации документа: {e}, doc: {doc}", exc_info=True)
        # Возвращаем безопасные значения по умолчанию
        return {
            "is_sleep_mode": False,
            "sleep_message": None,
        }


@router.get("/store/status", response_model=StoreStatus)
async def get_store_status(db: Optional[AsyncIOMotorDatabase] = Depends(get_db)):
    """Получает статус магазина."""
    import logging
    from typing import Optional
    from ..config import settings

    logger = logging.getLogger(__name__)

    # Если БД недоступна, сразу возвращаем fallback
    if db is None:
        return StoreStatus(
            is_sleep_mode=False,
            sleep_message=None,
        )

    try:
        # Убрали debug логи для производительности - они замедляют ответ
        doc = await get_or_create_store_status(db)
        normalized_doc = _normalize_store_status_doc(doc)
        result = StoreStatus(**normalized_doc)
        return result
    except Exception as e:
        logger.error(f"Ошибка при получении статуса магазина: {type(e).__name__}: {e}", exc_info=True)
        # Возвращаем fallback вместо 500, чтобы фронтенд не падал
        return StoreStatus(
            is_sleep_mode=False,
            sleep_message=None,
        )


@router.patch("/admin/store/sleep", response_model=StoreStatus)
async def toggle_store_sleep(
    payload: StoreSleepRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Переключает режим сна магазина."""
    doc = await get_or_create_store_status(db, use_cache=False)
    await db.store_status.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "is_sleep_mode": payload.sleep,
                "sleep_message": payload.message,
                "updated_at": datetime.utcnow(),
            },
            "$unset": {
                "payment_link": "",  # Удаляем старое поле, если оно есть
                "sleep_until": "",   # Удаляем старое поле, если оно есть
            }
        },
    )
    updated = await db.store_status.find_one({"_id": doc["_id"]})
    normalized_doc = _normalize_store_status_doc(updated)
    status_model = StoreStatus(**normalized_doc)
    _invalidate_cache()  # Инвалидируем кеш после изменения
    # Broadcaster removed - frontend uses polling instead of SSE
    return status_model


def _serialize_store_status(model: StoreStatus) -> dict:
    return {
        "is_sleep_mode": model.is_sleep_mode,
        "sleep_message": model.sleep_message,
        # sleep_until и payment_link убраны, т.к. не используются
    }


# Эндпоинт /admin/store/payment-link и функция _ensure_awake_if_needed удалены,
# т.к. payment_link и sleep_until больше не используются
