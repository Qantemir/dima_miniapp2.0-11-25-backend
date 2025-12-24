# Multi-stage build для бэкенда с фронтендом
# Этот Dockerfile ожидает, что фронтенд будет собран отдельно и скопирован в образ
# Или можно использовать multi-stage build с фронтендом

# Stage 1: Frontend build (опционально, если фронтенд в том же репозитории)
# Если фронтенд в отдельном репо, этот stage можно пропустить
FROM node:20-alpine AS frontend-builder
WORKDIR /app

# Копируем package.json и yarn.lock фронтенда (если есть)
# Если фронтенд в отдельном репо, закомментируйте этот stage
# COPY ../frontend/package.json ../frontend/yarn.lock ./
# RUN yarn install
# COPY ../frontend/ .
# RUN yarn build

# Stage 2: Backend
FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements.txt и устанавливаем Python зависимости
COPY requirements.txt .
# Показываем содержимое для отладки и принудительно сбрасываем кэш
RUN echo "=== Requirements.txt content ===" && cat requirements.txt && echo "=== Installing dependencies ===" && pip install --no-cache-dir -r requirements.txt

# Копируем бэкенд
COPY app/ ./app/

# Копируем собранный фронтенд (если был собран в Stage 1)
# Если фронтенд в отдельном репо, скопируйте .next и public вручную
# COPY --from=frontend-builder /app/.next/standalone ./
# COPY --from=frontend-builder /app/.next/static ./.next/static
# COPY --from=frontend-builder /app/public ./public

# Создаем директорию для uploads
RUN mkdir -p uploads

# Переменные окружения по умолчанию
ENV PYTHONUNBUFFERED=1
ENV NEXT_PUBLIC_VITE_API_URL=/api

# Устанавливаем Node.js для запуска Next.js standalone server (если нужен)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Запускаем FastAPI
# Если фронтенд интегрирован, можно запустить оба процесса:
# CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 & PORT=$PORT node server.js & wait"]
# Или только бэкенд:
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

