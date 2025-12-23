import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import close_mongo_connection, connect_to_mongo
from .cache import close_redis, get_redis
from .utils import permanently_delete_order_entry
from .routers import admin, bot_webhook, cart, catalog, orders, store

app = FastAPI(title="Mini Shop Telegram Backend", version="1.0.0")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse, Response
from starlette.concurrency import iterate_in_threadpool
import gzip
import io


class SafeGZipMiddleware(BaseHTTPMiddleware):
    """
    Собственная реализация GZip, которая не ломается на закрытых стримах
    и пропускает SSE/streaming/HEAD/304 ответы.
    """

    def __init__(self, app, minimum_size: int = 1000):
        super().__init__(app)
        self.minimum_size = minimum_size

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.method == "HEAD":
            return response
        if response.status_code in (204, 304):
            return response
        if isinstance(response, StreamingResponse) or (
            getattr(response, "media_type", None) == "text/event-stream"
        ):
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

# Добавляем Rate Limiting (только в продакшене или по настройке)
from .config import settings
if settings.environment == "production":
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
import os
from pathlib import Path

# Определяем путь к dist папке, учитывая разные запуски (uvicorn/Procfile/Dockerfile)
logger = logging.getLogger(__name__)

def _find_next_dir() -> Path:
    """Ищет папку .next для Next.js standalone output"""
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
        if settings.environment != "production":
            logger.info(f"🔍 Checking Next.js output at: {next_path}")
        if next_path.exists():
            if settings.environment != "production":
                logger.info(f"✅ Using Next.js output at: {next_path}")
            return next_path

    # Фолбэк — нет .next, вернём путь по умолчанию
    if settings.environment != "production":
        logger.warning("⚠️ .next directory not found, frontend will not be served")
    return Path("/.next")  # заведомо несуществующий

next_dir = _find_next_dir()

# Монтируем статические файлы Next.js
static_dir = next_dir / "static"
if static_dir.exists():
    if settings.environment != "production":
        logger.info(f"✅ Mounting Next.js static files from: {static_dir}")
    app.mount("/_next/static", StaticFiles(directory=str(static_dir)), name="next-static")

# Монтируем Next.js standalone server файлы для SSR
standalone_dir = next_dir / "standalone"
if standalone_dir.exists():
    server_dir = standalone_dir / "server"
    if server_dir.exists():
        if settings.environment != "production":
            logger.info(f"✅ Found Next.js standalone server at: {server_dir}")
        # Монтируем server chunks если есть
        chunks_dir = server_dir / "chunks"
        if chunks_dir.exists():
            app.mount("/_next/chunks", StaticFiles(directory=str(chunks_dir)), name="next-chunks")

# Монтируем public файлы
here = Path(__file__).resolve()
public_dir = here.parent.parent.parent / "public" if (here.parent.parent.parent / "public").exists() else Path.cwd() / "public"
if public_dir.exists():
    if settings.environment != "production":
        logger.info(f"✅ Mounting public files from: {public_dir}")
    # Монтируем favicon
    @app.get("/favicon.svg")
    async def favicon():
        from fastapi.responses import FileResponse
        from fastapi import HTTPException
        favicon_path = public_dir / "favicon.svg"
        if favicon_path.exists():
            return FileResponse(str(favicon_path))
        raise HTTPException(status_code=404)


