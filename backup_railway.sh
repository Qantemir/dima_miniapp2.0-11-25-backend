#!/bin/bash

# Простой скрипт для экспорта БД из Railway
# Использование: ./backup_railway.sh

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${GREEN}🚇 Экспорт БД из Railway${NC}"
echo ""

# Проверяем наличие railway CLI
if ! command -v railway &> /dev/null; then
    echo -e "${RED}❌ Railway CLI не установлен${NC}"
    echo -e "${YELLOW}Установите: brew install railway${NC}"
    exit 1
fi

# Проверяем авторизацию
if ! railway whoami &> /dev/null; then
    echo -e "${RED}❌ Не авторизованы в Railway${NC}"
    echo -e "${YELLOW}Выполните: railway login${NC}"
    exit 1
fi

# Загружаем переменные окружения из .env если существует
if [ -f .env ]; then
    set -a
    source .env 2>/dev/null || true
    set +a
fi

MONGO_DB="${MONGO_DB:-miniapp}"
OUTPUT_DIR="./backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
ARCHIVE_NAME="${OUTPUT_DIR}/${MONGO_DB}_railway_${TIMESTAMP}.archive"

# Создаем директорию для бэкапов
mkdir -p "${OUTPUT_DIR}"

echo -e "${BLUE}📋 Параметры экспорта:${NC}"
echo -e "  База данных: ${YELLOW}${MONGO_DB}${NC}"
echo -e "  Выходной файл: ${YELLOW}${ARCHIVE_NAME}${NC}"
echo ""

# Проверяем текущий сервис
CURRENT_SERVICE=$(railway status 2>/dev/null | grep -i "service:" | awk '{print $2}' || echo "")

if [ -z "$CURRENT_SERVICE" ] || [ "$CURRENT_SERVICE" = "MongoDB" ]; then
    echo -e "${YELLOW}⚠️  Нужно переключиться на сервис приложения (не MongoDB)${NC}"
    echo ""
    echo -e "${BLUE}Доступные сервисы:${NC}"
    railway service 2>&1 | head -20 || true
    echo ""
    echo -e "${YELLOW}Переключитесь на сервис приложения:${NC}"
    echo -e "  ${GREEN}railway service <имя_сервиса>${NC}"
    echo ""
    echo -e "${YELLOW}Или укажите имя сервиса при запуске:${NC}"
    echo -e "  ${GREEN}railway service <имя> && ./backup_railway.sh${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Текущий сервис: ${CURRENT_SERVICE}${NC}"
echo ""

# Выполняем экспорт через Railway
# Railway автоматически предоставляет MONGO_URL для сервисов, подключенных к MongoDB
echo -e "${BLUE}📦 Выполняю экспорт через Railway...${NC}"
echo -e "${YELLOW}Это может занять некоторое время...${NC}"
echo ""

# Используем MONGO_URL, который Railway предоставляет автоматически
# Если MONGO_URL не установлен, пробуем использовать MONGO_URI из .env
railway run sh -c "
    if [ -n \"\${MONGO_URL}\" ]; then
        echo 'Использую MONGO_URL из Railway'
        mongodump --uri=\"\${MONGO_URL}\" --db=\"${MONGO_DB}\" --archive
    elif [ -n \"\${MONGO_URI}\" ]; then
        echo 'Использую MONGO_URI из переменных окружения'
        mongodump --uri=\"\${MONGO_URI}\" --db=\"${MONGO_DB}\" --archive
    else
        echo 'Ошибка: MONGO_URL или MONGO_URI не установлены'
        exit 1
    fi
" > "${ARCHIVE_NAME}" 2>&1

EXIT_CODE=$?

# Проверяем результат
if [ $EXIT_CODE -eq 0 ] && [ -s "${ARCHIVE_NAME}" ]; then
    # Проверяем, что это не ошибка (ошибки обычно содержат текст "error" или "failed")
    if grep -qi "error\|failed\|not found" "${ARCHIVE_NAME}" 2>/dev/null; then
        echo ""
        echo -e "${RED}❌ Ошибка при экспорте${NC}"
        echo ""
        echo -e "${BLUE}Вывод ошибки:${NC}"
        cat "${ARCHIVE_NAME}"
        rm -f "${ARCHIVE_NAME}"
        exit 1
    fi
    
    ARCHIVE_SIZE=$(du -h "${ARCHIVE_NAME}" | cut -f1)
    
    echo ""
    echo -e "${GREEN}✅ Экспорт завершен успешно!${NC}"
    echo -e "  Архив: ${YELLOW}${ARCHIVE_NAME}${NC}"
    echo -e "  Размер: ${YELLOW}${ARCHIVE_SIZE}${NC}"
    echo ""
    echo -e "${YELLOW}Для импорта используйте:${NC}"
    echo -e "  ${GREEN}./restore_railway.sh ${ARCHIVE_NAME}${NC}"
    echo -e "  или"
    echo -e "  ${GREEN}make db-restore-railway FILE=${ARCHIVE_NAME}${NC}"
else
    echo ""
    echo -e "${RED}❌ Ошибка при экспорте${NC}"
    echo ""
    
    if [ -f "${ARCHIVE_NAME}" ]; then
        echo -e "${BLUE}Вывод ошибки:${NC}"
        cat "${ARCHIVE_NAME}"
        rm -f "${ARCHIVE_NAME}"
    fi
    
    echo ""
    echo -e "${YELLOW}Возможные причины:${NC}"
    echo "  1. mongodump не установлен на Railway сервере"
    echo "  2. MONGO_URL не предоставляется Railway автоматически"
    echo "  3. Проблемы с подключением к БД"
    echo ""
    echo -e "${YELLOW}Попробуйте:${NC}"
    echo "  1. Убедитесь, что сервис подключен к MongoDB в Railway"
    echo "  2. Проверьте, что Railway предоставляет MONGO_URL:"
    echo "     railway variables | grep MONGO"
    exit 1
fi
