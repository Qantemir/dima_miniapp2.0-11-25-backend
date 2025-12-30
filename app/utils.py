"""Утилиты для работы с базой данных и общих операций."""

import base64
import io
import logging
import re
from typing import List, Optional

from bson import ObjectId
from gridfs import GridFS
from motor.motor_asyncio import AsyncIOMotorDatabase
from PIL import Image
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


def validate_phone_number(phone: str) -> bool:
    """
    Валидирует номер телефона.
    
    Поддерживает форматы:
    - +7XXXXXXXXXX (11 цифр после +7)
    - 8XXXXXXXXXX (11 цифр, начинается с 8)
    - 7XXXXXXXXXX (11 цифр, начинается с 7)
    - XXXXXXXXXX (10 цифр)
    
    Args:
        phone: Номер телефона для валидации
        
    Returns:
        True если номер валиден, False в противном случае
    """
    if not phone or not isinstance(phone, str):
        return False
    
    # Сохраняем информацию о наличии + в начале
    has_plus = phone.strip().startswith('+')
    
    # Удаляем все пробелы, дефисы, скобки и другие символы
    cleaned = re.sub(r'[\s\-\(\)\+]', '', phone)
    
    # Проверяем, что остались только цифры
    if not cleaned.isdigit():
        return False
    
    # Проверяем длину и формат
    if len(cleaned) == 10:
        # 10 цифр - формат без кода страны
        return True
    elif len(cleaned) == 11:
        # 11 цифр - должен начинаться с 7 или 8
        # Если был + в начале, то номер должен начинаться с 7
        if has_plus:
            return cleaned.startswith('7')
        return cleaned.startswith(('7', '8'))
    
    return False


