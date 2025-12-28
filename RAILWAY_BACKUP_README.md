# Бэкапы с Railway

Простая система для создания и восстановления бэкапов базы данных MongoDB из/в Railway.

## Быстрый старт

### 1. Экспорт БД из Railway

```bash
cd backend

# Переключитесь на сервис приложения (не MongoDB)
railway service <имя_вашего_сервиса>

# Создайте бэкап
make db-backup-railway
# или
./backup_railway.sh
```

Бэкап будет сохранен в `backups/miniapp_railway_YYYYMMDD_HHMMSS.archive`

### 2. Импорт БД в Railway

```bash
cd backend

# Переключитесь на сервис приложения (не MongoDB)
railway service <имя_вашего_сервиса>

# Импортируйте бэкап
make db-restore-railway FILE=backups/miniapp_railway_20240101_120000.archive
# или
./restore_railway.sh backups/miniapp_railway_20240101_120000.archive

# С удалением существующих коллекций
make db-restore-railway FILE=backups/miniapp_railway_20240101_120000.archive DROP=true
# или
./restore_railway.sh backups/miniapp_railway_20240101_120000.archive --drop
```

## Требования

1. **Railway CLI** установлен и авторизован:
   ```bash
   brew install railway
   railway login
   ```

2. **MongoDB Database Tools** установлены на Railway сервере:
   - Railway обычно предоставляет их автоматически
   - Если нет, добавьте в Dockerfile:
     ```dockerfile
     RUN apt-get update && apt-get install -y mongodb-database-tools
     ```

3. **Сервис подключен к MongoDB** в Railway:
   - Railway автоматически предоставляет переменную `MONGO_URL` для сервисов, подключенных к MongoDB

## Как это работает

### Экспорт (`backup_railway.sh`)

1. Скрипт проверяет, что вы переключены на сервис приложения (не MongoDB)
2. Использует `railway run` для выполнения `mongodump` на Railway сервере
3. Использует переменную `MONGO_URL`, которую Railway предоставляет автоматически
4. Сохраняет архив в локальную директорию `backups/`

### Импорт (`restore_railway.sh`)

1. Скрипт проверяет, что вы переключены на сервис приложения
2. Кодирует архив в base64
3. Передает архив через `railway run` на сервер
4. Декодирует архив и выполняет `mongorestore`

## Альтернативные методы

### Экспорт через локальное подключение

Если у вас есть публичный URI MongoDB:

```bash
export MONGO_URI='mongodb://user:pass@host:port'
make db-export
```

### Импорт через локальное подключение

```bash
export MONGO_URI='mongodb://user:pass@host:port'
make db-import FILE=backups/miniapp_20240101_120000.tar.gz
```

### Использование Railway туннеля

```bash
# В одном терминале
railway connect

# В другом терминале
export MONGO_URI='mongodb://localhost:27017'
make db-export
# или
make db-import FILE=backups/miniapp_20240101_120000.tar.gz
```

## Структура файлов

- `backup_railway.sh` - экспорт БД из Railway
- `restore_railway.sh` - импорт БД в Railway
- `export_db.sh` - экспорт БД (локальный или через публичный URI)
- `import_db.sh` - импорт БД (локальный или через публичный URI)
- `backups/` - директория для хранения бэкапов

## Форматы файлов

- `.archive` - формат MongoDB archive (создается через `mongodump --archive`)
  - Используется скриптами `backup_railway.sh` и `restore_railway.sh`
  
- `.tar.gz` - сжатый tar архив (создается через `export_db.sh`)
  - Используется скриптами `export_db.sh` и `import_db.sh`

## Ограничения

- Файлы больше 100MB могут быть проблематичными для импорта через `restore_railway.sh` (из-за ограничений командной строки)
- Для больших файлов используйте альтернативные методы (туннель или публичный URI)

## Troubleshooting

### Ошибка: "MONGO_URL не установлен"

Убедитесь, что:
1. Вы переключены на сервис приложения (не MongoDB)
2. Сервис подключен к MongoDB в Railway Dashboard
3. Railway предоставляет переменную `MONGO_URL` автоматически

Проверка:
```bash
railway variables | grep MONGO
```

### Ошибка: "mongodump не найден"

Добавьте MongoDB Database Tools в Dockerfile:
```dockerfile
RUN apt-get update && apt-get install -y mongodb-database-tools
```

### Ошибка: "Внутренний адрес недоступен"

Используйте публичный URI или Railway туннель (см. альтернативные методы выше).

## Автоматизация

Для автоматических бэкапов можно использовать cron или GitHub Actions:

```bash
# Cron (ежедневно в 2:00)
0 2 * * * cd /path/to/backend && railway service <service> && ./backup_railway.sh
```

