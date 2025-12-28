#!/bin/bash

# Простой скрипт для импорта БД в Railway
# Использование: ./restore_railway.sh <backup_file.archive> [--drop]

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Проверяем аргументы
if [ $# -eq 0 ]; then
    echo -e "${RED}❌ Ошибка: не указан файл бэкапа${NC}"
    echo -e "Использование: ${YELLOW}./restore_railway.sh <backup_file.archive> [--drop]${NC}"
    echo -e "  --drop  - удалить существующие коллекции перед импортом"
    exit 1
fi

BACKUP_FILE="$1"
DROP_FLAG=""

# Проверяем флаг --drop
if [ "$2" == "--drop" ]; then
    DROP_FLAG="--drop"
    echo -e "${YELLOW}⚠️  Режим импорта с удалением существующих коллекций${NC}"
fi

echo -e "${GREEN}🚇 Импорт БД в Railway${NC}"
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

# Проверяем наличие файла
if [ ! -f "$BACKUP_FILE" ]; then
    echo -e "${RED}❌ Ошибка: файл ${BACKUP_FILE} не найден${NC}"
    exit 1
fi

# Проверяем формат файла
if [[ "$BACKUP_FILE" == *.tar.gz ]]; then
    echo -e "${RED}❌ Ошибка: файл имеет формат .tar.gz${NC}"
    echo -e "${YELLOW}Этот скрипт работает только с .archive файлами (созданными через backup_railway.sh)${NC}"
    echo ""
    echo -e "${YELLOW}Для .tar.gz файлов используйте:${NC}"
    echo -e "  ${GREEN}./import_db.sh ${BACKUP_FILE}${NC}"
    exit 1
fi

echo -e "${BLUE}📋 Параметры импорта:${NC}"
echo -e "  Файл бэкапа: ${YELLOW}${BACKUP_FILE}${NC}"
echo -e "  База данных: ${YELLOW}${MONGO_DB}${NC}"
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
    exit 1
fi

echo -e "${GREEN}✓ Текущий сервис: ${CURRENT_SERVICE}${NC}"
echo ""

# Подтверждение перед импортом
echo -e "${YELLOW}⚠️  ВНИМАНИЕ: Данные будут импортированы в базу данных ${MONGO_DB}${NC}"
if [ -n "$DROP_FLAG" ]; then
    echo -e "${RED}⚠️  Существующие коллекции будут УДАЛЕНЫ!${NC}"
fi
echo ""
read -p "Продолжить? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo -e "${YELLOW}Импорт отменен${NC}"
    exit 0
fi

# Проверяем размер файла (ограничение ~100MB для base64 передачи)
FILE_SIZE=$(stat -f%z "${BACKUP_FILE}" 2>/dev/null || stat -c%s "${BACKUP_FILE}" 2>/dev/null)
FILE_SIZE_MB=$((FILE_SIZE / 1024 / 1024))

if [ $FILE_SIZE_MB -gt 100 ]; then
    echo -e "${YELLOW}⚠️  Внимание: файл большой (${FILE_SIZE_MB}MB)${NC}"
    echo -e "${YELLOW}Передача через base64 может быть медленной или не работать${NC}"
    echo ""
    read -p "Продолжить? (yes/no): " CONFIRM_LARGE
    if [ "$CONFIRM_LARGE" != "yes" ]; then
        echo -e "${YELLOW}Импорт отменен${NC}"
        exit 0
    fi
fi

# Создаем временный файл для передачи архива в Railway
# Используем base64 для кодирования архива
echo -e "${BLUE}📦 Подготавливаю архив для импорта (${FILE_SIZE_MB}MB)...${NC}"
TEMP_B64=$(mktemp)
base64 < "${BACKUP_FILE}" > "${TEMP_B64}"

# Выполняем импорт через Railway
echo -e "${BLUE}🔄 Выполняю импорт через Railway...${NC}"
echo -e "${YELLOW}Это может занять некоторое время...${NC}"
echo ""

# Декодируем архив и импортируем
railway run sh -c "
    ARCHIVE_B64=\$(cat <<'EOF'
$(cat "${TEMP_B64}")
EOF
)
    echo 'Декодирую архив...'
    echo \"\$ARCHIVE_B64\" | base64 -d > /tmp/restore.archive
    
    echo 'Проверяю размер архива...'
    ls -lh /tmp/restore.archive
    
    if [ -n \"\${MONGO_URL}\" ]; then
        echo 'Использую MONGO_URL из Railway'
        mongorestore --uri=\"\${MONGO_URL}\" --db=\"${MONGO_DB}\" --archive=/tmp/restore.archive ${DROP_FLAG}
    elif [ -n \"\${MONGO_URI}\" ]; then
        echo 'Использую MONGO_URI из переменных окружения'
        mongorestore --uri=\"\${MONGO_URI}\" --db=\"${MONGO_DB}\" --archive=/tmp/restore.archive ${DROP_FLAG}
    else
        echo 'Ошибка: MONGO_URL или MONGO_URI не установлены'
        exit 1
    fi
    
    rm -f /tmp/restore.archive
    echo 'Импорт завершен'
" 2>&1

EXIT_CODE=$?

# Удаляем временный файл
rm -f "${TEMP_B64}"

# Проверяем результат
if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✅ Импорт завершен успешно!${NC}"
    echo -e "  База данных ${YELLOW}${MONGO_DB}${NC} восстановлена из ${YELLOW}${BACKUP_FILE}${NC}"
else
    echo ""
    echo -e "${RED}❌ Ошибка при импорте${NC}"
    echo ""
    echo -e "${YELLOW}Возможные причины:${NC}"
    echo "  1. mongorestore не установлен на Railway сервере"
    echo "  2. MONGO_URL не предоставляется Railway автоматически"
    echo "  3. Проблемы с подключением к БД"
    echo "  4. Неверный формат архива"
    echo ""
    echo -e "${YELLOW}Попробуйте:${NC}"
    echo "  1. Убедитесь, что сервис подключен к MongoDB в Railway"
    echo "  2. Проверьте, что Railway предоставляет MONGO_URL:"
    echo "     railway variables | grep MONGO"
    exit 1
fi
