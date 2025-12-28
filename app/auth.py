"""Модуль для проверки прав администратора."""

from fastapi import Depends, HTTPException, status

from .config import get_settings
from .security import TelegramUser, get_current_user


async def verify_admin(current_user: TelegramUser = Depends(get_current_user)) -> int:
    """Оптимизированная проверка прав администратора."""
    import logging
    
    settings = get_settings()
    user_id = int(current_user.id)
    logger = logging.getLogger(__name__)

    if not settings.admin_ids:
        logger.error(
            f"❌ ADMIN_IDS не настроен! Пользователь {user_id} не может получить доступ к админ-панели. "
            "Установите переменную окружения ADMIN_IDS=123456789,987654321"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_IDS не настроен. Обратитесь к администратору.",
        )

    # Оптимизированная проверка (используем кэшированный set)
    if user_id in settings.admin_ids_set:
        return current_user.id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Доступ запрещён. Требуются права администратора.",
    )

