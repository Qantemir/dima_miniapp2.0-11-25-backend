# Backend API - Mini Shop

FastAPI бэкенд для Telegram мини-приложения интернет-магазина.

## Технологии

- **FastAPI** - веб-фреймворк
- **Python 3.11** - язык программирования
- **MongoDB** - база данных
- **Redis** - кэширование
- **Python Telegram Bot** - интеграция с Telegram

## Структура проекта

```
backend/
├── app/
│   ├── routers/      # API роутеры
│   │   ├── catalog.py    # Каталог товаров
│   │   ├── cart.py       # Корзина
│   │   ├── orders.py     # Заказы
│   │   ├── admin.py      # Админ панель
│   │   ├── store.py      # Настройки магазина
│   │   └── bot_webhook.py # Telegram webhook
│   ├── middleware/   # Middleware
│   ├── main.py       # Точка входа FastAPI
│   └── ...
├── requirements.txt  # Python зависимости
├── Dockerfile        # Docker образ
└── docker-compose.yml # Docker Compose конфигурация
```

## Установка

```bash
# Установить зависимости
pip install -r requirements.txt
```

## Разработка

```bash
# Запустить в режиме разработки
python3 -m uvicorn app.main:app --reload --port 8000

# Или через скрипт
./start.sh
```

## Docker

### Локальная разработка с Docker Compose

```bash
# Запустить все сервисы (Backend, MongoDB, Redis)
docker-compose up -d

# Остановить
docker-compose down
```

### Сборка Docker образа

```bash
docker build -t backend-api .
docker run -p 8000:8000 --env-file .env backend-api
```

## API Endpoints

Все endpoints доступны по префиксу `/api`:

### Каталог
- `GET /api/catalog` - Получить каталог товаров

### Корзина
- `GET /api/cart` - Получить корзину пользователя
- `POST /api/cart` - Добавить товар в корзину
- `PATCH /api/cart/item` - Обновить товар в корзине
- `DELETE /api/cart/item` - Удалить товар из корзины

### Заказы
- `POST /api/order` - Создать заказ

### Админ панель
- `GET /api/admin/orders` - Список заказов
- `GET /api/admin/order/:id` - Детали заказа
- `PATCH /api/admin/order/:id/status` - Обновить статус заказа
- `POST /api/admin/order/:id/restore` - Восстановить заказ
- `GET /api/admin/catalog` - Каталог (админ)
- `POST /api/admin/product` - Создать товар
- `PATCH /api/admin/product/:id` - Обновить товар
- `DELETE /api/admin/product/:id` - Удалить товар
- `POST /api/admin/category` - Создать категорию
- `PATCH /api/admin/category/:id` - Обновить категорию
- `DELETE /api/admin/category/:id` - Удалить категорию
- `POST /api/admin/broadcast` - Отправить рассылку

### Настройки магазина
- `GET /api/store/status` - Статус магазина
- `PATCH /api/admin/store/sleep` - Установить режим сна
- `PATCH /api/admin/store/payment-link` - Установить ссылку на оплату

### Telegram Bot
- `POST /api/bot/webhook` - Webhook для Telegram

## Переменные окружения

Создайте `.env` файл:

```env
# MongoDB
MONGO_URI=mongodb://localhost:27017
MONGO_DB=miniapp

# Redis
REDIS_URL=redis://localhost:6379/0

# Telegram Bot
TELEGRAM_BOT_TOKEN=your_bot_token
ADMIN_IDS=123456789,987654321

# Security
JWT_SECRET=your-secret-key-change-in-production

# URLs
NEXT_PUBLIC_VITE_API_URL=/api
NEXT_PUBLIC_VITE_PUBLIC_URL=http://localhost:8000
PUBLIC_URL=http://localhost:8000
```

## Production развертывание

### Railway

Проект настроен для развертывания на Railway. Подключите репозиторий к Railway, и он автоматически соберет и запустит приложение используя `Dockerfile` и `railway.json`.

### Переменные окружения в Production

Убедитесь, что все переменные окружения установлены в настройках Railway.

# dima_miniapp2.0-11-25-backend
