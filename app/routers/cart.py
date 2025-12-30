"""Модуль для работы с корзиной покупок."""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from ..database import get_db
from ..schemas import AddToCartRequest, Cart, RemoveFromCartRequest, UpdateCartItemRequest
from ..security import TelegramUser, get_current_user
from ..utils import as_object_id, decrement_variant_quantity, normalize_product_images, restore_variant_quantity, serialize_doc

# Время жизни корзины в минутах
CART_EXPIRY_MINUTES = 15

router = APIRouter(tags=["cart"])


def normalize_cart(cart: dict) -> dict:
    """Оптимизированная нормализация корзины (минимальные проверки)."""
    items = cart.get("items") or []
    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = item.get("product_id")
        price = item.get("price")
        if not product_id or price is None:
            continue
        # Минимальная нормализация для скорости
        normalized_items.append(
            {
                "id": item.get("id") or uuid4().hex,
                "product_id": product_id,
                "product_name": item.get("product_name") or "Товар",
                "quantity": max(1, int(item.get("quantity", 0))),
                "price": float(price),
                "image": item.get("image"),
                "variant_id": item.get("variant_id"),
                "variant_name": item.get("variant_name"),
            }
        )
    cart["items"] = normalized_items
    recalculate_total(cart)
    return cart


async def cleanup_expired_cart(db: AsyncIOMotorDatabase, cart: dict):
    """Очищает просроченную корзину и возвращает товары на склад."""
    if not cart or not cart.get("items"):
        return False

    updated_at = cart.get("updated_at")
    if not updated_at:
        updated_at = cart.get("created_at", datetime.utcnow())

    # Оптимизированная проверка истечения
    if not isinstance(updated_at, datetime):
        if isinstance(updated_at, str):
            try:
                updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except Exception:
                updated_at = datetime.utcnow()
        else:
            updated_at = datetime.utcnow()
    expiry_time = updated_at + timedelta(minutes=CART_EXPIRY_MINUTES)

    if datetime.utcnow() > expiry_time:
        # Возвращаем все товары на склад
        for item in cart.get("items", []):
            if item.get("variant_id"):
                await restore_variant_quantity(
                    db, item.get("product_id"), item.get("variant_id"), item.get("quantity", 0)
                )

        # Удаляем корзину
        await db.carts.delete_one({"_id": cart["_id"]})
        return True
    return False


async def cleanup_expired_carts_periodic():
    """
    Фоновая задача для периодической очистки просроченных корзин.
    
    Оптимизированная версия: использует индекс на updated_at, обрабатывает небольшие батчи,
    работает реже в production для снижения нагрузки на сервер.
    """
    import logging
    from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError
    from ..database import get_db
    
    logger = logging.getLogger(__name__)
    
    # Настройки для оптимизации нагрузки
    BATCH_SIZE = 50  # Обрабатываем меньше корзин за раз
    PRODUCTION_INTERVAL = 600  # 10 минут
    
    while True:
        try:
            # Получаем базу данных
            db = await get_db()
            if db is None:
                await asyncio.sleep(60)
                continue

            # Находим корзины, которые не обновлялись более CART_EXPIRY_MINUTES минут
            # Используем только updated_at (есть индекс) для быстрого поиска
            cutoff_time = datetime.utcnow() - timedelta(minutes=CART_EXPIRY_MINUTES)
            
            # Оптимизированный запрос: используем индекс на updated_at, загружаем только нужные поля
            expired_carts = await db.carts.find(
                {
                    "items": {"$exists": True, "$ne": []},  # Только корзины с товарами
                    "updated_at": {"$lte": cutoff_time}  # Используем индекс
                },
                {
                    "_id": 1,
                    "items": 1,
                    "updated_at": 1,
                    "created_at": 1
                }  # Проекция: загружаем только нужные поля
            ).limit(BATCH_SIZE).to_list(length=BATCH_SIZE)
            
            # Также проверяем корзины без updated_at (используем created_at)
            # Это редкий случай, поэтому проверяем отдельно и с меньшим лимитом
            carts_without_updated = await db.carts.find(
                {
                    "items": {"$exists": True, "$ne": []},
                    "updated_at": {"$exists": False},
                    "created_at": {"$lte": cutoff_time}
                },
                {
                    "_id": 1,
                    "items": 1,
                    "created_at": 1
                }
            ).limit(10).to_list(length=10)
            
            expired_carts.extend(carts_without_updated)

            cleaned_count = 0
            for cart in expired_carts:
                try:
                    # Используем существующую функцию для очистки
                    if await cleanup_expired_cart(db, cart):
                        cleaned_count += 1
                except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
                    # Временные проблемы с подключением - пропускаем эту корзину
                    pass
                except Exception as e:
                    logger.error(f"Ошибка при очистке корзины {cart.get('_id')}: {e}")

            if cleaned_count > 0:
                logger.info(f"Очищено просроченных корзин: {cleaned_count}")

            # Ждем перед следующей проверкой (10 минут для экономии ресурсов)
            await asyncio.sleep(PRODUCTION_INTERVAL)
        except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
            # Временные проблемы с подключением - игнорируем
            await asyncio.sleep(PRODUCTION_INTERVAL)
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче очистки корзин: {e}")
            await asyncio.sleep(PRODUCTION_INTERVAL)


