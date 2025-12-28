"""Webhook для обработки callback от Telegram Bot API (кнопки в сообщениях)."""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..auth import verify_admin
from ..config import get_settings
from ..database import get_db
from ..notifications import notify_customer_order_status
from ..schemas import OrderStatus
from ..utils import as_object_id, mark_order_as_deleted

router = APIRouter(tags=["bot"])

logger = logging.getLogger(__name__)


@router.get("/bot/webhook/status")
async def get_webhook_status():
    """Проверяет статус webhook в Telegram Bot API."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return {"configured": False, "error": "TELEGRAM_BOT_TOKEN не настроен"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo")
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
                return {"configured": False, "error": result.get("description", "Unknown error")}
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса webhook: {e}")
        return {"configured": False, "error": str(e)}


@router.post("/bot/webhook/setup")
async def setup_webhook(request: Request):
    """
    Настраивает webhook для Telegram Bot API.

    Может принимать опциональный параметр 'url' в теле запроса.
    Если 'url' не указан, используется PUBLIC_URL из настроек.
    """
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN не настроен")

    # Пытаемся получить URL из тела запроса
    base_url = None
    try:
        body = await request.json()
        base_url = body.get("url") if isinstance(body, dict) else None
    except Exception:
        pass

    # Если URL не передан в запросе, используем из настроек
    if not base_url:
        base_url = settings.public_url

    if not base_url:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_URL не настроен. Укажите публичный URL сервера в .env или передайте 'url' в теле запроса",
        )

    try:
        webhook_url = f"{base_url.rstrip('/')}{settings.api_prefix}/bot/webhook"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
                json={"url": webhook_url, "allowed_updates": ["callback_query"]},  # Только callback queries
            )
            result = response.json()
            if result.get("ok"):
                return {"success": True, "url": webhook_url, "message": "Webhook успешно настроен"}
            else:
                error_msg = result.get("description", "Unknown error")
                logger.error(f"Не удалось настроить webhook: {error_msg}")
                raise HTTPException(status_code=400, detail=f"Не удалось настроить webhook: {error_msg}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка при настройке webhook: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при настройке webhook: {str(e)}")


@router.post("/bot/webhook")
async def handle_bot_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Обрабатывает webhook от Telegram Bot API (callback от inline-кнопок)."""
    try:
        data = await request.json()

        # Проверяем, что это callback query
        if not isinstance(data, dict) or "callback_query" not in data:
            return {"ok": True}

        callback_query = data["callback_query"]
        callback_query_id = callback_query.get("id")
        callback_data = callback_query.get("data", "")
        user_id = callback_query.get("from", {}).get("id")
        message = callback_query.get("message", {})
        message_id = message.get("message_id")
        chat_id = message.get("chat", {}).get("id")

        if not callback_query_id:
            logger.error("No callback_query_id in callback_query")
            return {"ok": True}

        if not user_id:
            await _answer_callback_query(
                callback_query_id, "Ошибка: не удалось определить пользователя", show_alert=True
            )
            return {"ok": True}

        if not callback_data:
            await _answer_callback_query(callback_query_id, "Ошибка: данные кнопки не найдены", show_alert=True)
            return {"ok": True}

        # Проверяем, что пользователь - администратор
        settings = get_settings()
        admin_ids_set = set(settings.admin_ids) if settings.admin_ids else set()

        if user_id not in admin_ids_set:
            # Отвечаем на callback, но не обрабатываем
            await _answer_callback_query(
                callback_query_id, "У вас нет прав для выполнения этого действия", show_alert=True
            )
            return {"ok": True}

        # Обрабатываем callback для изменения статуса заказа (новый формат)
        if callback_data.startswith("status|"):
            # Формат: status|{order_id}|{status}
            parts = callback_data.split("|")

            if len(parts) != 3:
                logger.error(f"Invalid callback_data format: {callback_data}, parts={parts}")
                await _answer_callback_query(callback_query_id, "Некорректный формат команды", show_alert=True)
                return {"ok": True}

            order_id = parts[1]
            new_status_value = parts[2]
            
            logger.info(f"Processing status change: order_id={order_id}, new_status={new_status_value}, user_id={user_id}")

            # Получаем заказ
            doc = await db.orders.find_one({"_id": as_object_id(order_id)})
            if not doc:
                await _answer_callback_query(callback_query_id, "Заказ не найден", show_alert=True)
                return {"ok": True}

            # Проверяем, что статус валидный
            valid_statuses = {
                OrderStatus.ACCEPTED.value,
                OrderStatus.REJECTED.value,
            }

            if new_status_value not in valid_statuses:
                logger.error(f"Invalid status: {new_status_value}, valid_statuses={valid_statuses}")
                await _answer_callback_query(
                    callback_query_id, f"Некорректный статус: {new_status_value}", show_alert=True
                )
                return {"ok": True}

            current_status = doc.get("status")

            if current_status == new_status_value:
                await _answer_callback_query(
                    callback_query_id, f"Заказ уже имеет статус: {new_status_value}", show_alert=False
                )
                return {"ok": True}

            # Если заказ отклоняется, возвращаем товары на склад
            from datetime import datetime

            from ..utils import restore_variant_quantity

            if new_status_value == OrderStatus.REJECTED.value and current_status != OrderStatus.REJECTED.value:
                items = doc.get("items", [])
                for item in items:
                    if item.get("variant_id"):
                        await restore_variant_quantity(
                            db, item.get("product_id"), item.get("variant_id"), item.get("quantity", 0)
                        )

            old_status = current_status

            # Формируем операцию обновления
            update_operations: dict = {
                "$set": {
                    "status": new_status_value,
                    "updated_at": datetime.utcnow(),
                    "can_edit_address": False,  # Адрес нельзя редактировать после создания
                }
            }

            # Если статус "отказано", нужно запросить причину (но через кнопки это не сделать, поэтому просто обновляем)
            # Для отказа через кнопки причина будет пустой, админ может указать её позже через админку
            if new_status_value == OrderStatus.REJECTED.value:
                # Если причина не указана, оставляем пустой (админ может указать позже)
                if not doc.get("rejection_reason"):
                    update_operations["$set"]["rejection_reason"] = "Отклонено через кнопку в Telegram"
            else:
                # Если статус меняется с "отказано" на другой, убираем причину отказа
                update_operations["$unset"] = {"rejection_reason": ""}

            # Атомарно обновляем заказ - только один раз, без дополнительных операций
            try:
                updated = await db.orders.find_one_and_update(
                    {"_id": as_object_id(order_id)},
                    update_operations,
                    return_document=True,
                )
            except Exception as e:
                logger.error(f"Error updating order: {e}")
                await _answer_callback_query(
                    callback_query_id, f"Ошибка при обновлении заказа: {str(e)}", show_alert=True
                )
                return {"ok": True}

            if updated:
                # Формируем сообщение подтверждения
                status_messages = {
                    OrderStatus.ACCEPTED.value: "✅ Заказ принят!",
                    OrderStatus.REJECTED.value: "❌ Заказ отклонён!",
                }
                confirm_message = status_messages.get(new_status_value, f"Статус изменён на: {new_status_value}")

                # Отвечаем на callback
                await _answer_callback_query(callback_query_id, confirm_message, show_alert=False)

                # Обновляем сообщение, обновляя кнопки (показываем текущий статус)
                await _edit_message_reply_markup(
                    settings.telegram_bot_token, chat_id, message_id, None  # Убираем кнопки после изменения статуса
                )

                # Отправляем уведомление клиенту об изменении статуса
                customer_user_id = updated.get("user_id")
                if customer_user_id and old_status != new_status_value:
                    try:
                        rejection_reason = updated.get("rejection_reason") if new_status_value == OrderStatus.REJECTED.value else None
                        logger.info(f"Sending notification to customer: user_id={customer_user_id}, order_id={order_id}, status={new_status_value}")
                        await notify_customer_order_status(
                            user_id=customer_user_id,
                            order_id=order_id,
                            order_status=new_status_value,
                            customer_name=updated.get("customer_name"),
                            rejection_reason=rejection_reason,
                        )
                        logger.info(f"Notification sent successfully to customer {customer_user_id}")
                    except Exception as e:
                        logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")
            else:
                logger.error(f"❌ Не удалось обновить заказ {order_id}")
                await _answer_callback_query(callback_query_id, "Ошибка при обновлении заказа", show_alert=True)

        # Обрабатываем callback для принятия заказа (старый формат для совместимости)
        elif callback_data.startswith("accept_order_"):
            order_id = callback_data.replace("accept_order_", "")

            # Получаем заказ
            doc = await db.orders.find_one({"_id": as_object_id(order_id)})
            if not doc:
                await _answer_callback_query(callback_query_id, "Заказ не найден", show_alert=True)
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
                await _answer_callback_query(callback_query_id, "✅ Заказ принят!", show_alert=False)
                await _edit_message_reply_markup(settings.telegram_bot_token, chat_id, message_id, None)
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
            else:
                await _answer_callback_query(callback_query_id, "Ошибка при обновлении заказа", show_alert=True)

        # Обрабатываем callback для отмены заказа (старый формат для совместимости)
        elif callback_data.startswith("cancel_order_"):
            order_id = callback_data.replace("cancel_order_", "")

            # Получаем заказ
            doc = await db.orders.find_one({"_id": as_object_id(order_id)})
            if not doc:
                await _answer_callback_query(callback_query_id, "Заказ не найден", show_alert=True)
                return {"ok": True}

            # Обновляем статус на "отказано" и возвращаем товары на склад
            from datetime import datetime

            from ..utils import restore_variant_quantity

            items = doc.get("items", [])
            for item in items:
                if item.get("variant_id"):
                    await restore_variant_quantity(
                        db, item.get("product_id"), item.get("variant_id"), item.get("quantity", 0)
                    )

            updated = await db.orders.find_one_and_update(
                {"_id": as_object_id(order_id)},
                {
                    "$set": {
                        "status": OrderStatus.REJECTED.value,
                        "updated_at": datetime.utcnow(),
                        "can_edit_address": False,
                        "rejection_reason": "Отклонено через кнопку в Telegram",
                    }
                },
                return_document=True,
            )

            if updated:
                # Отвечаем на callback
                await _answer_callback_query(callback_query_id, "❌ Заказ отклонён!", show_alert=False)

                # Обновляем сообщение, убирая кнопки
                await _edit_message_reply_markup(
                    settings.telegram_bot_token, chat_id, message_id, None  # Убираем кнопки
                )

                # Отправляем уведомление клиенту об изменении статуса
                customer_user_id = updated.get("user_id")
                if customer_user_id:
                    try:
                        await notify_customer_order_status(
                            user_id=customer_user_id,
                            order_id=order_id,
                            order_status=OrderStatus.REJECTED.value,
                            customer_name=updated.get("customer_name"),
                            rejection_reason=updated.get("rejection_reason"),
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке уведомления клиенту о статусе заказа {order_id}: {e}")
            else:
                await _answer_callback_query(callback_query_id, "Ошибка при обновлении заказа", show_alert=True)
        else:
            await _answer_callback_query(callback_query_id, "Неизвестная команда", show_alert=True)

        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка при обработке webhook: {e}")
        return {"ok": True}


async def _answer_callback_query(callback_query_id: str, text: str, show_alert: bool = False) -> bool:
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
                },
            )
            result = response.json()
            if result.get("ok"):
                return True
            else:
                logger.error(f"Failed to answer callback query: {result.get('description', 'Unknown error')}")
                return False
    except Exception as e:
        logger.error(f"Ошибка при ответе на callback query {callback_query_id}: {e}")
        return False


async def _edit_message_reply_markup(bot_token: str, chat_id: int, message_id: int, reply_markup: dict | None):
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

            await client.post(f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup", json=data)
    except Exception as e:
        logger.error(f"Ошибка при обновлении сообщения: {e}")
