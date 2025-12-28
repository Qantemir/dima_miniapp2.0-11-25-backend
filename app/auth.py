"""Модуль для проверки прав администратора и доступа к бэкапу."""

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


async def verify_backup_user(current_user: TelegramUser = Depends(get_current_user)) -> int:
    """
    Проверка прав доступа к функциям бэкапа.
    
    Доступ имеют:
    - Пользователи из BACKUP_USER_IDS
    - Администраторы (из ADMIN_IDS) - для удобства админы тоже могут делать бэкап
    
    Args:
        current_user: Текущий пользователь из Telegram
        
    Returns:
        ID пользователя, если доступ разрешен
        
    Raises:
        HTTPException: Если доступ запрещен
    """
    import logging
    
    settings = get_settings()
    user_id = int(current_user.id)
    logger = logging.getLogger(__name__)

    # Проверяем, является ли пользователь админом (админы тоже могут делать бэкап)
    if settings.admin_ids and user_id in settings.admin_ids_set:
        return current_user.id
    
    # Проверяем, является ли пользователь backup-пользователем
    if settings.backup_user_ids and user_id in settings.backup_user_ids_set:
        return current_user.id
    
    # Если ни backup_user_ids, ни admin_ids не настроены, но пользователь пытается получить доступ
    if not settings.backup_user_ids and not settings.admin_ids:
        logger.error(
            f"❌ BACKUP_USER_IDS и ADMIN_IDS не настроены! "
            f"Пользователь {user_id} не может получить доступ к бэкапу. "
            "Установите переменную окружения BACKUP_USER_IDS=123456789 или ADMIN_IDS=123456789"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="BACKUP_USER_IDS или ADMIN_IDS не настроены. Обратитесь к администратору.",
        )
    
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Доступ запрещён. Требуются права доступа к бэкапу или права администратора.",
    )
