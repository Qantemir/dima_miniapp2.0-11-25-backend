"""
Webhook для обработки callback от Telegram Bot API (кнопки в сообщениях).
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase
import httpx

from ..database import get_db
from ..config import get_settings
from ..schemas import OrderStatus
from ..utils import as_object_id, mark_order_as_deleted
from ..auth import verify_admin
from ..notifications import notify_customer_order_status

router = APIRouter(tags=["bot"])

logger = logging.getLogger(__name__)


@router.get("/bot/webhook/status")
async def get_webhook_status():
    """
    Проверяет статус webhook в Telegram Bot API.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        return {
            "configured": False,
            "error": "TELEGRAM_BOT_TOKEN не настроен"
        }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"
            )
            result = response.json()
            if result.get("ok"):
                webhook_info = result.get("result", {})
                return {
                    "configured": True,
                    "url": webhook_info.get("url", ""),
                    "has_custom_certificate": webhook_info.get("has_custom_certificate", False),
                    "pending_update_count": webhook_info.get("pending_update_count", 0),
                    "last_error_date": webhook_info.get("last_error_date"),
                    "last_error_message": webhook_info.get("last_error_message"),
                    "max_connections": webhook_info.get("max_connections"),
                }
            else:
                return {
                    "configured": False,
                    "error": result.get("description", "Unknown error")
                }
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса webhook: {e}")
        return {
            "configured": False,
            "error": str(e)
        }


