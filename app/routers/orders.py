"""Модуль для работы с заказами."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import settings
from ..database import get_db
from ..notifications import notify_admins_new_order
from ..schemas import Cart, Order, OrderStatus
from ..security import TelegramUser, get_current_user
from ..utils import as_object_id, compress_image_bytes, ensure_store_is_awake, get_gridfs, serialize_doc, validate_phone_number

router = APIRouter(tags=["orders"])
logger = logging.getLogger(__name__)


async def get_cart(db: AsyncIOMotorDatabase, user_id: int) -> Cart | None:
    """Получает корзину пользователя."""
    cart = await db.carts.find_one({"user_id": user_id})
    if not cart or not cart.get("items"):
        return None
    return Cart(**serialize_doc(cart) | {"id": str(cart["_id"])})


ALLOWED_RECEIPT_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
MAX_RECEIPT_SIZE_BYTES = settings.max_receipt_size_mb * 1024 * 1024


async def _save_payment_receipt(db: AsyncIOMotorDatabase, file: UploadFile) -> tuple[str, str | None]:
    """Сохраняет чек в GridFS и возвращает file_id и оригинальное имя файла."""
    content_type = (file.content_type or "").lower()
    extension = ALLOWED_RECEIPT_MIME_TYPES.get(content_type)
    if not extension:
        original_suffix = Path(file.filename or "").suffix.lower()
        if original_suffix in {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".heic", ".heif"}:
            extension = original_suffix
        else:
            raise HTTPException(
                status_code=400,
                detail="Поддерживаются только изображения (JPG, PNG, WEBP, HEIC) или PDF",
            )

    # Читаем файл правильно, чтобы избежать ошибок с закрытым файлом
    # Используем асинхронный метод read() который правильно обрабатывает файл
    try:
        file_bytes = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка при чтении файла: {str(e)}")

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Файл чека пустой")
    if len(file_bytes) > MAX_RECEIPT_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Файл слишком большой. Максимум {settings.max_receipt_size_mb} МБ",
        )

    # Сжимаем изображения перед сохранением (PDF не сжимаем)
    is_image = extension in {".jpg", ".jpeg", ".png", ".webp"} or (
        content_type and content_type.startswith("image/") and "pdf" not in content_type
    )
    if is_image:
        # Определяем формат для сжатия
        if extension in {".jpg", ".jpeg"} or "jpeg" in content_type:
            format = "JPEG"
        elif extension == ".png" or "png" in content_type:
            format = "PNG"
        elif extension == ".webp" or "webp" in content_type:
            format = "WEBP"
        else:
            format = "JPEG"  # По умолчанию
        
        # Сжимаем изображение асинхронно в executor
        try:
            loop = asyncio.get_event_loop()
            file_bytes = await loop.run_in_executor(
                None,
                compress_image_bytes,
                file_bytes,
                1920,  # max_width
                1920,  # max_height
                85,    # quality
                format
            )
        except Exception as e:
            # В случае ошибки сжатия продолжаем с оригинальным файлом
            logger.warning(f"Не удалось сжать изображение чека: {e}")

    # Сохраняем в GridFS используя синхронный клиент
    # Выполняем в executor, чтобы не блокировать event loop
    loop = asyncio.get_event_loop()
    fs = get_gridfs()
    filename = f"{uuid4().hex}{extension}"

    # Определяем content_type для GridFS
    gridfs_content_type = content_type if content_type else "application/octet-stream"

    # Сохраняем файл в GridFS (синхронная операция в executor)
    try:
        file_id = await loop.run_in_executor(
            None,
            lambda: fs.put(
                file_bytes,
                filename=filename,
                content_type=gridfs_content_type,
                metadata={
                    "original_filename": file.filename,
                    "uploaded_at": datetime.utcnow(),
                },
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при сохранении файла в GridFS: {str(e)}")

    # Возвращаем file_id как строку и оригинальное имя файла
    return str(file_id), file.filename


@router.post("/order", response_model=Order, status_code=status.HTTP_201_CREATED)
async def create_order(
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    comment: str | None = Form(None),
    delivery_type: str | None = Form(None),
    payment_type: str | None = Form(None),
    payment_receipt: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: TelegramUser = Depends(get_current_user),
):
    """Создает новый заказ."""
    user_id = current_user.id
    await ensure_store_is_awake(db)
    
    # Валидация номера телефона
    if not validate_phone_number(phone):
        raise HTTPException(
            status_code=400,
            detail="Некорректный номер телефона. Используйте формат: +7XXXXXXXXXX, 8XXXXXXXXXX или XXXXXXXXXX"
        )
    
    cart = await get_cart(db, user_id)
    if not cart:
        raise HTTPException(status_code=400, detail="Корзина пуста")

    # Оптимизированная проверка доступности (только для товаров с variant_id)
    if cart.items:
        # Сохраняем список items с variant_id один раз для переиспользования
        items_with_variants = [item for item in cart.items if item.variant_id]
        if items_with_variants:
            check_tasks = [
                db.products.find_one({"_id": as_object_id(item.product_id)}, {"variants": 1})
                for item in items_with_variants
            ]
            products = await asyncio.gather(*check_tasks, return_exceptions=True)
            for product, item in zip(products, items_with_variants):
                if isinstance(product, dict):
                    variant = next((v for v in product.get("variants", []) if v.get("id") == item.variant_id), None)
                    if variant and variant.get("quantity", 0) < 0:
                        raise HTTPException(status_code=400, detail=f"Товар '{item.product_name}' больше не доступен")

    receipt_file_id, original_filename = await _save_payment_receipt(db, payment_receipt)

    # Преобразуем items один раз для переиспользования
    items_dict = [item.dict() for item in cart.items]

    order_doc = {
        "user_id": user_id,
        "customer_name": name,
        "customer_phone": phone,
        "delivery_address": address,
        "comment": comment,
        "status": OrderStatus.NEW.value,
        "items": items_dict,
        "total_amount": cart.total_amount,
        "can_edit_address": False,  # Адрес нельзя редактировать после создания
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "payment_receipt_file_id": receipt_file_id,  # ID файла в GridFS
        "payment_receipt_filename": original_filename,
        "delivery_type": delivery_type,
        "payment_type": payment_type,
    }

    try:
        result = await db.orders.insert_one(order_doc)
    except Exception:
        # Удаляем файл из GridFS при ошибке (fire-and-forget)
        try:
            asyncio.create_task(
                asyncio.get_event_loop().run_in_executor(None, lambda: get_gridfs().delete(ObjectId(receipt_file_id)))
            )
        except Exception:
            pass
        raise

    # Добавляем _id к order_doc для создания ответа без дополнительного запроса к БД
    order_doc["_id"] = result.inserted_id
    
    # Удаляем корзину в фоне (не ждем завершения для ускорения ответа)
    async def delete_cart_background():
        try:
            await db.carts.delete_one({"_id": as_object_id(cart.id)})
        except Exception:
            pass  # Игнорируем ошибки при удалении корзины в фоне
    
    asyncio.create_task(delete_cart_background())
    
    # Создаем объект Order из order_doc без дополнительного запроса к БД
    order = Order(**serialize_doc(order_doc) | {"id": str(result.inserted_id)})

    # Отправляем уведомление администраторам в фоновом режиме для скорости
    background_tasks.add_task(
        notify_admins_new_order,
        order_id=order.id,
        customer_name=name,
        customer_phone=phone,
        delivery_address=address,
        total_amount=cart.total_amount,
        items=items_dict,  # Используем уже преобразованные данные
        user_id=user_id,
        receipt_file_id=receipt_file_id,
        db=db,
    )

    return order


# Эндпоинты для просмотра заказов пользователем удалены


# Эндпоинт для получения чека пользователем удален


# Эндпоинт для редактирования адреса удален - адрес нельзя менять после создания заказа
