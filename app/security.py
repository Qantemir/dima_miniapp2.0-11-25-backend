from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, status

from .config import settings

_cached_secret_key: bytes | None = None


@dataclass(frozen=True, slots=True)
class TelegramUser:
  id: int
  first_name: str | None = None
  last_name: str | None = None
  username: str | None = None
  language_code: str | None = None
  is_premium: bool | None = None


def _get_secret_key() -> bytes:
  """
  Telegram recommends using SHA256("WebAppData" + bot_token) as secret key.
  We cache it to avoid re-computing on every request.
  """
  global _cached_secret_key
  if _cached_secret_key is not None:
    return _cached_secret_key
  if not settings.telegram_bot_token:
    raise HTTPException(
      status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
      detail="TELEGRAM_BOT_TOKEN не настроен на сервере",
    )
  raw = "WebAppData" + settings.telegram_bot_token
  _cached_secret_key = sha256(raw.encode()).digest()
  return _cached_secret_key


def _build_check_string(data: Mapping[str, str]) -> str:
  parts = [f"{key}={value}" for key, value in sorted(data.items())]
  return "\n".join(parts)


def _parse_init_data(init_data: str) -> dict[str, str]:
  """
  Telegram init data is passed as query string, but values can be URL-encoded.
  parse_qsl already handles decoding.
  """
  parsed = parse_qsl(init_data, keep_blank_values=True)
  return {key: value for key, value in parsed}


def _validate_hash(parsed_data: dict[str, str]) -> dict[str, Any]:
  data = dict(parsed_data)
  received_hash = data.pop("hash", None)
  if not received_hash:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Отсутствует hash параметр Telegram initData",
    )

  check_string = _build_check_string(data)
  secret_key = _get_secret_key()
  calculated_hash = hmac.new(secret_key, check_string.encode(), sha256).hexdigest()

  if not hmac.compare_digest(received_hash, calculated_hash):
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Недействительная подпись Telegram",
    )

  if settings.telegram_data_ttl_seconds > 0:
    auth_date_raw = data.get("auth_date")
    if not auth_date_raw:
      raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Отсутствует auth_date в Telegram initData",
      )
    try:
      auth_date = int(auth_date_raw)
    except ValueError:
      raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Некорректное значение auth_date",
      ) from None
    if time.time() - auth_date > settings.telegram_data_ttl_seconds:
      raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Данные Telegram устарели. Перезапустите Mini App из Telegram.",
      )

  user_raw = data.get("user")
  if not user_raw:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Отсутствуют данные пользователя Telegram",
    )

  try:
    user_payload = json.loads(user_raw)
  except json.JSONDecodeError:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Некорректные данные пользователя Telegram",
    ) from None

  return user_payload


def _build_dev_user(dev_user_id: int | None) -> TelegramUser:
  fallback_dev_id = dev_user_id or settings.default_dev_user_id
  if fallback_dev_id is None:
    raise HTTPException(
      status_code=status.HTTP_401_UNAUTHORIZED,
      detail="Dev user недоступен: отсутствует идентификатор",
    )
  if settings.dev_allowed_user_ids and fallback_dev_id not in settings.dev_allowed_user_ids:
    raise HTTPException(
      status_code=status.HTTP_403_FORBIDDEN,
      detail="Dev user не разрешён для текущей конфигурации",
    )
  return TelegramUser(id=fallback_dev_id)


async def get_current_user(
  telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
  dev_user_id: str | None = Header(None, convert_underscores=False, alias="X-Dev-User-Id"),
) -> TelegramUser:
  """Оптимизированное получение user_id из заголовка"""
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

