"""Утилиты для работы с базой данных и общих операций."""

import logging

from bson import ObjectId
from gridfs import GridFS
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import MongoClient

from .config import settings

logger = logging.getLogger(__name__)

# Глобальный синхронный клиент MongoDB для GridFS
_sync_client: MongoClient | None = None
_gridfs: GridFS | None = None


def get_gridfs() -> GridFS:
    """
    Возвращает синхронный экземпляр GridFS для работы с файлами.
    
    GridFS используется синхронно в executor, поэтому нужен синхронный клиент.
    """
    global _sync_client, _gridfs
    
    if _gridfs is None or _sync_client is None:
        # Создаем синхронный клиент MongoDB для GridFS
        # Используем те же настройки, что и для async клиента
        use_ssl = "mongodb.net" in settings.mongo_uri or "ssl=true" in settings.mongo_uri.lower()
        
        client_config = {
            "serverSelectionTimeoutMS": 30000,
            "maxPoolSize": 50,
            "minPoolSize": 10,
            "maxIdleTimeMS": 45000,
            "connectTimeoutMS": 20000,
            "socketTimeoutMS": 60000,
            "retryWrites": True,
            "retryReads": True,
        }
        
        if use_ssl:
            client_config["ssl"] = True
        
        _sync_client = MongoClient(settings.mongo_uri, **client_config)
        _gridfs = GridFS(_sync_client[settings.mongo_db])
    
    return _gridfs


def as_object_id(value: str | ObjectId) -> ObjectId:
    """
    Преобразует строку в ObjectId.
    
    Args:
        value: Строка или ObjectId для преобразования
        
    Returns:
        ObjectId
        
    Raises:
        ValueError: Если значение не может быть преобразовано в ObjectId
    """
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception as e:
        raise ValueError(f"Некорректный ObjectId: {value}") from e


def serialize_doc(doc: dict | None) -> dict:
    """
    Сериализует документ MongoDB, преобразуя ObjectId в строки.
    
    Args:
        doc: Документ MongoDB
        
    Returns:
        Сериализованный словарь
    """
    if doc is None:
        return {}
    
    serialized = {}
    for key, value in doc.items():
        if isinstance(value, ObjectId):
            serialized[key] = str(value)
        elif isinstance(value, dict):
            serialized[key] = serialize_doc(value)
        elif isinstance(value, list):
            serialized[key] = [
                serialize_doc(item) if isinstance(item, dict) else (
                    str(item) if isinstance(item, ObjectId) else item
                )
                for item in value
            ]
        else:
            serialized[key] = value
    
    return serialized


async def ensure_store_is_awake(db: AsyncIOMotorDatabase) -> None:
    """
    Проверяет, что магазин не в режиме сна.
    
    Args:
        db: Подключение к базе данных
        
    Raises:
        HTTPException: Если магазин в режиме сна
    """
    from fastapi import HTTPException, status
    
    store_status = await db.store_status.find_one({})
    if store_status and store_status.get("is_sleep_mode"):
        sleep_message = store_status.get("sleep_message") or "Магазин временно закрыт"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=sleep_message
        )


async def decrement_variant_quantity(
    db: AsyncIOMotorDatabase,
    product_id: str,
    variant_id: str,
    quantity: int
) -> bool:
    """
    Уменьшает количество варианта товара на складе.
    
    Args:
        db: Подключение к базе данных
        product_id: ID товара
        variant_id: ID варианта
        quantity: Количество для уменьшения
        
    Returns:
        True если операция успешна, False если недостаточно товара
    """
    try:
        product_oid = as_object_id(product_id)
        result = await db.products.update_one(
            {
                "_id": product_oid,
                "variants.id": variant_id,
                "variants.quantity": {"$gte": quantity}
            },
            {
                "$inc": {"variants.$.quantity": -quantity}
            }
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Ошибка при уменьшении количества варианта: {e}")
        return False


async def restore_variant_quantity(
    db: AsyncIOMotorDatabase,
    product_id: str,
    variant_id: str,
    quantity: int
) -> None:
    """
    Восстанавливает количество варианта товара на складе.
    
    Args:
        db: Подключение к базе данных
        product_id: ID товара
        variant_id: ID варианта
        quantity: Количество для восстановления
    """
    try:
        product_oid = as_object_id(product_id)
        await db.products.update_one(
            {
                "_id": product_oid,
                "variants.id": variant_id
            },
            {
                "$inc": {"variants.$.quantity": quantity}
            }
        )
    except Exception as e:
        logger.error(f"Ошибка при восстановлении количества варианта: {e}")


async def mark_order_as_deleted(
    db: AsyncIOMotorDatabase,
    order_id: str
) -> bool:
    """
    Помечает заказ как удаленный (мягкое удаление).
    
    Args:
        db: Подключение к базе данных
        order_id: ID заказа
        
    Returns:
        True если операция успешна, False в противном случае
    """
    from datetime import datetime
    
    try:
        order_oid = as_object_id(order_id)
        result = await db.orders.update_one(
            {"_id": order_oid},
            {
                "$set": {"deleted_at": datetime.utcnow()}
            }
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Ошибка при пометке заказа как удаленного: {e}")
        return False


async def restore_order_entry(
    db: AsyncIOMotorDatabase,
    order_id: str
) -> bool:
    """
    Восстанавливает удаленный заказ (убирает пометку об удалении).
    
    Args:
        db: Подключение к базе данных
        order_id: ID заказа
        
    Returns:
        True если операция успешна, False в противном случае
    """
    try:
        order_oid = as_object_id(order_id)
        result = await db.orders.update_one(
            {"_id": order_oid},
            {
                "$unset": {"deleted_at": ""}
            }
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Ошибка при восстановлении заказа: {e}")
        return False


async def permanently_delete_order_entry(
    db: AsyncIOMotorDatabase,
    order_doc: dict
) -> None:
    """
    Окончательно удаляет заказ из базы данных.
    
    Удаляет файл чека из GridFS и сам заказ из коллекции orders.
    
    Args:
        db: Подключение к базе данных
        order_doc: Документ заказа
    """
    import asyncio
    
    order_id = str(order_doc.get("_id"))
    receipt_file_id = order_doc.get("payment_receipt_file_id")
    
    # Удаляем файл чека из GridFS, если он есть
    if receipt_file_id:
        try:
            fs = get_gridfs()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: fs.delete(ObjectId(receipt_file_id)))
        except Exception as e:
            logger.error(f"Ошибка при удалении файла чека {receipt_file_id}: {e}")
    
    # Удаляем заказ из базы данных
    try:
        order_oid = as_object_id(order_id)
        await db.orders.delete_one({"_id": order_oid})
        logger.info(f"Заказ {order_id} окончательно удален")
    except Exception as e:
        logger.error(f"Ошибка при окончательном удалении заказа {order_id}: {e}")