@router.post("/bot/webhook/setup")
async def setup_webhook(request: Request):
    """
    Настраивает webhook для Telegram Bot API.
    Может принимать опциональный параметр 'url' в теле запроса.
    Если 'url' не указан, используется PUBLIC_URL из настроек.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise HTTPException(
            status_code=400,
            detail="TELEGRAM_BOT_TOKEN не настроен"
        )
    
    # Пытаемся получить URL из тела запроса
    base_url = None
    try:
        body = await request.json()
        base_url = body.get("url") if isinstance(body, dict) else None
    except:
        pass
    
    # Если URL не передан в запросе, используем из настроек
    if not base_url:
        base_url = settings.public_url
    
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_URL не настроен. Укажите публичный URL сервера в .env или передайте 'url' в теле запроса"
        )
    
    try:
        webhook_url = f"{base_url.rstrip('/')}{settings.api_prefix}/bot/webhook"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["callback_query"]  # Только callback queries
                }
            )
            result = response.json()
            if result.get("ok"):
                logger.info(f"Webhook успешно настроен: {webhook_url}")
                return {
                    "success": True,
                    "url": webhook_url,
                    "message": "Webhook успешно настроен"
                }
            else:
                error_msg = result.get("description", "Unknown error")
                logger.error(f"Не удалось настроить webhook: {error_msg}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Не удалось настроить webhook: {error_msg}"
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка при настройке webhook: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Ошибка при настройке webhook: {str(e)}"
        )


@router.post("/bot/webhook")
async def handle_bot_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Обрабатывает webhook от Telegram Bot API (callback от inline-кнопок).
    """
    try:
        data = await request.json()
        
        # Логируем входящий запрос для отладки (ограничиваем размер лога)
        if isinstance(data, dict):
            data_keys = list(data.keys())
            logger.info(f"Webhook received, data keys: {data_keys}")
            if "callback_query" in data:
                callback_preview = {
                    "id": data["callback_query"].get("id"),
                    "data": data["callback_query"].get("data", "")[:50],  # Первые 50 символов
                    "from_id": data["callback_query"].get("from", {}).get("id"),
                }
                logger.info(f"Callback query preview: {callback_preview}")
        else:
            logger.warning(f"Webhook received non-dict data: {type(data)}")
            return {"ok": True}
        
        # Проверяем, что это callback query
        if "callback_query" not in data:
            logger.debug("No callback_query in data, returning ok")
            return {"ok": True}
        
        callback_query = data["callback_query"]
        callback_query_id = callback_query.get("id")
        callback_data = callback_query.get("data", "")
        user_id = callback_query.get("from", {}).get("id")
        message = callback_query.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat", {}).get("id")
        
        logger.info(f"Callback received: callback_query_id={callback_query_id}, user_id={user_id}, callback_data={callback_data}, chat_id={chat_id}, message_id={message_id}")
        
        if not callback_query_id:
            logger.error("No callback_query_id in callback_query")
            return {"ok": True}
        
        if not user_id:
            logger.warning("No user_id in callback_query")
            await _answer_callback_query(
                callback_query_id,
                "Ошибка: не удалось определить пользователя",
                show_alert=True
            )
            return {"ok": True}
        
        if not callback_data:
            logger.warning(f"No callback_data in callback_query for user_id={user_id}")
            await _answer_callback_query(
                callback_query_id,
                "Ошибка: данные кнопки не найдены",
                show_alert=True
            )
            return {"ok": True}
        
        # Проверяем, что пользователь - администратор
        settings = get_settings()
        admin_ids_set = set(settings.admin_ids) if settings.admin_ids else set()
        logger.info(f"Checking admin access: user_id={user_id}, admin_ids={admin_ids_set}")
        
        if user_id not in admin_ids_set:
            logger.warning(f"User {user_id} is not in admin_ids {admin_ids_set}")
            # Отвечаем на callback, но не обрабатываем
            await _answer_callback_query(
                callback_query_id,
                "У вас нет прав для выполнения этого действия",
                show_alert=True
            )
            return {"ok": True}
        
        logger.info(f"User {user_id} is admin, processing callback_data={callback_data}")
        
        # Обрабатываем callback для изменения статуса заказа (новый формат)
        if callback_data.startswith("status|"):
            # Формат: status|{order_id}|{status}
            parts = callback_data.split("|")
            logger.info(f"Parsing callback_data: parts={parts}, len={len(parts)}")
            
            if len(parts) != 3:
                logger.error(f"Invalid callback_data format: {callback_data}, parts={parts}")
                await _answer_callback_query(
                    callback_query_id,
                    "Некорректный формат команды",
                    show_alert=True
                )
                return {"ok": True}
            
            order_id = parts[1]
            new_status_value = parts[2]
            logger.info(f"Parsed: order_id={order_id}, new_status={new_status_value}")
            
            # Получаем заказ
            doc = await db.orders.find_one({"_id": as_object_id(order_id)})
            if not doc:
                await _answer_callback_query(
                    callback_query.get("id"),
                    "Заказ не найден",
                    show_alert=True
                )
                return {"ok": True}
            
            # Проверяем, что статус валидный
            valid_statuses = {
                OrderStatus.PROCESSING.value,
                OrderStatus.ACCEPTED.value,
                OrderStatus.SHIPPED.value,
                OrderStatus.DONE.value,
                OrderStatus.CANCELED.value,
            }
            
            if new_status_value not in valid_statuses:
                logger.error(f"Invalid status: {new_status_value}, valid_statuses={valid_statuses}")
                await _answer_callback_query(
                    callback_query_id,
                    f"Некорректный статус: {new_status_value}",
                    show_alert=True
                )
                return {"ok": True}
            
            current_status = doc.get("status")
            logger.info(f"Current status: {current_status}, new status: {new_status_value}")
            
            if current_status == new_status_value:
                logger.info(f"Status already set to {new_status_value}")
                await _answer_callback_query(
                    callback_query_id,
                    f"Заказ уже имеет статус: {new_status_value}",
                    show_alert=False
                )
                return {"ok": True}
            
            # Если заказ отменяется, возвращаем товары на склад
            from datetime import datetime
            from ..utils import restore_variant_quantity
            
            if new_status_value == OrderStatus.CANCELED.value and current_status != OrderStatus.CANCELED.value:
                items = doc.get("items", [])
                for item in items:
                    if item.get("variant_id"):
                        await restore_variant_quantity(
                            db,
                            item.get("product_id"),
                            item.get("variant_id"),
                            item.get("quantity", 0)
                        )
            
            # Определяем, можно ли редактировать адрес
            editable_statuses = {
                OrderStatus.PROCESSING.value,
            }
            can_edit_address = new_status_value in editable_statuses
            
            should_archive = new_status_value == OrderStatus.DONE.value
            old_status = current_status

            # Формируем операцию обновления
            update_operations: dict = {
                "$set": {
                    "status": new_status_value,
                    "updated_at": datetime.utcnow(),
                    "can_edit_address": can_edit_address,
                }
            }

            # Если заказ был завершён и мы изменяем статус на другой, убираем метку deleted_at полностью
            if old_status == OrderStatus.DONE.value and new_status_value != OrderStatus.DONE.value:
                update_operations["$unset"] = {"deleted_at": ""}
            # Если заказ завершается, сразу помечаем как удаленный (в одной атомарной операции)
            elif should_archive:
                update_operations["$set"]["deleted_at"] = datetime.utcnow()

            # Атомарно обновляем заказ - только один раз, без дополнительных операций
            try:
                updated = await db.orders.find_one_and_update(
                    {"_id": as_object_id(order_id)},
                    update_operations,
                    return_document=True,
                )
                logger.info(f"Update result: updated={updated is not None}")
            except Exception as e:
                logger.error(f"Error updating order: {e}")
                await _answer_callback_query(
                    callback_query_id,
                    f"Ошибка при обновлении заказа: {str(e)}",
                    show_alert=True
                )
                return {"ok": True}
            
            if updated:
                # Формируем сообщение подтверждения
                status_messages = {
                    OrderStatus.PROCESSING.value: "🔄 Статус изменён на 'В обработке'",
                    OrderStatus.ACCEPTED.value: "✅ Заказ принят!",
                    OrderStatus.SHIPPED.value: "🚚 Заказ выехал!",
                    OrderStatus.DONE.value: "🎉 Заказ завершён!",
                    OrderStatus.CANCELED.value: "❌ Заказ отменён!",
                }
                confirm_message = status_messages.get(new_status_value, f"Статус изменён на: {new_status_value}")
                
                # Отвечаем на callback
                answer_result = await _answer_callback_query(
                    callback_query_id,
                    confirm_message,
                    show_alert=False
                )
                logger.info(f"Answer callback query result: {answer_result}")
                
                # Обновляем сообщение, обновляя кнопки (показываем текущий статус)
                await _edit_message_reply_markup(
                    settings.telegram_bot_token,
                    chat_id,
                    message_id,
                    None  # Убираем кнопки после изменения статуса
                )
                
                # Отправляем уведомление клиенту об изменении статуса
                customer_user_id = updated.get("user_id")
                if customer_user_id and old_status != new_status_value:
                    try:
                        await notify_customer_order_status(
                            user_id=customer_user_id,
                            order_id=order_id,
                            order_status=new_status_value,
                            customer_name=updated.get("customer_name"),
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")
                
                logger.info(f"✅ Заказ {order_id} изменён на статус '{new_status_value}' администратором {user_id} через кнопку")
            else:
                logger.error(f"❌ Не удалось обновить заказ {order_id}")
                await _answer_callback_query(
                    callback_query_id,
                    "Ошибка при обновлении заказа",
                    show_alert=True
                )
        
        # Обрабатываем callback для принятия заказа (старый формат для совместимости)
        elif callback_data.startswith("accept_order_"):
            order_id = callback_data.replace("accept_order_", "")
            logger.info(f"Processing accept_order callback for order_id={order_id}")
            
            # Получаем заказ
            doc = await db.orders.find_one({"_id": as_object_id(order_id)})
            if not doc:
                await _answer_callback_query(
                    callback_query_id,
                    "Заказ не найден",
                    show_alert=True
                )
                return {"ok": True}
            
            # Обновляем статус на "принят"
            from datetime import datetime
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
            
            if updated:
                await _answer_callback_query(
                    callback_query_id,
                    "✅ Заказ принят!",
                    show_alert=False
                )
                await _edit_message_reply_markup(
                    settings.telegram_bot_token,
                    chat_id,
                    message_id,
                    None
                )
                customer_user_id = updated.get("user_id")
                if customer_user_id:
                    try:
                        await notify_customer_order_status(
                            user_id=customer_user_id,
                            order_id=order_id,
                            order_status=OrderStatus.ACCEPTED.value,
                            customer_name=updated.get("customer_name"),
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")
                logger.info(f"Заказ {order_id} принят администратором {user_id} через кнопку")
            else:
                await _answer_callback_query(
                    callback_query_id,
                    "Ошибка при обновлении заказа",
                    show_alert=True
                )
        
        # Обрабатываем callback для отмены заказа (старый формат для совместимости)
        elif callback_data.startswith("cancel_order_"):
            order_id = callback_data.replace("cancel_order_", "")
            logger.info(f"Processing cancel_order callback for order_id={order_id}")
            
            # Получаем заказ
            doc = await db.orders.find_one({"_id": as_object_id(order_id)})
            if not doc:
                await _answer_callback_query(
                    callback_query_id,
                    "Заказ не найден",
                    show_alert=True
                )
                return {"ok": True}
            
            # Проверяем, что заказ можно отменить
            current_status = doc.get("status")
            if current_status in {OrderStatus.SHIPPED.value, OrderStatus.DONE.value, OrderStatus.CANCELED.value}:
                await _answer_callback_query(
                    callback_query_id,
                    f"Заказ нельзя отменить. Текущий статус: {current_status}",
                    show_alert=True
                )
                return {"ok": True}
            
            # Обновляем статус на "отменён" и возвращаем товары на склад
            from datetime import datetime
            from ..utils import restore_variant_quantity
            
            items = doc.get("items", [])
            for item in items:
                if item.get("variant_id"):
                    await restore_variant_quantity(
                        db,
                        item.get("product_id"),
                        item.get("variant_id"),
                        item.get("quantity", 0)
                    )
            
            updated = await db.orders.find_one_and_update(
                {"_id": as_object_id(order_id)},
                {
                    "$set": {
                        "status": OrderStatus.CANCELED.value,
                        "updated_at": datetime.utcnow(),
                        "can_edit_address": False,
                    }
                },
                return_document=True,
            )
            
            if updated:
                # Отвечаем на callback
                await _answer_callback_query(
                    callback_query_id,
                    "❌ Заказ отменён!",
                    show_alert=False
                )
                
                # Обновляем сообщение, убирая кнопки
                await _edit_message_reply_markup(
                    settings.telegram_bot_token,
                    chat_id,
                    message_id,
                    None  # Убираем кнопки
                )
                
                # Отправляем уведомление клиенту об изменении статуса
                customer_user_id = updated.get("user_id")
                if customer_user_id:
                    try:
                        await notify_customer_order_status(
                            user_id=customer_user_id,
                            order_id=order_id,
                            order_status=OrderStatus.CANCELED.value,
                            customer_name=updated.get("customer_name"),
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")
                
                logger.info(f"Заказ {order_id} отменён администратором {user_id} через кнопку")
            else:
                await _answer_callback_query(
                    callback_query_id,
                    "Ошибка при обновлении заказа",
                    show_alert=True
                )
        else:
            logger.warning(f"Unhandled callback_data: {callback_data}")
            await _answer_callback_query(
                callback_query_id,
                "Неизвестная команда",
                show_alert=True
            )
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка при обработке webhook: {e}")
        return {"ok": True}


async def _answer_callback_query(
    callback_query_id: str,
    text: str,
    show_alert: bool = False
) -> bool:
    """Отвечает на callback query от Telegram."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set, cannot answer callback query")
        return False
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": show_alert,
                }
            )
            result = response.json()
            if result.get("ok"):
                logger.info(f"Successfully answered callback query {callback_query_id}: {text}")
                return True
            else:
                logger.error(f"Failed to answer callback query: {result.get('description', 'Unknown error')}")
                return False
    except Exception as e:
        logger.error(f"Ошибка при ответе на callback query {callback_query_id}: {e}")
        return False


async def _edit_message_reply_markup(
    bot_token: str,
    chat_id: int,
    message_id: int,
    reply_markup: dict | None
):
    """Обновляет reply_markup сообщения."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            data = {
                "chat_id": chat_id,
                "message_id": message_id,
            }
            if reply_markup is None:
                data["reply_markup"] = "{}"
            else:
                import json
                data["reply_markup"] = json.dumps(reply_markup)
            
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup",
                json=data
            )
    except Exception as e:
        logger.error(f"Ошибка при обновлении сообщения: {e}")

