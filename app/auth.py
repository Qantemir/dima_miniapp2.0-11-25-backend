from fastapi import Depends, HTTPException, status

from .config import get_settings
from .security import TelegramUser, get_current_user


async def verify_admin(current_user: TelegramUser = Depends(get_current_user)) -> int:
  """Оптимизированная проверка прав администратора"""
  settings = get_settings()
  user_id = int(current_user.id)
  
  if not settings.admin_ids:
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

