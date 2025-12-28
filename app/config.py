"""Модуль конфигурации приложения."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, ConfigDict, AliasChoices, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Any, List

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT_DIR / ".env"


def _parse_id_list(value: Any) -> List[int]:
    """
    Парсит значение в список целых чисел.
    Обрабатывает строки вида "123,456", списки и другие типы.
    """
    if isinstance(value, list):
        return [int(v) for v in value if v]
    if isinstance(value, str):
        str_value = value.strip()
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
                    # Пропускаем некорректное значение
                    pass
        return ids
    return []


class Settings(BaseSettings):
    """Настройки приложения."""

    mongo_uri: str = Field("mongodb://localhost:27017", env="MONGO_URI")
    mongo_db: str = Field("miniapp", env="MONGO_DB")
    redis_url: str = Field("redis://localhost:6379/0", env="REDIS_URL")
    api_prefix: str = "/api"
    # BaseSettings автоматически загружает переменные окружения по имени поля (case-insensitive)
    # Но для надежности также проверяем ADMIN_IDS в валидаторе
    admin_ids: List[int] = Field(default_factory=list)

    @property
    def admin_ids_set(self) -> set[int]:
        """Кэшированный set для быстрой проверки в verify_admin."""
        if not hasattr(self, "_admin_ids_set_cache"):
            self._admin_ids_set_cache = set(self.admin_ids) if self.admin_ids else set()
        return self._admin_ids_set_cache


    telegram_bot_token: str | None = Field(None, env="TELEGRAM_BOT_TOKEN")
    # Значения по умолчанию, можно переопределить через env при необходимости
    upload_dir: Path = Field(ROOT_DIR / "uploads", env="UPLOAD_DIR")
    max_receipt_size_mb: int = Field(10, env="MAX_RECEIPT_SIZE_MB")  # 10 МБ по умолчанию
    public_url: str | None = Field(
        None, env="PUBLIC_URL"
    )  # Публичный URL для webhook (например, https://your-domain.com)

    @field_validator("public_url", mode="before")
    @classmethod
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

    @model_validator(mode="before")
    @classmethod
    def parse_id_fields_before(cls, data: Any) -> Any:
        """
        Обрабатывает поля admin_ids до создания модели.
        Это предотвращает попытку JSON парсинга pydantic-settings.
        """
        if isinstance(data, dict):
            # Обрабатываем admin_ids
            if "admin_ids" in data:
                data["admin_ids"] = _parse_id_list(data["admin_ids"])
            if "ADMIN_IDS" in data:
                data["admin_ids"] = _parse_id_list(data["ADMIN_IDS"])
                del data["ADMIN_IDS"]
            
        
        return data

    @model_validator(mode="after")
    def load_env_variables(self):
        """Загружает переменные окружения, если они не были загружены автоматически."""
        # Загружаем ADMIN_IDS
        if not self.admin_ids:
            env_value = os.getenv("ADMIN_IDS")
            if env_value:
                str_value = env_value.strip()
                if str_value:
                    ids = []
                    for v in str_value.split(","):
                        v = v.strip()
                        if v:
                            try:
                                ids.append(int(v))
                            except ValueError:
                                pass
                    if ids:
                        self.admin_ids = ids
        
        # Загружаем критические строковые переменные, если они не загрузились
        # (BaseSettings должен загружать их автоматически, но для надежности проверяем)
        if not self.mongo_uri or self.mongo_uri == "mongodb://localhost:27017":
            env_value = os.getenv("MONGO_URI")
            if env_value:
                self.mongo_uri = env_value.strip()
        
        if not self.redis_url or self.redis_url == "redis://localhost:6379/0":
            env_value = os.getenv("REDIS_URL")
            if env_value:
                self.redis_url = env_value.strip()
        
        if not self.telegram_bot_token:
            env_value = os.getenv("TELEGRAM_BOT_TOKEN")
            if env_value:
                self.telegram_bot_token = env_value.strip()
        
        return self

    @field_validator("upload_dir", mode="before")
    @classmethod
    def ensure_upload_dir(cls, value):
        """Обеспечивает, что upload_dir является Path объектом."""
        if isinstance(value, Path):
            return value
        return Path(value)

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH) if ENV_PATH.exists() else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=False,  # Не игнорируем пустые значения, чтобы видеть, что переменная установлена
        extra="ignore",
        # Явно указываем, что нужно загружать из переменных окружения
        env_prefix="",  # Без префикса
    )


@lru_cache
def get_settings() -> Settings:
    """Получить настройки приложения."""
    import logging
    import os
    
    logger = logging.getLogger(__name__)
    
    # Детальная диагностика переменных окружения перед созданием Settings
    admin_ids_from_env = os.getenv("ADMIN_IDS")
    admin_ids_from_file = None
    
    # Проверяем все возможные источники
    # Проверяем .env файл
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("ADMIN_IDS="):
                        admin_ids_from_file = line.split("=", 1)[1].strip()
                        break
        except Exception as e:
            pass
    
    # Создаем Settings - model_validator автоматически загрузит ADMIN_IDS из os.environ, если нужно
    try:
        settings = Settings()
    except Exception as e:
        logger.error(f"❌ Ошибка при создании Settings: {e}", exc_info=True)
        raise
    
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    
    # Логируем информацию о ADMIN_IDS
    if not settings.admin_ids:
        logger.error(
            "❌ ADMIN_IDS не настроен или пуст! "
            "Автоматический редирект для админов не будет работать на фронтенде."
        )
        logger.error("   Установите переменную окружения ADMIN_IDS в Railway для бэкенда!")
        logger.error("   Формат: ADMIN_IDS=123456789,987654321")
        logger.error("   💡 Перейдите в Railway → Settings → Variables → Add Variable")
        logger.error("   💡 Имя: ADMIN_IDS")
        logger.error("   💡 Значение: 123456789,987654321 (замените на ваши реальные ID)")
    
    return settings


settings = get_settings()