@app.middleware("http")
async def apply_security_and_cache_headers(request, call_next):
  response = await call_next(request)
  
  # Cache-Control headers для оптимизации
  path = request.url.path
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
  Фоновая задача для окончательного удаления заказов,
  которые были помечены как удаленные более 10 минут назад.
  """
  from datetime import datetime, timedelta
  from .database import get_db
  from .utils import permanently_delete_order_entry
  from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError
  
  import asyncio
  logger = logging.getLogger(__name__)
  
  while True:
    try:
      # Получаем базу данных
      db = await get_db()
      
      # Находим заказы, удаленные более 10 минут назад
      cutoff_time = datetime.utcnow() - timedelta(minutes=10)
      deleted_orders = await db.orders.find({
        "deleted_at": {"$exists": True, "$lte": cutoff_time}
      }).to_list(length=100)
      
      for order_doc in deleted_orders:
        try:
          await permanently_delete_order_entry(db, order_doc)
          logger.info(f"Окончательно удален заказ {order_doc.get('_id')}")
        except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError) as e:
          # Временные проблемы с подключением - логируем как предупреждение, не как ошибку
          logger.warning(f"Временная проблема с подключением к MongoDB при удалении заказа {order_doc.get('_id')}: {e}")
        except Exception as e:
          logger.error(f"Ошибка при окончательном удалении заказа {order_doc.get('_id')}: {e}")
      
      # Ждем перед следующей проверкой (дольше в production для экономии ресурсов)
      from .config import settings
      sleep_time = 300 if settings.environment == "production" else 60  # 5 минут в production, 1 минута в dev
      await asyncio.sleep(sleep_time)
    except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError) as e:
      # Временные проблемы с подключением - логируем только в dev
      if settings.environment != "production":
        logger.warning(f"Временная проблема с подключением к MongoDB в фоновой задаче очистки заказов: {e}")
      sleep_time = 300 if settings.environment == "production" else 60
      await asyncio.sleep(sleep_time)
    except Exception as e:
      # Логируем только критические ошибки в production
      if settings.environment != "production":
        logger.error(f"Ошибка в фоновой задаче очистки заказов: {e}")
      sleep_time = 300 if settings.environment == "production" else 60
      await asyncio.sleep(sleep_time)


@app.on_event("startup")
async def startup():
  # Настраиваем логирование для максимальной производительности
  # Убираем лишние логи в production
  if settings.environment == "production":
    logging.getLogger("pymongo").setLevel(logging.ERROR)  # Только ошибки
    logging.getLogger("motor").setLevel(logging.ERROR)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # Убираем access logs
  else:
    pymongo_logger = logging.getLogger("pymongo")
    pymongo_logger.setLevel(logging.WARNING)  # Только предупреждения и ошибки
  
  # Подключаемся к MongoDB при старте для быстрого первого запроса
  await connect_to_mongo()
  
  # Подключаемся к Redis при старте
  await get_redis()
  
  # Запускаем фоновую задачу для очистки удаленных заказов (реже в production)
  import asyncio
  if settings.environment == "production":
    # В production очищаем реже для экономии ресурсов
    asyncio.create_task(cleanup_deleted_orders())
  else:
    asyncio.create_task(cleanup_deleted_orders())
  
  # Настраиваем webhook для Telegram Bot API (если указан публичный URL)
  import os
  logger = logging.getLogger(__name__)
  
  # Проверяем, был ли PUBLIC_URL определен автоматически
  if settings.public_url:
    # Проверяем, был ли он установлен явно через переменную окружения
    explicit_public_url = os.getenv("PUBLIC_URL")
    if explicit_public_url:
      logger.info(f"PUBLIC_URL установлен явно: {settings.public_url}")
    else:
      logger.info(f"PUBLIC_URL определен автоматически из переменных окружения хостинга: {settings.public_url}")
  
  if settings.telegram_bot_token and settings.public_url:
    try:
      import httpx
      webhook_url = f"{settings.public_url.rstrip('/')}{settings.api_prefix}/bot/webhook"
      logger.info(f"Настраиваем webhook: {webhook_url} (PUBLIC_URL: {settings.public_url})")
      
      async with httpx.AsyncClient(timeout=15.0) as client:
        # Сначала удаляем старый webhook (если есть)
        try:
          await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/deleteWebhook",
            json={"drop_pending_updates": False}
          )
        except:
          pass
        
        # Устанавливаем новый webhook
        response = await client.post(
          f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
          json={
            "url": webhook_url,
            "allowed_updates": ["callback_query"]  # Только callback queries
          }
        )
        result = response.json()
        if result.get("ok"):
          logger.info(f"✅ Webhook успешно настроен: {webhook_url}")
          
          # Проверяем статус webhook
          check_response = await client.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"
          )
          check_result = check_response.json()
          if check_result.get("ok"):
            webhook_info = check_result.get("result", {})
            logger.info(f"Webhook info: url={webhook_info.get('url')}, pending={webhook_info.get('pending_update_count', 0)}")
        else:
          error_desc = result.get("description", "Unknown error")
          logger.error(f"❌ Не удалось настроить webhook: {error_desc}")
          logger.error(f"Проверьте, что URL {webhook_url} доступен из интернета")
    except Exception as e:
      logger.error(f"Ошибка при настройке webhook: {e}", exc_info=True)
  elif settings.telegram_bot_token and not settings.public_url:
    logger.warning("⚠️ TELEGRAM_BOT_TOKEN настроен, но PUBLIC_URL не указан. Webhook не будет настроен автоматически.")
    logger.warning("Добавьте PUBLIC_URL в .env или используйте POST /api/bot/webhook/setup с параметром 'url' для ручной настройки")
    logger.info("Проверьте переменные окружения: RAILWAY_PUBLIC_DOMAIN, RENDER_EXTERNAL_URL, FLY_APP_NAME, VERCEL_URL и др.")
  elif not settings.telegram_bot_token:
    logger.warning("⚠️ TELEGRAM_BOT_TOKEN не настроен. Webhook не будет работать.")


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
    logger.info("MongoDB соединение закрыто")
  except Exception as e:
    logger.warning(f"Ошибка при закрытии соединения с MongoDB: {e}")
  
  try:
    await close_redis()
    logger.info("Redis соединение закрыто")
  except Exception as e:
    logger.warning(f"Ошибка при закрытии соединения с Redis: {e}")


app.include_router(catalog.router, prefix=settings.api_prefix)
app.include_router(cart.router, prefix=settings.api_prefix)
app.include_router(orders.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)
app.include_router(store.router, prefix=settings.api_prefix)
app.include_router(bot_webhook.router, prefix=settings.api_prefix)


@app.get("/")
async def root():
  return {"message": "Mini Shop API is running"}

@app.get("/health")
async def health():
  """Health check endpoint that doesn't require database."""
  return {"status": "ok", "message": "Server is running"}

# SPA fallback - отдаем Next.js для всех не-API маршрутов
# В production на Railway Next.js работает через FastAPI прокси
# Next.js standalone server обрабатывает маршруты через rewrites

