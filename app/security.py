"""Модуль для аутентификации и безопасности."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, status


@dataclass(frozen=True, slots=True)
class TelegramUser:
    """Модель пользователя Telegram."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool | None = None


async def get_current_user(
    dev_user_id: str | None = Header(None, convert_underscores=False, alias="X-Dev-User-Id"),
) -> TelegramUser:
    """Получение user_id из заголовка."""
    if not dev_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не удалось получить ID пользователя от Telegram. Убедитесь, что приложение запущено через Telegram.",
        )

    try:
        return TelegramUser(id=int(str(dev_user_id).strip()))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Некорректный формат user_id: {dev_user_id}",
        )