async def get_cart_document(db: AsyncIOMotorDatabase, user_id: int, check_expiry: bool = True):
    """Получает документ корзины пользователя."""
    cart = await db.carts.find_one({"user_id": user_id})
    if not cart:
        cart = {
            "user_id": user_id,
            "items": [],
            "total_amount": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        try:
            result = await db.carts.insert_one(cart)
            cart["_id"] = result.inserted_id
        except DuplicateKeyError:
            # Корзина уже была создана параллельным запросом - получаем её
            cart = await db.carts.find_one({"user_id": user_id})
            if not cart:
                # Если всё ещё не найдена (крайне редкий случай), создаём заново
                cart = {
                    "user_id": user_id,
                    "items": [],
                    "total_amount": 0,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
                result = await db.carts.insert_one(cart)
                cart["_id"] = result.inserted_id
    elif check_expiry:
        # Оптимизированная проверка истечения
        updated_at = cart.get("updated_at") or cart.get("created_at", datetime.utcnow())
        if not isinstance(updated_at, datetime):
            if isinstance(updated_at, str):
                try:
                    updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                except Exception:
                    updated_at = datetime.utcnow()
            else:
                updated_at = datetime.utcnow()

        if datetime.utcnow() > updated_at + timedelta(minutes=CART_EXPIRY_MINUTES):
            # Очищаем корзину в фоне, не блокируя ответ
            asyncio.create_task(cleanup_expired_cart(db, cart))
            # Создаем новую корзину сразу
            cart = {
                "user_id": user_id,
                "items": [],
                "total_amount": 0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            try:
                result = await db.carts.insert_one(cart)
                cart["_id"] = result.inserted_id
            except DuplicateKeyError:
                # Корзина уже была создана параллельным запросом - получаем её
                cart = await db.carts.find_one({"user_id": user_id})
                if not cart:
                    # Если всё ещё не найдена, создаём заново
                    cart = {
                        "user_id": user_id,
                        "items": [],
                        "total_amount": 0,
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                    result = await db.carts.insert_one(cart)
                    cart["_id"] = result.inserted_id
    return cart


def recalculate_total(cart):
    """Пересчитывает общую сумму корзины."""
    cart["total_amount"] = round(
        sum((item.get("price") or 0) * (item.get("quantity") or 0) for item in cart.get("items", [])), 2
    )
    return cart


@router.get("/cart", response_model=Cart)
async def get_cart(
    current_user: TelegramUser = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Получает корзину пользователя."""
    # Быстрое получение корзины без блокирующей проверки истечения
    user_id = current_user.id
    cart = await get_cart_document(db, user_id, check_expiry=True)
    safe_cart = normalize_cart(cart)
    
    # Сериализуем документ и убеждаемся, что _id правильно обработан
    serialized = serialize_doc(safe_cart)
    cart_id = str(cart.get("_id", ""))
    if not cart_id or cart_id == "None":
        raise HTTPException(status_code=500, detail="Ошибка: корзина не имеет идентификатора")
    
    # Создаем модель Cart, исключая служебные поля, которые не нужны в ответе
    cart_data = {k: v for k, v in serialized.items() if k not in ("_id", "created_at", "updated_at")}
    cart_data["id"] = cart_id
    cart_data["user_id"] = user_id
    
    return Cart(**cart_data)


@router.post("/cart", response_model=Cart)
async def add_to_cart(
    payload: AddToCartRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: TelegramUser = Depends(get_current_user),
):
    """Добавляет товар в корзину."""
    user_id = current_user.id
    try:
        product_oid = as_object_id(payload.product_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный идентификатор товара")

    # Получаем товар и корзину параллельно для оптимизации
    # Для добавления в корзину не нужно проверять истечение (это делается при GET)
    # Используем проекцию для товара - не загружаем лишние поля
    # Включаем и image, и images для нормализации
    product_task = db.products.find_one(
        {"_id": product_oid},
        {
            "name": 1,
            "price": 1,
            "image": 1,
            "images": 1,
            "variants": 1,
            "_id": 1,
        },
    )
    cart_task = get_cart_document(db, user_id, check_expiry=False)

    product, cart = await asyncio.gather(product_task, cart_task)

    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")
    
    # Нормализуем изображения товара для консистентности
    product = normalize_product_images(product)

    # Вариации обязательны для всех товаров
    variants = product.get("variants", [])
    if not variants or len(variants) == 0:
        raise HTTPException(
            status_code=400, detail="Товар не может быть продан без вариаций (вкусов). Обратитесь к администратору."
        )

    # Проверяем вариацию (variant_id теперь обязателен в схеме, но проверяем существование)
    variant = next((v for v in variants if v.get("id") == payload.variant_id), None)
    if not variant:
        raise HTTPException(status_code=404, detail="Вариация не найдена")

    variant_name = variant.get("name")
    variant_price = product.get("price", 0)
    try:
        variant_quantity = int(variant.get("quantity", 0))
    except Exception:
        variant_quantity = 0

    # Используем атомарные операции MongoDB для обновления корзины и списания товара
    now = datetime.utcnow()

    # Вычисляем изменение total_amount заранее
    price_delta = variant_price * payload.quantity

    # Перечитываем корзину для актуальных данных (защита от race conditions)
    fresh_cart = await db.carts.find_one({"_id": cart["_id"]}, {"items": 1})
    if not fresh_cart:
        raise HTTPException(status_code=404, detail="Корзина не найдена")

    # Оптимизированный поиск существующего товара
    items = fresh_cart.get("items", [])
    existing = next(
        (
            item
            for item in items
            if item.get("product_id") == payload.product_id and item.get("variant_id") == payload.variant_id
        ),
        None,
    )
    already_in_cart = existing.get("quantity", 0) if existing else 0
    total_needed = already_in_cart + payload.quantity

    # Проверка количества с учетом уже добавленного
    if variant_quantity < total_needed:
        can_add = max(0, variant_quantity - already_in_cart)
        if can_add == 0:
            raise HTTPException(
                status_code=400,
                detail=f"Недостаточно товара. В наличии: {variant_quantity}, уже в корзине: {already_in_cart}",
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Недостаточно товара. Доступно для добавления: {can_add}, уже в корзине: {already_in_cart}",
            )

    if existing:
        # Обновляем существующий товар атомарно и списываем товар со склада одновременно
        # Сначала списываем товар
        success = await decrement_variant_quantity(db, payload.product_id, payload.variant_id, payload.quantity)
        if not success:
            raise HTTPException(status_code=400, detail=f"Недостаточно товара. В наличии: {variant_quantity}")

        # Обновляем корзину с пересчетом total_amount в одном запросе
        # Используем актуальный _id из fresh_cart
        final_cart = await db.carts.find_one_and_update(
            {
                "_id": fresh_cart["_id"],
                "items": {"$elemMatch": {"product_id": payload.product_id, "variant_id": payload.variant_id}},
            },
            {"$inc": {"items.$.quantity": payload.quantity, "total_amount": price_delta}, "$set": {"updated_at": now}},
            return_document=True,
        )
    else:
        # Добавляем новый товар атомарно
        new_item = {
            "id": uuid4().hex,
            "product_id": payload.product_id,
            "variant_id": payload.variant_id,
            "product_name": product["name"],
            "variant_name": variant_name,
            "quantity": payload.quantity,
            "price": variant_price,
            # Используем первое изображение из массива images, или fallback на image для обратной совместимости
            "image": (
                (variant.get("images") or [])[0] if variant and variant.get("images") 
                else variant.get("image") if variant 
                else (product.get("images") or [])[0] if product.get("images") 
                else product.get("image")
            ),
        }

        # Сначала списываем товар
        success = await decrement_variant_quantity(db, payload.product_id, payload.variant_id, payload.quantity)
        if not success:
            raise HTTPException(status_code=400, detail=f"Недостаточно товара. В наличии: {variant_quantity}")

        # Обновляем корзину с пересчетом total_amount в одном запросе
        # Используем актуальный _id из fresh_cart
        final_cart = await db.carts.find_one_and_update(
            {"_id": fresh_cart["_id"]},
            {"$push": {"items": new_item}, "$inc": {"total_amount": price_delta}, "$set": {"updated_at": now}},
            return_document=True,
        )

    if not final_cart:
        # Если обновление не удалось, возвращаем товар на склад
        await restore_variant_quantity(db, payload.product_id, payload.variant_id, payload.quantity)
        raise HTTPException(status_code=500, detail="Ошибка при обновлении корзины")

    # Обновление клиента в фоне (fire-and-forget для скорости)
    try:
        asyncio.create_task(
            db.customers.update_one({"telegram_id": user_id}, {"$set": {"last_cart_activity": now}}, upsert=True)
        )
    except Exception:
        pass  # Игнорируем ошибки

    safe_cart = normalize_cart(final_cart)
    return Cart(**serialize_doc(safe_cart) | {"id": str(final_cart["_id"])})


@router.patch("/cart/item", response_model=Cart)
async def update_cart_item(
    payload: UpdateCartItemRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: TelegramUser = Depends(get_current_user),
):
    """Обновляет количество товара в корзине."""
    user_id = current_user.id
    now = datetime.utcnow()
    
    # Получаем только нужные поля корзины для оптимизации
    cart = await db.carts.find_one(
        {"user_id": user_id},
        {"items": 1, "_id": 1}
    )
    if not cart:
        raise HTTPException(status_code=404, detail="Корзина не найдена")
    
    # Находим товар в корзине
    item = next((item for item in cart.get("items", []) if item.get("id") == payload.item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден в корзине")

    old_quantity = item.get("quantity", 0)
    quantity_diff = payload.quantity - old_quantity
    
    if quantity_diff == 0:
        # Количество не изменилось, просто возвращаем корзину
        cart = await get_cart_document(db, user_id, check_expiry=False)
        safe_cart = normalize_cart(cart)
        return Cart(**serialize_doc(safe_cart) | {"id": str(cart["_id"])})

    # Обработка изменения количества на складе
    if item.get("variant_id") and quantity_diff != 0:
        product = await db.products.find_one(
            {"_id": as_object_id(item["product_id"])},
            {"variants": 1}
        )
        if product:
            variant = next((v for v in product.get("variants", []) if v.get("id") == item.get("variant_id")), None)
            if variant:
                try:
                    variant_quantity = int(variant.get("quantity", 0))
                except Exception:
                    variant_quantity = 0

                if quantity_diff > 0:
                    if variant_quantity < quantity_diff:
                        raise HTTPException(
                            status_code=400, detail=f"Недостаточно товара. В наличии: {variant_quantity}"
                        )
                    if not await decrement_variant_quantity(
                        db, item["product_id"], item.get("variant_id"), quantity_diff
                    ):
                        raise HTTPException(status_code=400, detail="Недостаточно товара")
                else:
                    await restore_variant_quantity(
                        db, item["product_id"], item.get("variant_id"), abs(quantity_diff)
                    )

    # Вычисляем изменение total_amount
    price = item.get("price", 0)
    price_delta = price * quantity_diff

    # Атомарное обновление корзины с пересчетом total_amount в MongoDB
    final_cart = await db.carts.find_one_and_update(
        {
            "_id": cart["_id"],
            "items.id": payload.item_id
        },
        {
            "$set": {"items.$.quantity": payload.quantity, "updated_at": now},
            "$inc": {"total_amount": price_delta}
        },
        return_document=True
    )
    
    if not final_cart:
        # Если обновление не удалось, возвращаем товар на склад
        if item.get("variant_id") and quantity_diff > 0:
            await restore_variant_quantity(db, item["product_id"], item.get("variant_id"), quantity_diff)
        raise HTTPException(status_code=500, detail="Ошибка при обновлении корзины")

    safe_cart = normalize_cart(final_cart)
    return Cart(**serialize_doc(safe_cart) | {"id": str(final_cart["_id"])})


@router.delete("/cart/item", response_model=Cart)
async def remove_from_cart(
    payload: RemoveFromCartRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: TelegramUser = Depends(get_current_user),
):
    """Удаляет товар из корзины."""
    user_id = current_user.id
    now = datetime.utcnow()
    
    # Получаем только нужные поля корзины для оптимизации
    cart = await db.carts.find_one(
        {"user_id": user_id},
        {"items": 1, "_id": 1}
    )
    if not cart:
        raise HTTPException(status_code=404, detail="Корзина не найдена")
    
    # Находим товар для удаления
    item_to_remove = next((item for item in cart.get("items", []) if item.get("id") == payload.item_id), None)
    if not item_to_remove:
        raise HTTPException(status_code=404, detail="Товар не найден в корзине")

    # Вычисляем сумму удаляемого товара для пересчета total_amount
    price = item_to_remove.get("price", 0)
    quantity = item_to_remove.get("quantity", 0)
    price_delta = -(price * quantity)

    # Возвращаем товар на склад при удалении из корзины
    if item_to_remove.get("variant_id"):
        await restore_variant_quantity(
            db, item_to_remove["product_id"], item_to_remove.get("variant_id"), quantity
        )

    # Атомарное удаление товара из корзины с пересчетом total_amount в MongoDB
    final_cart = await db.carts.find_one_and_update(
        {"_id": cart["_id"]},
        {
            "$pull": {"items": {"id": payload.item_id}},
            "$inc": {"total_amount": price_delta},
            "$set": {"updated_at": now}
        },
        return_document=True
    )
    
    if not final_cart:
        raise HTTPException(status_code=500, detail="Ошибка при удалении товара из корзины")

    safe_cart = normalize_cart(final_cart)
    return Cart(**serialize_doc(safe_cart) | {"id": str(final_cart["_id"])})


@router.delete("/cart", response_model=Cart)
async def clear_cart(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: TelegramUser = Depends(get_current_user),
):
    """Очищает корзину и возвращает все товары на склад."""
    cart = await get_cart_document(db, current_user.id, check_expiry=False)

    # Возвращаем все товары на склад
    for item in cart.get("items", []):
        if item.get("variant_id"):
            await restore_variant_quantity(db, item.get("product_id"), item.get("variant_id"), item.get("quantity", 0))

    # Очищаем корзину
    cart["items"] = []
    cart["total_amount"] = 0
    cart["updated_at"] = datetime.utcnow()
    await db.carts.update_one({"_id": cart["_id"]}, {"$set": cart})
    safe_cart = normalize_cart(cart)
    return Cart(**serialize_doc(safe_cart) | {"id": str(cart["_id"])})
