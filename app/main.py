"""Main FastAPI application module."""

import asyncio
import gzip
import io
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError
from starlette.concurrency import iterate_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .cache import close_redis, get_redis
from .config import settings, ENV_PATH
from .database import close_mongo_connection, connect_to_mongo, get_db
from .routers import admin, bot_webhook, cart, catalog, orders, store
from .routers.cart import cleanup_expired_carts_periodic
from .schemas import CatalogResponse, StoreStatus
from .utils import permanently_delete_order_entry

app = FastAPI(title="Mini Shop Telegram Backend", version="1.0.0")


# Обработчик ошибок валидации запросов
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Обрабатывает ошибки валидации запросов."""
    logger = logging.getLogger(__name__)
    logger.error(f"Ошибка валидации запроса {request.method} {request.url.path}: {exc.errors()}")
    logger.error(f"Тело запроса: {await request.body()}")
    response = JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "body": str(exc.body) if hasattr(exc, 'body') else None}
    )
    # Добавляем CORS заголовки для ошибок валидации
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


# Глобальный обработчик исключений для 503 ошибок (БД недоступна)
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Обрабатывает HTTPException и возвращает fallback значения для критичных эндпоинтов."""
    logger = logging.getLogger(__name__)
    
    # Логируем все 400 ошибки для диагностики
    if exc.status_code == status.HTTP_400_BAD_REQUEST:
        logger.error(f"400 ошибка на {request.method} {request.url.path}: {exc.detail}")
        try:
            body = await request.body()
            logger.error(f"Тело запроса: {body.decode('utf-8', errors='replace')}")
        except Exception:
            pass

    if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        path = request.url.path

        # Для /api/store/status возвращаем fallback статус
        if path == "/api/store/status":
            return JSONResponse(
                status_code=200,
                content={
                    "is_sleep_mode": False,
                    "sleep_message": None,
                },
            )

        # Для /api/catalog возвращаем пустой каталог
        if path == "/api/catalog":
            return JSONResponse(
                status_code=200,
                content={
                    "categories": [],
                    "products": [],
                },
            )

        # Для /api/store/status/stream возвращаем простой стрим с fallback данными
        if path == "/api/store/status/stream":
            async def fallback_stream():
                fallback_data = {
                    "is_sleep_mode": False,
                    "sleep_message": None,
                }
                yield f"event: status\ndata: {json.dumps(fallback_data, ensure_ascii=False)}\n\n"
                # Отправляем одно сообщение и закрываем стрим
                await asyncio.sleep(0.1)

            response = StreamingResponse(fallback_stream(), media_type="text/event-stream")
            response.headers["Content-Encoding"] = "identity"
            return response

    # Для остальных ошибок возвращаем стандартный ответ с CORS заголовками
    response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


