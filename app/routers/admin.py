"""Admin router for managing orders and broadcasting messages."""

import asyncio
from datetime import datetime
from typing import List, Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from ..auth import verify_admin
from ..config import get_settings
from ..database import get_db
from ..notifications import notify_customer_order_status
from ..schemas import (
    BroadcastRequest,
    BroadcastResponse,
    Order,
    OrderStatus,
    OrderSummary,
    PaginatedOrdersResponse,
    UpdateStatusRequest,
)
from ..utils import (
    as_object_id,
    get_gridfs,
    mark_order_as_deleted,
    restore_variant_quantity,
    serialize_doc,
)

router = APIRouter(tags=["admin"])


@router.get("/admin/orders", response_model=PaginatedOrdersResponse)
async def list_orders(
    status_filter: Optional[OrderStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    include_deleted: bool = Query(False, description="Включить удаленные заказы"),
    cursor: Optional[str] = Query(None, description="ObjectId последнего заказа предыдущей страницы"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """List orders with pagination and filtering."""
    # Оптимизированное построение запроса
    query = {}
    if status_filter:
        query["status"] = status_filter.value
    if not include_deleted:
        query["deleted_at"] = {"$exists": False}
    if cursor:
        try:
            query["_id"] = {"$lt": as_object_id(cursor)}
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректный cursor")

    # Проекция: получаем только нужные поля для списка (без items, comment, receipt и т.д.)
    projection = {
        "_id": 1,
        "customer_name": 1,
        "customer_phone": 1,
        "delivery_address": 1,
        "status": 1,
        "total_amount": 1,
        "created_at": 1,
        "items": 1,  # Нужен только для подсчета длины, не передаем в ответ
    }

    # Используем индекс для быстрой сортировки
    # Убираем hint - MongoDB сам выберет оптимальный индекс
    docs = await (
        db.orders.find(query, projection)
        .sort("_id", -1)
        .limit(limit + 1)
        .to_list(length=limit + 1)
    )

    # Оптимизированная валидация заказов - создаем OrderSummary напрямую
    orders = []
    for doc in docs:
        try:
            # Подсчитываем количество товаров из items массива
            items_count = len(doc.get("items", []))
            # Создаем упрощенный объект без полной валидации Order
            order_data = serialize_doc(doc) | {"id": str(doc["_id"]), "items_count": items_count}
            # Удаляем items из данных, т.к. они не нужны в OrderSummary
            order_data.pop("items", None)
            orders.append(OrderSummary(**order_data))
        except Exception:
            continue

    next_cursor = None
    if len(orders) > limit:
        next_cursor = orders[limit].id
        orders = orders[:limit]
    return PaginatedOrdersResponse(orders=orders, next_cursor=next_cursor)


@router.get("/admin/order/{order_id}", response_model=Order)
async def get_order(
    order_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Get order by ID."""
    doc = await db.orders.find_one({"_id": as_object_id(order_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return Order(**serialize_doc(doc) | {"id": str(doc["_id"])})


@router.get("/admin/order/{order_id}/receipt")
async def get_admin_order_receipt(
    order_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Получает чек заказа из GridFS для администратора."""
    doc = await db.orders.find_one({"_id": as_object_id(order_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    receipt_file_id = doc.get("payment_receipt_file_id")
    if not receipt_file_id:
        raise HTTPException(status_code=404, detail="Чек не найден")

    try:
        fs = get_gridfs()
        loop = asyncio.get_event_loop()

        # Получаем файл из GridFS (синхронная операция в executor)
        grid_file = await loop.run_in_executor(None, lambda: fs.get(ObjectId(receipt_file_id)))
        file_data = await loop.run_in_executor(None, grid_file.read)
        filename = grid_file.filename or "receipt"
        content_type = grid_file.content_type or "application/octet-stream"

        return Response(
            content=file_data,
            media_type=content_type,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
            },
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Не удалось загрузить чек: {str(e)}")


@router.patch("/admin/order/{order_id}/status", response_model=Order)
async def update_order_status(
    order_id: str,
    payload: UpdateStatusRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Update order status."""
    # Получаем старый статус заказа
    old_doc = await db.orders.find_one({"_id": as_object_id(order_id)})
    if not old_doc:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    old_status = old_doc.get("status")
    new_status = payload.status.value

    # Валидация: для статуса "отказано" обязательна причина (не пустая строка)
    if new_status == OrderStatus.REJECTED.value:
        if not payload.rejection_reason or not payload.rejection_reason.strip():
            raise HTTPException(
                status_code=400, 
                detail="Для статуса 'отказано' необходимо указать причину отказа"
            )

    # Если заказ отклоняется, возвращаем товары на склад
    if new_status == OrderStatus.REJECTED.value and old_status != OrderStatus.REJECTED.value:
        items = old_doc.get("items", [])
        for item in items:
            if item.get("variant_id"):
                await restore_variant_quantity(
                    db, item.get("product_id"), item.get("variant_id"), item.get("quantity", 0)
                )

    update_operations: dict[str, dict] = {
        "$set": {
            "status": payload.status.value,
            "updated_at": datetime.utcnow(),
            "can_edit_address": False,  # Адрес нельзя редактировать после создания
        }
    }

    # Если статус "отказано", сохраняем причину отказа
    if new_status == OrderStatus.REJECTED.value:
        update_operations["$set"]["rejection_reason"] = payload.rejection_reason
    else:
        # Если статус меняется с "отказано" на другой, убираем причину отказа
        update_operations["$unset"] = {"rejection_reason": ""}

    # Атомарно обновляем заказ - только один раз, без дополнительных операций
    doc = await db.orders.find_one_and_update(
        {"_id": as_object_id(order_id)},
        update_operations,
        return_document=True,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    order_payload = Order(**serialize_doc(doc) | {"id": str(doc["_id"])})

    # Отправляем уведомление клиенту об изменении статуса (fire-and-forget для скорости)
    user_id = doc.get("user_id")
    if user_id and old_status != new_status:
        try:
            rejection_reason = doc.get("rejection_reason") if new_status == OrderStatus.REJECTED.value else None
            asyncio.create_task(
                notify_customer_order_status(
                    user_id=user_id,
                    order_id=order_id,
                    order_status=new_status,
                    customer_name=doc.get("customer_name"),
                    rejection_reason=rejection_reason,
                )
            )
        except Exception:
            pass  # Игнорируем ошибки уведомлений

    return order_payload


@router.post("/admin/order/{order_id}/quick-accept", response_model=Order)
async def quick_accept_order(
    order_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """
    Быстрое принятие заказа (перевод в статус "принят").

    Используется для обработки callback от кнопки в Telegram уведомлении.
    """
    # Получаем заказ
    doc = await db.orders.find_one({"_id": as_object_id(order_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    # Проверяем, что заказ еще не обработан
    current_status = doc.get("status")
    if current_status in [OrderStatus.ACCEPTED.value, OrderStatus.REJECTED.value]:
        raise HTTPException(status_code=400, detail=f"Заказ уже обработан. Текущий статус: {current_status}")

    # Обновляем статус на "принят"
    old_status = doc.get("status")
    update_ops = {
        "$set": {
            "status": OrderStatus.ACCEPTED.value,
            "updated_at": datetime.utcnow(),
            "can_edit_address": False,
        }
    }
    # Убираем причину отказа если она была
    if old_status == OrderStatus.REJECTED.value:
        update_ops["$unset"] = {"rejection_reason": ""}
    
    updated = await db.orders.find_one_and_update(
        {"_id": as_object_id(order_id)},
        update_ops,
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    # Отправляем уведомление клиенту об изменении статуса
    user_id = updated.get("user_id")
    if user_id and old_status != OrderStatus.ACCEPTED.value:
        try:
            await notify_customer_order_status(
                user_id=user_id,
                order_id=order_id,
                order_status=OrderStatus.ACCEPTED.value,
                customer_name=updated.get("customer_name"),
                rejection_reason=None,
            )
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")

    return Order(**serialize_doc(updated) | {"id": str(updated["_id"])})


@router.delete("/admin/order/{order_id}")
async def delete_order(
    order_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Удаляет заказ (мягкое удаление)."""
    order_oid = as_object_id(order_id)
    doc = await db.orders.find_one({"_id": order_oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    # Помечаем заказ как удаленный
    deleted = await mark_order_as_deleted(db, order_id)
    if not deleted:
        raise HTTPException(status_code=400, detail="Не удалось удалить заказ")

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/admin/broadcast", response_model=BroadcastResponse)
async def send_broadcast(
    payload: BroadcastRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    _admin_id: int = Depends(verify_admin),
):
    """Send broadcast message to all users with production-ready error handling and rate limiting."""
    import logging
    import time
    
    logger = logging.getLogger(__name__)
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN не настроен. Добавьте токен бота в .env файл.")

    # Валидация размера сообщения (Telegram лимит: 4096 символов)
    message_text = f"{payload.title}\n\n{payload.message}"
    if payload.link:
        message_text += f"\n\n🔗 {payload.link}"
    
    if len(message_text) > 4096:
        raise HTTPException(
            status_code=400,
            detail=f"Сообщение слишком длинное ({len(message_text)} символов). Максимум 4096 символов."
        )

    batch_size = 50  # Размер батча для чтения из БД
    concurrency = 25  # Параллельные отправки (макс. 30 - лимит Telegram)
    customers_cursor = db.customers.find({}, {"telegram_id": 1})

    # Отправляем сообщения через Telegram Bot API
    bot_api_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    sent_count = 0
    failed_count = 0
    total_count = 0
    invalid_user_ids: list[int] = []
    start_time = time.time()
    
    # Rate limiting для Telegram API (30 сообщений в секунду)
    # Используем sliding window для контроля скорости
    last_send_times: list[float] = []

    async def send_to_customer_with_retry(
        client: httpx.AsyncClient,
        telegram_id: int,
        max_retries: int = 3,
    ) -> tuple[bool, bool]:
        """Отправляет сообщение с retry логикой и обработкой rate limits."""
        for attempt in range(max_retries):
            try:
                # Rate limiting: ждем если нужно
                now = time.time()
                if last_send_times:
                    # Удаляем старые записи (старше 1 секунды)
                    last_send_times[:] = [t for t in last_send_times if now - t < 1.0]
                    
                    # Если достигли лимита (30 в секунду), ждем
                    if len(last_send_times) >= 30:
                        sleep_time = 1.0 - (now - last_send_times[0])
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                            now = time.time()
                            last_send_times[:] = [t for t in last_send_times if now - t < 1.0]
                
                last_send_times.append(now)
                
                response = await client.post(
                    bot_api_url,
                    json={
                        "chat_id": telegram_id,
                        "text": message_text,
                    },
                    timeout=15.0,  # Увеличенный timeout для надежности
                )
                result = response.json()
                
                if result.get("ok"):
                    return True, False
                
                error_code = result.get("error_code")
                description = (result.get("description") or "").lower()
                
                # Обработка rate limit от Telegram (429)
                if error_code == 429:
                    retry_after = result.get("parameters", {}).get("retry_after", 1)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_after)
                        continue
                    return False, False
                
                # Временные ошибки сервера (503, 502, 500) - retry
                if response.status_code in {500, 502, 503} and attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 0.5  # Exponential backoff: 0.5s, 1s, 2s
                    await asyncio.sleep(wait_time)
                    continue
                
                # Постоянные ошибки (невалидные пользователи)
                invalid_phrases = (
                    "chat not found", "user not found", "blocked", "bot blocked",
                    "bot was blocked", "user is deactivated", "receiver not found",
                    "chat_id is empty", "peer_id_invalid"
                )
                is_invalid = error_code in {400, 403, 404} or any(
                    phrase in description for phrase in invalid_phrases
                )
                return False, is_invalid
                
            except httpx.TimeoutException:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 0.5
                    await asyncio.sleep(wait_time)
                    continue
                return False, False
            except httpx.HTTPStatusError as exc:
                # Временные ошибки сети
                if exc.response.status_code in {500, 502, 503, 429} and attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 0.5
                    await asyncio.sleep(wait_time)
                    continue
                is_invalid = exc.response.status_code in {400, 403, 404}
                return False, is_invalid
            except Exception as e:
                # Логируем неожиданные ошибки при последней попытке
                if attempt == max_retries - 1:
                    logger.warning(f"Ошибка при отправке сообщения пользователю {telegram_id}: {type(e).__name__}")
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 0.5
                    await asyncio.sleep(wait_time)
                    continue
                return False, False
        
        return False, False

    async def flush_invalids():
        nonlocal failed_count, invalid_user_ids
        if not invalid_user_ids:
            return
        chunk = invalid_user_ids.copy()
        invalid_user_ids.clear()
        failed_count += len(chunk)
        try:
            await db.customers.delete_many({"telegram_id": {"$in": chunk}})
        except Exception as e:
            logger.error(f"Ошибка при удалении невалидных пользователей: {e}")

    # Используем connection pooling для лучшей производительности
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    try:
        async with httpx.AsyncClient(timeout=15.0, limits=limits) as client:
            batch_num = 0
            while True:
                batch = await customers_cursor.to_list(length=batch_size)
                if not batch:
                    break
                
                batch_num += 1
                total_count += len(batch)
                telegram_ids = [customer["telegram_id"] for customer in batch]

                # Логируем прогресс каждые 10 батчей
                if batch_num % 10 == 0:
                    elapsed = time.time() - start_time
                    rate = sent_count / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"Рассылка: обработано {total_count} пользователей, "
                        f"отправлено {sent_count}, ошибок {failed_count}, "
                        f"скорость {rate:.1f} сообщений/сек"
                    )

                # Ограничиваем конкуренцию, разбивая на подгруппы
                for i in range(0, len(telegram_ids), concurrency):
                    chunk = telegram_ids[i : i + concurrency]
                    results = await asyncio.gather(
                        *[send_to_customer_with_retry(client, telegram_id) for telegram_id in chunk],
                        return_exceptions=True,  # Обрабатываем исключения
                    )
                    for telegram_id, result in zip(chunk, results):
                        if isinstance(result, Exception):
                            logger.warning(f"Исключение при отправке пользователю {telegram_id}: {result}")
                            failed_count += 1
                            continue
                        sent, invalid = result
                        if sent:
                            sent_count += 1
                        elif invalid:
                            invalid_user_ids.append(telegram_id)
                        else:
                            failed_count += 1
    finally:
        # Финальная очистка невалидных пользователей (гарантированно выполняется даже при ошибках)
        await flush_invalids()

    elapsed_time = time.time() - start_time
    rate = sent_count / elapsed_time if elapsed_time > 0 else 0

    # Логируем итоговую статистику
    logger.info(
        f"Рассылка завершена: всего {total_count}, отправлено {sent_count}, "
        f"ошибок {failed_count}, время {elapsed_time:.1f}с, скорость {rate:.1f} сообщений/сек"
    )

    return BroadcastResponse(success=True, sent_count=sent_count, total_count=total_count, failed_count=failed_count)
