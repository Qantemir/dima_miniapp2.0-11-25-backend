from datetime import datetime
from pathlib import Path
from uuid import uuid4
from bson import ObjectId
import asyncio

from fastapi import (
  APIRouter,
  Depends,
  File,
  Form,
  HTTPException,
  UploadFile,
  Response,
  status,
)
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..config import settings
from ..database import get_db
from ..schemas import (
  Cart,
  Order,
  OrderStatus,
  UpdateAddressRequest,
)
from ..utils import as_object_id, serialize_doc, get_gridfs, ensure_store_is_awake
from ..security import TelegramUser, get_current_user
from ..notifications import notify_admins_new_order

router = APIRouter(tags=["orders"])

async def get_cart(db: AsyncIOMotorDatabase, user_id: int) -> Cart | None:
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
  """
  Сохраняет чек в GridFS и возвращает file_id и оригинальное имя файла.
  """
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
    raise HTTPException(
      status_code=400,
      detail=f"Ошибка при чтении файла: {str(e)}"
    )

  if not file_bytes:
    raise HTTPException(status_code=400, detail="Файл чека пустой")
  if len(file_bytes) > MAX_RECEIPT_SIZE_BYTES:
    raise HTTPException(
      status_code=400,
      detail=f"Файл слишком большой. Максимум {settings.max_receipt_size_mb} МБ",
    )

  # Сохраняем в GridFS используя синхронный клиент
  # Выполняем в executor, чтобы не блокировать event loop
  loop = asyncio.get_event_loop()
  fs = get_gridfs()
  filename = f"{uuid4().hex}{extension}"
  
  # Определяем content_type для GridFS
  gridfs_content_type = content_type if content_type else f"application/octet-stream"
  
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
        }
      )
    )
  except Exception as e:
    raise HTTPException(
      status_code=500,
      detail=f"Ошибка при сохранении файла в GridFS: {str(e)}"
    )
  
  # Возвращаем file_id как строку и оригинальное имя файла
  return str(file_id), file.filename


@router.post("/order", response_model=Order, status_code=status.HTTP_201_CREATED)
async def create_order(
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
  user_id = current_user.id
  await ensure_store_is_awake(db)
  cart = await get_cart(db, user_id)
  if not cart:
    raise HTTPException(status_code=400, detail="Корзина пуста")

  # Оптимизированная проверка доступности (только для товаров с variant_id)
  if cart.items:
    check_tasks = [
      db.products.find_one({"_id": as_object_id(item.product_id)}, {"variants": 1})
      for item in cart.items if item.variant_id
    ]
    if check_tasks:
      products = await asyncio.gather(*check_tasks, return_exceptions=True)
      for product, item in zip(products, [item for item in cart.items if item.variant_id]):
        if isinstance(product, dict):
          variant = next((v for v in product.get("variants", []) if v.get("id") == item.variant_id), None)
          if variant and variant.get("quantity", 0) < 0:
              raise HTTPException(
                status_code=400,
              detail=f"Товар '{item.product_name}' больше не доступен"
              )

  receipt_file_id, original_filename = await _save_payment_receipt(db, payment_receipt)

  order_doc = {
    "user_id": user_id,
    "customer_name": name,
    "customer_phone": phone,
    "delivery_address": address,
    "comment": comment,
    "status": OrderStatus.PROCESSING.value,
    "items": [item.dict() for item in cart.items],  # Преобразуем CartItem объекты в словари
    "total_amount": cart.total_amount,
    "can_edit_address": True,
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
        asyncio.get_event_loop().run_in_executor(
          None, lambda: get_gridfs().delete(ObjectId(receipt_file_id))
        )
      )
    except:
      pass
    raise

  # Удаляем корзину и получаем заказ параллельно
  cart_delete_task = db.carts.delete_one({"_id": as_object_id(cart.id)})
  order_doc_task = db.orders.find_one({"_id": result.inserted_id})
  await cart_delete_task  # Ждем только удаление корзины
  doc = await order_doc_task
  order = Order(**serialize_doc(doc) | {"id": str(doc["_id"])})
  
  # Отправляем уведомление администраторам о новом заказе
  try:
    await notify_admins_new_order(
      order_id=order.id,
      customer_name=name,
      customer_phone=phone,
      delivery_address=address,
      total_amount=cart.total_amount,
      items=[item.dict() for item in cart.items],
      user_id=user_id,
      receipt_file_id=receipt_file_id,
      db=db,
    )
  except Exception:
    # Игнорируем ошибки уведомлений для скорости (не критично)
    pass
  
  return order


@router.get("/order/last", response_model=Order | None)
async def get_last_order(
  current_user: TelegramUser = Depends(get_current_user),
  db: AsyncIOMotorDatabase = Depends(get_db),
):
  # Используем индекс для быстрого поиска последнего заказа
  # Составной индекс [("user_id", 1), ("created_at", -1)] уже создан в database.py
  doc = await db.orders.find_one(
    {"user_id": current_user.id},
    sort=[("created_at", -1)],
  )
  if not doc:
    return None
  return Order(**serialize_doc(doc) | {"id": str(doc["_id"])})


@router.get("/order/{order_id}", response_model=Order)
async def get_order_by_id(
  order_id: str,
  current_user: TelegramUser = Depends(get_current_user),
  db: AsyncIOMotorDatabase = Depends(get_db),
):
  doc = await db.orders.find_one(
    {"_id": as_object_id(order_id), "user_id": current_user.id},
  )
  if not doc:
    raise HTTPException(status_code=404, detail="Заказ не найден")
  return Order(**serialize_doc(doc) | {"id": str(doc["_id"])})


@router.get("/order/{order_id}/receipt")
async def get_order_receipt(
  order_id: str,
  db: AsyncIOMotorDatabase = Depends(get_db),
  current_user: TelegramUser = Depends(get_current_user),
):
  """
  Получает чек заказа из GridFS.
  """
  doc = await db.orders.find_one(
    {"_id": as_object_id(order_id), "user_id": current_user.id},
  )
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


@router.patch("/order/{order_id}/address", response_model=Order)
async def update_order_address(
  order_id: str,
  payload: UpdateAddressRequest,
  db: AsyncIOMotorDatabase = Depends(get_db),
  current_user: TelegramUser = Depends(get_current_user),
):
  doc = await db.orders.find_one({"_id": as_object_id(order_id)})
  if not doc or doc["user_id"] != current_user.id:
    raise HTTPException(status_code=404, detail="Заказ не найден")
  editable_statuses = {
    OrderStatus.PROCESSING.value,
  }
  if doc["status"] not in editable_statuses:
    raise HTTPException(status_code=400, detail="Адрес можно менять только для новых заказов или заказов в работе")

  updated = await db.orders.find_one_and_update(
    {"_id": as_object_id(order_id)},
    {
      "$set": {
        "delivery_address": payload.address,
        "updated_at": datetime.utcnow(),
        "can_edit_address": True,
      }
    },
    return_document=True,
  )
  return Order(**serialize_doc(updated) | {"id": str(updated["_id"])})

