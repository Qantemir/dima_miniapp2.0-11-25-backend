"""
Rate Limiting Middleware для защиты API от злоупотреблений
"""

import time
from collections import defaultdict
from typing import Optional
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
import logging

logger = logging.getLogger(__name__)

# In-memory хранилище для rate limiting (в продакшене лучше использовать Redis)
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_cleanup_interval = 300  # Очистка каждые 5 минут
_last_cleanup = time.time()


def _cleanup_old_entries():
    """Очищает старые записи из rate limit store"""
    global _last_cleanup, _rate_limit_store
    now = time.time()
    if now - _last_cleanup < _cleanup_interval:
        return
    
    _last_cleanup = now
    cutoff = now - 60  # Удаляем записи старше 1 минуты
    
    keys_to_delete = []
    for key, timestamps in _rate_limit_store.items():
        _rate_limit_store[key] = [ts for ts in timestamps if ts > cutoff]
        if not _rate_limit_store[key]:
            keys_to_delete.append(key)
    
    for key in keys_to_delete:
        del _rate_limit_store[key]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Rate limiting middleware с настраиваемыми лимитами для разных endpoints
    """
    
    def __init__(self, app, default_limit: int = 100, window: int = 60):
        super().__init__(app)
        self.default_limit = default_limit  # Запросов в минуту
        self.window = window  # Окно в секундах
        
        # Специфичные лимиты для разных endpoints
        self.endpoint_limits = {
            "/api/cart": 30,  # Корзина - меньше лимит
            "/api/order": 10,  # Создание заказа - очень строгий лимит
            "/api/admin": 200,  # Админка - больше лимит
        }
    
    async def dispatch(self, request: Request, call_next):
        # Пропускаем health check и статические файлы
        path = request.url.path
        if path in ["/health", "/"] or path.startswith("/assets/") or path.startswith("/uploads/"):
            return await call_next(request)
        
        # Определяем лимит для endpoint
        limit = self.default_limit
        for endpoint, endpoint_limit in self.endpoint_limits.items():
            if path.startswith(endpoint):
                limit = endpoint_limit
                break
        
        # Получаем идентификатор клиента (IP или user_id из заголовков)
        client_id = self._get_client_id(request)
        
        # Очищаем старые записи периодически
        _cleanup_old_entries()
        
        # Проверяем rate limit
        now = time.time()
        window_start = now - self.window
        
        # Фильтруем запросы в текущем окне
        client_requests = _rate_limit_store[client_id]
        client_requests[:] = [ts for ts in client_requests if ts > window_start]
        
        # Проверяем лимит
        if len(client_requests) >= limit:
            logger.warning(f"Rate limit exceeded for {client_id} on {path}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Слишком много запросов. Попробуйте через {self.window} секунд."
            )
        
        # Добавляем текущий запрос
        client_requests.append(now)
        
        return await call_next(request)
    
    def _get_client_id(self, request: Request) -> str:
        """Получает идентификатор клиента для rate limiting"""
        # Пытаемся получить user_id из заголовков (если есть)
        user_id = request.headers.get("X-Telegram-User-Id")
        if user_id:
            return f"user:{user_id}"
        
        # Иначе используем IP
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        else:
            ip = request.client.host if request.client else "unknown"
        
        return f"ip:{ip}"

