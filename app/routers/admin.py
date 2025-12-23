from datetime import datetime
from typing import List, Optional
import asyncio
import httpx
from bson import ObjectId

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import ServerSelectionTimeoutError, ConnectionFailure

from ..database import get_db
from ..schemas import (
  BroadcastRequest,
  BroadcastResponse,
  Order,
  OrderStatus,
  PaginatedOrdersResponse,
  UpdateStatusRequest,
)
from ..utils import (
  as_object_id,
  serialize_doc,
  restore_variant_quantity,
  mark_order_as_deleted,
  restore_order_entry,
  get_gridfs,
)
from ..config import get_settings
from ..auth import verify_admin
from ..notifications import notify_customer_order_status

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
  # Оптимизированное построение запроса
    query = {}
    if status_filter:
    query["status"] = {"$in": [OrderStatus.PROCESSING.value, OrderStatus.NEW.value]} if status_filter == OrderStatus.PROCESSING else status_filter.value
    if not include_deleted:
      query["deleted_at"] = {"$exists": False}
    if cursor:
      try:
        query["_id"] = {"$lt": as_object_id(cursor)}
      except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный cursor")

    # Используем индекс для быстрой сортировки
    docs = await (
      db.orders.find(query)
      .sort("_id", -1)
    .hint([("status", 1), ("created_at", -1)])
      .limit(limit + 1)
      .to_list(length=limit + 1)
    )
  
  # Оптимизированная валидация заказов
    orders = []
    for doc in docs:
      try:
      orders.append(Order(**serialize_doc(doc) | {"id": str(doc["_id"])}))
    except:
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
  """
  Получает чек заказа из GridFS для администратора.
  """
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
      }
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
  # Получаем старый статус заказа
  old_doc = await db.orders.find_one({"_id": as_object_id(order_id)})
  if not old_doc:
    raise HTTPException(status_code=404, detail="Заказ не найден")
  
  old_status = old_doc.get("status")
  new_status = payload.status.value
  
  # Если заказ отменяется, возвращаем товары на склад
  if new_status == OrderStatus.CANCELED.value and old_status != OrderStatus.CANCELED.value:
    items = old_doc.get("items", [])
    for item in items:
      if item.get("variant_id"):
        await restore_variant_quantity(
          db,
          item.get("product_id"),
          item.get("variant_id"),
          item.get("quantity", 0)
        )
  
  editable_statuses = {
    OrderStatus.PROCESSING.value,
  }
  should_archive = new_status == OrderStatus.DONE.value

  update_operations: dict[str, dict] = {
    "$set": {
      "status": payload.status.value,
      "updated_at": datetime.utcnow(),
      "can_edit_address": payload.status.value in editable_statuses,
    }
  }

  # Если заказ был завершён и мы изменяем статус на другой, убираем метку deleted_at полностью
  if old_status == OrderStatus.DONE.value and new_status != OrderStatus.DONE.value:
    update_operations["$unset"] = {"deleted_at": ""}
  # Если заказ завершается, сразу помечаем как удаленный (в одной атомарной операции)
  elif should_archive:
    update_operations["$set"]["deleted_at"] = datetime.utcnow()

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
      asyncio.create_task(notify_customer_order_status(
        user_id=user_id,
        order_id=order_id,
        order_status=new_status,
        customer_name=doc.get("customer_name"),
      ))
    except:
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
  
  # Проверяем, что заказ еще в обработке
  if doc.get("status") != OrderStatus.PROCESSING.value:
    raise HTTPException(
      status_code=400, 
      detail=f"Заказ уже обработан. Текущий статус: {doc.get('status')}"
    )
  
  # Обновляем статус на "принят"
  old_status = doc.get("status")
  updated = await db.orders.find_one_and_update(
    {"_id": as_object_id(order_id)},
    {
      "$set": {
        "status": OrderStatus.ACCEPTED.value,
        "updated_at": datetime.utcnow(),
        "can_edit_address": False,
      }
    },
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
      )
    except Exception as e:
      import logging
      logger = logging.getLogger(__name__)
      logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")
  
  return Order(**serialize_doc(updated) | {"id": str(updated["_id"])})