# Глобальный обработчик ВСЕХ исключений для критичных эндпоинтов
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Обрабатывает все исключения и возвращает fallback значения для критичных эндпоинтов."""
    logger = logging.getLogger(__name__)
    path = request.url.path

    # Логируем ошибку
    logger.error(f"Необработанное исключение для {path}: {type(exc).__name__}: {exc}", exc_info=True)

    # Для критичных эндпоинтов возвращаем fallback вместо 500
    if path == "/api/store/status":
        return JSONResponse(
            status_code=200,
            content={
                "is_sleep_mode": False,
                "sleep_message": None,
            },
        )

    if path == "/api/catalog":
        return JSONResponse(
            status_code=200,
            content={
                "categories": [],
                "products": [],
            },
        )

    if path == "/api/store/status/stream":
        async def fallback_stream():
            fallback_data = {
                "is_sleep_mode": False,
                "sleep_message": None,
            }
            yield f"event: status\ndata: {json.dumps(fallback_data, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.1)

        response = StreamingResponse(fallback_stream(), media_type="text/event-stream")
        response.headers["Content-Encoding"] = "identity"
        return response

    # Для остальных эндпоинтов возвращаем стандартную ошибку 500 с CORS заголовками
    response = JSONResponse(status_code=500, content={"detail": f"Internal server error: {str(exc)}"})
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


class SafeGZipMiddleware(BaseHTTPMiddleware):
    """
    Собственная реализация GZip, которая не ломается на закрытых стримах.

    Пропускает SSE/streaming/HEAD/304 ответы.
    """

    def __init__(self, app, minimum_size: int = 1000):
        """Initialize the middleware with minimum size threshold."""
        super().__init__(app)
        self.minimum_size = minimum_size

    async def dispatch(self, request: Request, call_next):
        """Process request and compress response if needed."""
        response = await call_next(request)

        if request.method == "HEAD":
            return response
        if response.status_code in (204, 304):
            return response
        if isinstance(response, StreamingResponse) or (getattr(response, "media_type", None) == "text/event-stream"):
            return response

        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response
        if "content-encoding" in response.headers:
            return response

        if getattr(response, "body_iterator", None) is not None:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            body = b"".join(chunks)
        else:
            body = getattr(response, "body", b"") or b""

        if len(body) < self.minimum_size:
            response.body_iterator = iterate_in_threadpool(iter([body]))
            return response

        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb") as gzip_file:
            gzip_file.write(body)
        compressed_body = buffer.getvalue()

        new_response = Response(
            content=compressed_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
        new_response.headers["Content-Encoding"] = "gzip"
        new_response.headers["Content-Length"] = str(len(compressed_body))
        vary = new_response.headers.get("Vary")
        if vary:
            if "accept-encoding" not in vary.lower():
                new_response.headers["Vary"] = f"{vary}, Accept-Encoding"
        else:
            new_response.headers["Vary"] = "Accept-Encoding"

        return new_response


# Добавляем безопасный GZip middleware (минимальный threshold для максимальной компрессии)
app.add_middleware(SafeGZipMiddleware, minimum_size=200)

# Добавляем Rate Limiting
from .middleware.rate_limit import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware, default_limit=100, window=60)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # Убрано, так как несовместимо с allow_origins=["*"] и cookies не используются
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

# Монтируем статические файлы фронтенда (dist папка)
# Определяем путь к dist папке, учитывая разные запуски (uvicorn/Procfile/Dockerfile)
logger = logging.getLogger(__name__)


def _find_next_dir() -> Path:
    """Ищет папку .next для Next.js standalone output."""
    candidates = []
    here = Path(__file__).resolve()
    # 1) .../backend/app/main.py → project root = ../../
    candidates.append(here.parent.parent.parent / ".next")
    # 2) .../app/main.py (если пакет «app» лежит в рабочей директории) → project root = ../
    candidates.append(here.parent.parent / ".next")
    # 3) Текущая рабочая директория (WORKDIR в Docker) → ./.next
    candidates.append(Path.cwd() / ".next")
    # 4) Старый формат dist (для обратной совместимости)
    candidates.append(here.parent.parent.parent / "dist")
    candidates.append(here.parent.parent / "dist")
    candidates.append(Path.cwd() / "dist")

    for next_path in candidates:
        if next_path.exists():
            return next_path

    # Фолбэк — нет .next, вернём путь по умолчанию
    return Path("/.next")  # заведомо несуществующий


next_dir = _find_next_dir()

# Монтируем статические файлы Next.js
static_dir = next_dir / "static"
if static_dir.exists():
    app.mount("/_next/static", StaticFiles(directory=str(static_dir)), name="next-static")

# Монтируем Next.js standalone server файлы для SSR
standalone_dir = next_dir / "standalone"
if standalone_dir.exists():
    server_dir = standalone_dir / "server"
    if server_dir.exists():
        # Монтируем server chunks если есть
        chunks_dir = server_dir / "chunks"
        if chunks_dir.exists():
            app.mount("/_next/chunks", StaticFiles(directory=str(chunks_dir)), name="next-chunks")

# Монтируем public файлы
here = Path(__file__).resolve()
public_dir = (
    here.parent.parent.parent / "public" if (here.parent.parent.parent / "public").exists() else Path.cwd() / "public"
)
if public_dir.exists():
    # Favicon убран, так как приложение используется только в Telegram WebView
    pass


@app.middleware("http")
async def apply_security_and_cache_headers(request, call_next):
    """Apply security and cache headers to responses."""
    response = await call_next(request)

    # Убеждаемся, что CORS заголовки присутствуют для всех ответов
    # Это важно для ошибок, которые могут не проходить через CORSMiddleware
    if "Access-Control-Allow-Origin" not in response.headers:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"

    # Cache-Control headers для оптимизации
    path = request.url.path
    
    # Явно добавляем CORS заголовки для изображений продуктов
    if "/product/image/" in path:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
    
    if path.startswith("/api/catalog"):
        # Каталог кэшируется на 10 минут для максимальной производительности
        response.headers["Cache-Control"] = "public, max-age=600, stale-while-revalidate=120"
        response.headers["Vary"] = "Accept-Encoding"
    elif path.startswith("/api/store/status"):
        # Статус магазина кэшируется на 1 минуту
        response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=20"
    elif path.startswith("/assets/") or path.endswith((".js", ".css", ".png", ".jpg", ".svg", ".woff2")):
        # Статические файлы кэшируются на 1 год
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    # Убрали Permissions-Policy заголовок, чтобы избежать ошибок с browsing-topics
    # response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


async def cleanup_deleted_orders():
    """
    Фоновая задача для окончательного удаления заказов.

    Удаляет заказы, которые были помечены как удаленные более 10 минут назад.
    """
    logger = logging.getLogger(__name__)

    while True:
        try:
            # Получаем базу данных
            db = await get_db()

            # Находим заказы, удаленные более 10 минут назад
            cutoff_time = datetime.utcnow() - timedelta(minutes=10)
            deleted_orders = await db.orders.find({"deleted_at": {"$exists": True, "$lte": cutoff_time}}).to_list(
                length=100
            )

            for order_doc in deleted_orders:
                try:
                    await permanently_delete_order_entry(db, order_doc)
                except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
                    # Временные проблемы с подключением - игнорируем
                    pass
                except Exception as e:
                    logger.error(f"Ошибка при окончательном удалении заказа {order_doc.get('_id')}: {e}")

            # Ждем перед следующей проверкой (5 минут для экономии ресурсов)
            await asyncio.sleep(300)
        except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError):
            # Временные проблемы с подключением - игнорируем
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче очистки заказов: {e}")
            await asyncio.sleep(300)


@app.on_event("startup")
async def startup():
    """Initialize application on startup."""
    # Настраиваем логирование для максимальной производительности
    logging.getLogger("pymongo").setLevel(logging.ERROR)  # Только ошибки
    logging.getLogger("motor").setLevel(logging.ERROR)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # Убираем access logs

    # Логируем информацию о конфигурации при старте
    logger = logging.getLogger(__name__)

    # Подключаемся к MongoDB при старте для быстрого первого запроса
    await connect_to_mongo()

    # Подключаемся к Redis при старте
    await get_redis()

    # Запускаем фоновую задачу для очистки удаленных заказов
    asyncio.create_task(cleanup_deleted_orders())

    # Запускаем фоновую задачу для очистки просроченных корзин
    asyncio.create_task(cleanup_expired_carts_periodic())

    # Настраиваем webhook для Telegram Bot API (если указан публичный URL)
    logger = logging.getLogger(__name__)

    if settings.telegram_bot_token and settings.public_url:
        try:
            import httpx

            webhook_url = f"{settings.public_url.rstrip('/')}{settings.api_prefix}/bot/webhook"

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Сначала удаляем старый webhook (если есть)
                try:
                    await client.post(
                        f"https://api.telegram.org/bot{settings.telegram_bot_token}/deleteWebhook",
                        json={"drop_pending_updates": False},
                    )
                except Exception:
                    pass

                # Устанавливаем новый webhook
                response = await client.post(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
                    json={"url": webhook_url, "allowed_updates": ["callback_query"]},  # Только callback queries
                )
                result = response.json()
                if not result.get("ok"):
                    error_desc = result.get("description", "Unknown error")
                    logger.error(f"❌ Не удалось настроить webhook: {error_desc}")
                    logger.error(f"Проверьте, что URL {webhook_url} доступен из интернета")
        except Exception as e:
            logger.error(f"Ошибка при настройке webhook: {e}", exc_info=True)


@app.on_event("shutdown")
async def shutdown():
    """
    Обработка shutdown события.

    Примечание: ошибки gzip (RuntimeError: lost gzip_file) при остановке контейнера
    не критичны - они уже помечены как "Exception ignored" в Python и не влияют
    на работу приложения. Это происходит потому что файловые дескрипторы закрываются
    раньше, чем gzip-стримы успевают закрыться.
    """
    logger = logging.getLogger(__name__)
    try:
        await close_mongo_connection()
    except Exception as e:
        logger.error(f"Ошибка при закрытии соединения с MongoDB: {e}")

    try:
        await close_redis()
    except Exception as e:
        logger.error(f"Ошибка при закрытии соединения с Redis: {e}")


app.include_router(catalog.router, prefix=settings.api_prefix)
app.include_router(cart.router, prefix=settings.api_prefix)
app.include_router(orders.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)
app.include_router(store.router, prefix=settings.api_prefix)
app.include_router(bot_webhook.router, prefix=settings.api_prefix)


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "Mini Shop API is running"}


@app.get("/health")
async def health():
    """Health check endpoint that doesn't require database."""
    return {"status": "ok", "message": "Server is running"}


@app.get("/debug/env")
async def debug_env():
    """Debug endpoint to check environment variables."""
    import os
    
    # Получаем все переменные окружения, связанные с ADMIN
    admin_vars = {k: v for k, v in os.environ.items() if "ADMIN" in k.upper()}
    
    return {
        "admin_ids_from_settings": settings.admin_ids,
        "admin_ids_from_env": os.getenv("ADMIN_IDS"),
        "all_admin_env_vars": admin_vars,
        "env_file_exists": ENV_PATH.exists() if hasattr(settings, "ENV_PATH") else False,
    }


# SPA fallback - отдаем Next.js для всех не-API маршрутов
# В production на Railway Next.js работает через FastAPI прокси
# Next.js standalone server обрабатывает маршруты через rewrites
