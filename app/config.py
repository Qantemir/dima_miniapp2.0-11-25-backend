from functools import lru_cache
from pathlib import Path
from typing import List
import os

from pydantic import Field, validator
from pydantic_settings import BaseSettings

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT_DIR / ".env"


class Settings(BaseSettings):
  mongo_uri: str = Field("mongodb://localhost:27017", env="MONGO_URI")
  mongo_db: str = Field("miniapp", env="MONGO_DB")
  redis_url: str = Field("redis://localhost:6379/0", env="REDIS_URL")
  api_prefix: str = "/api"
  admin_ids: List[int] = Field(default_factory=list, env="ADMIN_IDS")
  
  @property
  def admin_ids_set(self) -> set[int]:
    """Кэшированный set для быстрой проверки в verify_admin"""
    if not hasattr(self, '_admin_ids_set_cache'):
      self._admin_ids_set_cache = set(self.admin_ids) if self.admin_ids else set()
    return self._admin_ids_set_cache
  telegram_bot_token: str | None = Field(None, env="TELEGRAM_BOT_TOKEN")
  jwt_secret: str = Field("change-me", env="JWT_SECRET")
  upload_dir: Path = Field(ROOT_DIR / "uploads", env="UPLOAD_DIR")
  max_receipt_size_mb: int = Field(10, env="MAX_RECEIPT_SIZE_MB")
  telegram_data_ttl_seconds: int = Field(300, env="TELEGRAM_DATA_TTL_SECONDS")
  allow_dev_requests: bool = Field(True, env="ALLOW_DEV_REQUESTS")
  dev_allowed_user_ids: List[int] = Field(default_factory=list, env="DEV_ALLOWED_USER_IDS")
  default_dev_user_id: int | None = Field(1, env="DEFAULT_DEV_USER_ID")
  enforce_telegram_signature: bool = Field(False, env="ENFORCE_TELEGRAM_SIGNATURE")
  catalog_cache_ttl_seconds: int = Field(600, env="CATALOG_CACHE_TTL_SECONDS")  # 10 минут для максимальной производительности
  broadcast_batch_size: int = Field(25, env="BROADCAST_BATCH_SIZE")
  broadcast_concurrency: int = Field(10, env="BROADCAST_CONCURRENCY")
  environment: str = Field("development", env="ENVIRONMENT")
  public_url: str | None = Field(None, env="PUBLIC_URL")  # Публичный URL для webhook (например, https://your-domain.com)

  @validator("public_url", pre=True)
  def auto_detect_public_url(cls, value):
    """Автоматически определяет PUBLIC_URL из переменных окружения хостинга, если не указан явно."""
    if value:
      return value
    
    # Пытаемся определить из переменных окружения различных хостингов
    # Railway
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_STATIC_URL")
    if railway_domain:
      # Railway может предоставить домен без протокола
      if railway_domain.startswith("http"):
        return railway_domain
      return f"https://{railway_domain}"
    
    # Render
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
      return render_url
    
    # Fly.io
    fly_app = os.getenv("FLY_APP_NAME")
    if fly_app:
      return f"https://{fly_app}.fly.dev"
    
    # Heroku (нужно использовать кастомную переменную или определить из Request)
    # Для Heroku лучше указать PUBLIC_URL явно
    
    # Vercel
    vercel_url = os.getenv("VERCEL_URL")
    if vercel_url:
      return f"https://{vercel_url}"
    
    # Общая переменная для многих платформ
    service_url = os.getenv("SERVICE_URL") or os.getenv("APP_URL")
    if service_url:
      return service_url
    
    return None

  @validator("admin_ids", pre=True)
  def split_admin_ids(cls, value):
    if not value:
      return []
    if isinstance(value, list):
      return [int(v) for v in value]
    # Обрабатываем строку - убираем пробелы и разбиваем по запятой
    str_value = str(value).strip()
    if not str_value:
      return []
    # Разбиваем по запятой и обрабатываем каждый элемент
    ids = []
    for v in str_value.split(","):
      v = v.strip()
      if v:
        try:
          ids.append(int(v))
        except ValueError:
          # Логируем, но не падаем - просто пропускаем некорректное значение
          import logging
          logger = logging.getLogger(__name__)
          logger.warning(f"Некорректное значение в ADMIN_IDS: '{v}', пропускаем")
    return ids

  @validator("upload_dir", pre=True)
  def ensure_upload_dir(cls, value):
    if isinstance(value, Path):
      return value
    return Path(value)

  @validator("dev_allowed_user_ids", pre=True)
  def split_dev_ids(cls, value):
    if not value:
      return []
    if isinstance(value, list):
      return [int(v) for v in value]
    return [int(v.strip()) for v in str(value).split(",") if v.strip()]

  class Config:
    env_file = ENV_PATH
    env_file_encoding = "utf-8"
    case_sensitive = False


@lru_cache
def get_settings() -> Settings:
  settings = Settings()
  settings.upload_dir.mkdir(parents=True, exist_ok=True)
  return settings


settings = get_settings()