@router.post("/admin/order/{order_id}/restore", response_model=Order)
async def restore_order(
  order_id: str,
  db: AsyncIOMotorDatabase = Depends(get_db),
  _admin_id: int = Depends(verify_admin),
):
  """
  Восстанавливает удаленный заказ в течение 10 минут после завершения.
  """
  from datetime import datetime, timedelta
  
  order_oid = as_object_id(order_id)
  doc = await db.orders.find_one({"_id": order_oid})
  if not doc:
    raise HTTPException(status_code=404, detail="Заказ не найден")
  
  deleted_at = doc.get("deleted_at")
  if not deleted_at:
    raise HTTPException(status_code=400, detail="Заказ не был удален")
  
  # Проверяем, что прошло не более 10 минут
  if isinstance(deleted_at, datetime):
    time_diff = datetime.utcnow() - deleted_at
    if time_diff > timedelta(minutes=10):
      raise HTTPException(
        status_code=400,
        detail="Время восстановления истекло. Заказ был окончательно удален."
      )
  
  # Восстанавливаем заказ
  restored = await restore_order_entry(db, order_oid)
  if not restored:
    raise HTTPException(status_code=400, detail="Не удалось восстановить заказ")
  
  # Получаем обновленный документ
  updated = await db.orders.find_one({"_id": order_oid})
  if not updated:
    raise HTTPException(status_code=404, detail="Заказ не найден после восстановления")
  
  return Order(**serialize_doc(updated) | {"id": str(updated["_id"])})


@router.post("/admin/broadcast", response_model=BroadcastResponse)
async def send_broadcast(
  payload: BroadcastRequest,
  db: AsyncIOMotorDatabase = Depends(get_db),
  _admin_id: int = Depends(verify_admin),
):
  settings = get_settings()
  if not settings.telegram_bot_token:
    raise HTTPException(
      status_code=500,
      detail="TELEGRAM_BOT_TOKEN не настроен. Добавьте токен бота в .env файл."
    )

  batch_size = max(1, settings.broadcast_batch_size)
  concurrency = max(1, settings.broadcast_concurrency)
  customers_cursor = db.customers.find({}, {"telegram_id": 1})

  # Формируем текст сообщения (без Markdown, чтобы избежать ошибок парсинга)
  message_text = f"{payload.title}\n\n{payload.message}"
  if payload.link:
    message_text += f"\n\n🔗 {payload.link}"

  # Отправляем сообщения через Telegram Bot API
  bot_api_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
  sent_count = 0
  failed_count = 0
  total_count = 0
  invalid_user_ids: list[int] = []

  async def send_to_customer(
    client: httpx.AsyncClient,
    telegram_id: int,
  ) -> tuple[bool, bool]:
    try:
      response = await client.post(
        bot_api_url,
        json={
          "chat_id": telegram_id,
          "text": message_text,
        },
      )
      payload = response.json()
      if payload.get("ok"):
        return True, False
      error_code = payload.get("error_code")
      description = (payload.get("description") or "").lower()
      is_invalid = error_code in {400, 403, 404} or any(
        phrase in description for phrase in ("chat not found", "user not found", "blocked")
      )
      return False, is_invalid
    except httpx.HTTPStatusError as exc:
      return False, exc.response.status_code in {400, 403, 404}
    except Exception:
      return False, False

  async def flush_invalids():
    nonlocal failed_count, invalid_user_ids
    if not invalid_user_ids:
      return
    chunk = invalid_user_ids
    invalid_user_ids = []
    failed_count += len(chunk)
    await db.customers.delete_many({"telegram_id": {"$in": chunk}})

  async with httpx.AsyncClient(timeout=10.0) as client:
    while True:
      batch = await customers_cursor.to_list(length=batch_size)
      if not batch:
        break
      total_count += len(batch)
      telegram_ids = [customer["telegram_id"] for customer in batch]

      # Ограничиваем конкуренцию, разбивая на подгруппы
      for i in range(0, len(telegram_ids), concurrency):
        chunk = telegram_ids[i:i + concurrency]
        results = await asyncio.gather(
          *[send_to_customer(client, telegram_id) for telegram_id in chunk],
          return_exceptions=False,
        )
        for telegram_id, (sent, invalid) in zip(chunk, results):
          if sent:
            sent_count += 1
          if invalid:
            invalid_user_ids.append(telegram_id)

      if len(invalid_user_ids) >= 500:
        await flush_invalids()

  await flush_invalids()

  # Сохраняем запись о рассылке с статистикой
  entry = {
    "title": payload.title,
    "message": payload.message,
    "segment": payload.segment,
    "link": payload.link,
    "total_count": total_count,
    "sent_count": sent_count,
    "failed_count": failed_count,
    "created_at": datetime.utcnow(),
  }
  await db.broadcasts.insert_one(entry)

  return BroadcastResponse(
    success=True,
    sent_count=sent_count,
    total_count=total_count,
    failed_count=failed_count
  )