def normalize_product_images(doc: dict) -> dict:
    """Нормализует поля image и images в единый массив images.
    
    Объединяет image и images в массив images, избегая дубликатов.
    Для обратной совместимости оставляет поле image как первый элемент массива.
    """
    result = doc.copy()
    images_list = []
    
    # Собираем все изображения
    if "images" in doc and isinstance(doc["images"], list):
        images_list = [img for img in doc["images"] if img]  # Фильтруем пустые значения
    
    if "image" in doc and doc["image"]:
        # Добавляем image в начало массива, если его там еще нет
        if doc["image"] not in images_list:
            images_list.insert(0, doc["image"])
    
    # Обновляем поля
    if images_list:
        result["images"] = images_list
        # Для обратной совместимости оставляем image как первый элемент
        result["image"] = images_list[0]
    else:
        # Если нет изображений, удаляем оба поля
        result.pop("image", None)
        result.pop("images", None)
    
    return result


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
    Автоматически обновляет флаг available товара, если все варианты закончились.
    
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
        # Сначала получаем текущее состояние товара для проверки количества после списания
        product = await db.products.find_one(
            {"_id": product_oid, "variants.id": variant_id},
            {"variants": 1, "available": 1}
        )

        if not product:
            return False

        # Находим вариант и проверяем, достаточно ли товара
        variant = next((v for v in product.get("variants", []) if v.get("id") == variant_id), None)
        if not variant:
            return False

        try:
            current_quantity = int(variant.get("quantity", 0))
        except Exception:
            current_quantity = 0

        if current_quantity < quantity:
            return False

        new_quantity = current_quantity - quantity

        # Уменьшаем количество варианта (без фильтра по типу, чтобы сработало при хранении строки)
        result = await db.products.update_one(
            {
                "_id": product_oid,
                "variants.id": variant_id,
            },
            {
                "$set": {"variants.$.quantity": new_quantity}
            }
        )

        if result.modified_count == 0:
            return False

        # Получаем обновленный товар со всеми вариантами для проверки доступности
        updated_product = await db.products.find_one(
            {"_id": product_oid},
            {"variants": 1}
        )

        if updated_product:
            variants = updated_product.get("variants", [])
            # Проверяем, есть ли хотя бы один вариант с quantity > 0
            has_available_variant = any((v.get("quantity", 0) or 0) > 0 for v in variants)

            # Если все варианты закончились, устанавливаем available = false
            if not has_available_variant:
                await db.products.update_one(
                    {"_id": product_oid},
                    {"$set": {"available": False}}
                )

        return True
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
    Автоматически обновляет флаг available товара, если товар снова становится доступным.
    
    Args:
        db: Подключение к базе данных
        product_id: ID товара
        variant_id: ID варианта
        quantity: Количество для восстановления
    """
    try:
        product_oid = as_object_id(product_id)
        
        # Получаем текущее состояние товара
        product = await db.products.find_one(
            {"_id": product_oid, "variants.id": variant_id},
            {"variants": 1, "available": 1}
        )
        
        if not product:
            logger.warning(f"Товар {product_id} или вариант {variant_id} не найден при восстановлении количества")
            return
        
        # Находим вариант и получаем текущее количество
        variant = next((v for v in product.get("variants", []) if v.get("id") == variant_id), None)
        if not variant:
            logger.warning(f"Вариант {variant_id} не найден в товаре {product_id}")
            return
        
        current_quantity = variant.get("quantity", 0)
        was_unavailable = current_quantity <= 0
        
        try:
            numeric_current = int(current_quantity)
        except Exception:
            numeric_current = 0

        new_quantity = numeric_current + quantity

        # Восстанавливаем количество варианта
        await db.products.update_one(
            {
                "_id": product_oid,
                "variants.id": variant_id
            },
            {
                "$set": {"variants.$.quantity": new_quantity}
            }
        )

        # Если товар был недоступен (quantity <= 0) и теперь стал доступен (quantity > 0),
        # устанавливаем available = true
        if was_unavailable and new_quantity > 0:
            await db.products.update_one(
                {"_id": product_oid},
                {"$set": {"available": True}}
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


def compress_image_bytes(
    image_bytes: bytes,
    max_width: int = 1920,
    max_height: int = 1920,
    quality: int = 85,
    format: str = "JPEG",
    min_size_to_compress: int = 100 * 1024  # 100 КБ - минимальный размер для сжатия
) -> bytes:
    """
    Сжимает изображение из бинарных данных.
    
    Args:
        image_bytes: Бинарные данные изображения
        max_width: Максимальная ширина (по умолчанию 1920px)
        max_height: Максимальная высота (по умолчанию 1920px)
        quality: Качество JPEG (1-100, по умолчанию 85)
        format: Формат выходного изображения (JPEG, PNG, WEBP)
        min_size_to_compress: Минимальный размер файла для сжатия (по умолчанию 100 КБ)
        
    Returns:
        Сжатые бинарные данные изображения
    """
    # Если файл уже маленький, пропускаем сжатие для ускорения
    if len(image_bytes) < min_size_to_compress:
        return image_bytes
    
    try:
        # Открываем изображение из байтов
        img = Image.open(io.BytesIO(image_bytes))
        
        # Если изображение уже маленькое по размерам, пропускаем изменение размера
        needs_resize = img.width > max_width or img.height > max_height
        
        # Конвертируем RGBA в RGB для JPEG (если нужно)
        if format == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            # Создаем белый фон для прозрачных изображений
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB" and format == "JPEG":
            img = img.convert("RGB")
        
        # Изменяем размер, если изображение слишком большое
        if needs_resize:
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        
        # Сохраняем в буфер
        output = io.BytesIO()
        
        if format == "JPEG":
            img.save(output, format="JPEG", quality=quality, optimize=True)
        elif format == "PNG":
            img.save(output, format="PNG", optimize=True)
        elif format == "WEBP":
            img.save(output, format="WEBP", quality=quality, method=6)
        else:
            img.save(output, format=format, quality=quality)
        
        compressed_bytes = output.getvalue()
        output.close()
        
        # Если сжатие не дало результата (файл стал больше), возвращаем оригинал
        if len(compressed_bytes) >= len(image_bytes):
            logger.debug(f"Сжатие не уменьшило размер изображения, возвращаем оригинал")
            return image_bytes
        
        # Логируем результат сжатия
        original_size = len(image_bytes)
        compressed_size = len(compressed_bytes)
        reduction = ((original_size - compressed_size) / original_size) * 100 if original_size > 0 else 0
        logger.info(
            f"Изображение сжато: {original_size} → {compressed_size} байт "
            f"({reduction:.1f}% уменьшение)"
        )
        
        return compressed_bytes
    except Exception as e:
        logger.error(f"Ошибка при сжатии изображения: {e}")
        # В случае ошибки возвращаем оригинальные данные
        return image_bytes


def compress_base64_image(
    base64_string: str,
    max_width: int = 1920,
    max_height: int = 1920,
    quality: int = 85,
    min_size_to_compress: int = 100 * 1024  # 100 КБ - минимальный размер для сжатия
) -> Optional[str]:
    """
    Сжимает изображение из base64 строки и возвращает сжатую base64 строку.
    
    Args:
        base64_string: Base64 строка изображения (может содержать data URL префикс)
        max_width: Максимальная ширина (по умолчанию 1920px)
        max_height: Максимальная высота (по умолчанию 1920px)
        quality: Качество JPEG (1-100, по умолчанию 85)
        min_size_to_compress: Минимальный размер файла для сжатия (по умолчанию 100 КБ)
        
    Returns:
        Сжатая base64 строка с data URL префиксом или None в случае ошибки
    """
    if not base64_string:
        return None
    
    try:
        # Убираем data URL префикс, если есть (data:image/jpeg;base64,...)
        if "," in base64_string:
            header, data = base64_string.split(",", 1)
            # Определяем формат из заголовка
            if "jpeg" in header.lower() or "jpg" in header.lower():
                format = "JPEG"
            elif "png" in header.lower():
                format = "PNG"
            elif "webp" in header.lower():
                format = "WEBP"
            else:
                format = "JPEG"  # По умолчанию JPEG
        else:
            data = base64_string
            format = "JPEG"  # По умолчанию JPEG
        
        # Декодируем base64
        image_bytes = base64.b64decode(data)
        
        # Если файл уже маленький, возвращаем оригинал без сжатия
        if len(image_bytes) < min_size_to_compress:
            return base64_string
        
        # Сжимаем изображение
        compressed_bytes = compress_image_bytes(
            image_bytes,
            max_width=max_width,
            max_height=max_height,
            quality=quality,
            format=format,
            min_size_to_compress=min_size_to_compress
        )
        
        # Если сжатие не дало результата, возвращаем оригинал
        if len(compressed_bytes) >= len(image_bytes):
            return base64_string
        
        # Кодируем обратно в base64
        compressed_base64 = base64.b64encode(compressed_bytes).decode("utf-8")
        
        # Возвращаем с data URL префиксом
        mime_type = f"image/{format.lower()}" if format != "JPEG" else "image/jpeg"
        return f"data:{mime_type};base64,{compressed_base64}"
    except Exception as e:
        logger.error(f"Ошибка при сжатии base64 изображения: {e}")
        # В случае ошибки возвращаем оригинальную строку
        return base64_string


async def compress_base64_image_async(
    base64_string: str,
    max_width: int = 1920,
    max_height: int = 1920,
    quality: int = 85
) -> Optional[str]:
    """
    Асинхронно сжимает изображение из base64 строки.
    Выполняет сжатие в executor, чтобы не блокировать event loop.
    
    Args:
        base64_string: Base64 строка изображения (может содержать data URL префикс)
        max_width: Максимальная ширина (по умолчанию 1920px)
        max_height: Максимальная высота (по умолчанию 1920px)
        quality: Качество JPEG (1-100, по умолчанию 85)
        
    Returns:
        Сжатая base64 строка с data URL префиксом или None в случае ошибки
    """
    if not base64_string:
        return None
    
    import asyncio
    
    loop = asyncio.get_event_loop()
    try:
        # Выполняем сжатие в executor, чтобы не блокировать event loop
        result = await loop.run_in_executor(
            None,
            compress_base64_image,
            base64_string,
            max_width,
            max_height,
            quality
        )
        return result
    except Exception as e:
        logger.error(f"Ошибка при асинхронном сжатии base64 изображения: {e}")
        # В случае ошибки возвращаем оригинальную строку
        return base64_string


async def save_base64_image_to_gridfs(
    base64_string: str,
    max_width: int = 1920,
    max_height: int = 1920,
    quality: int = 85,
) -> Optional[str]:
    """
    Сохраняет base64 изображение в GridFS и возвращает file_id.
    
    Args:
        base64_string: Base64 строка изображения (может содержать data URL префикс)
        max_width: Максимальная ширина (по умолчанию 1920px)
        max_height: Максимальная высота (по умолчанию 1920px)
        quality: Качество JPEG (1-100, по умолчанию 85)
        
    Returns:
        file_id как строка или None в случае ошибки
    """
    if not base64_string:
        return None
    
    import asyncio
    from datetime import datetime
    from uuid import uuid4
    
    try:
        loop = asyncio.get_event_loop()
        fs = get_gridfs()
        
        # Определяем формат и декодируем base64
        if "," in base64_string:
            header, data = base64_string.split(",", 1)
            # Определяем формат из заголовка
            if "jpeg" in header.lower() or "jpg" in header.lower():
                format = "JPEG"
                mime_type = "image/jpeg"
                extension = ".jpg"
            elif "png" in header.lower():
                format = "PNG"
                mime_type = "image/png"
                extension = ".png"
            elif "webp" in header.lower():
                format = "WEBP"
                mime_type = "image/webp"
                extension = ".webp"
            else:
                format = "JPEG"
                mime_type = "image/jpeg"
                extension = ".jpg"
        else:
            data = base64_string
            format = "JPEG"
            mime_type = "image/jpeg"
            extension = ".jpg"
        
        # Декодируем base64
        image_bytes = base64.b64decode(data)
        
        # Сжимаем изображение если нужно
        compressed_bytes = await loop.run_in_executor(
            None,
            compress_image_bytes,
            image_bytes,
            max_width,
            max_height,
            quality,
            format
        )
        
        # Генерируем уникальное имя файла
        filename = f"{uuid4().hex}{extension}"
        
        # Сохраняем в GridFS (синхронная операция в executor)
        file_id = await loop.run_in_executor(
            None,
            lambda: fs.put(
                compressed_bytes,
                filename=filename,
                content_type=mime_type,
                metadata={
                    "uploaded_at": datetime.utcnow(),
                    "source": "product_image",
                },
            ),
        )
        
        return str(file_id)
    except Exception as e:
        logger.error(f"Ошибка при сохранении base64 изображения в GridFS: {e}")
        return None


async def save_base64_images_to_gridfs(
    base64_strings: List[str],
    max_width: int = 1920,
    max_height: int = 1920,
    quality: int = 85,
) -> List[str]:
    """
    Сохраняет список base64 изображений в GridFS и возвращает список file_id.
    
    Args:
        base64_strings: Список base64 строк изображений
        max_width: Максимальная ширина (по умолчанию 1920px)
        max_height: Максимальная высота (по умолчанию 1920px)
        quality: Качество JPEG (1-100, по умолчанию 85)
        
    Returns:
        Список file_id (строки), None значения пропускаются
    """
    if not base64_strings:
        return []
    
    results = []
    for base64_str in base64_strings:
        if base64_str:
            file_id = await save_base64_image_to_gridfs(
                base64_str, max_width, max_height, quality
            )
            if file_id:
                results.append(file_id)
    
    return results


def _delete_gridfs_file(file_id: str) -> None:
    """
    Вспомогательная функция для синхронного удаления файла из GridFS.
    
    Args:
        file_id: ID файла в GridFS
    """
    fs = get_gridfs()
    fs.delete(ObjectId(file_id))


async def delete_product_images_from_gridfs(
    product_doc: dict
) -> None:
    """
    Удаляет изображения товара из GridFS.
    
    Args:
        product_doc: Документ товара с полями image и images
    """
    import asyncio
    
    loop = asyncio.get_event_loop()
    
    # Удаляем основное изображение
    if product_doc.get("image"):
        image_id = product_doc["image"]
        # Проверяем, что это не base64 строка (старые данные)
        if isinstance(image_id, str) and not image_id.startswith("data:image") and ObjectId.is_valid(image_id):
            try:
                await loop.run_in_executor(None, _delete_gridfs_file, image_id)
                logger.debug(f"Удалено основное изображение товара: {image_id}")
            except Exception as e:
                logger.error(f"Ошибка при удалении основного изображения товара {image_id}: {e}")
    
    # Удаляем дополнительные изображения
    if product_doc.get("images") and isinstance(product_doc["images"], list):
        for image_id in product_doc["images"]:
            # Проверяем, что это не base64 строка (старые данные)
            if isinstance(image_id, str) and not image_id.startswith("data:image") and ObjectId.is_valid(image_id):
                try:
                    await loop.run_in_executor(None, _delete_gridfs_file, image_id)
                    logger.debug(f"Удалено дополнительное изображение товара: {image_id}")
                except Exception as e:
                    logger.error(f"Ошибка при удалении дополнительного изображения товара {image_id}: {e}")
