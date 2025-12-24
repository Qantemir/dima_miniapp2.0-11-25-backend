#!/bin/bash

# Скрипт для сборки Docker образа бэкенда

set -e

echo "🔨 Сборка Docker образа для dimabot-backend..."

# Проверяем что Docker запущен
if ! docker ps > /dev/null 2>&1; then
    echo "❌ Ошибка: Docker daemon не запущен. Запустите Docker и попробуйте снова."
    exit 1
fi

# Собираем образ
docker build -t dimabot-backend:latest .

echo "✅ Сборка завершена успешно!"
echo "📦 Образ: dimabot-backend:latest"
echo ""
echo "Для запуска используйте:"
echo "  docker-compose up"
echo "или"
echo "  docker run -p 8000:8000 dimabot-backend:latest"

