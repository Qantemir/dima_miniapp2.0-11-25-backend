.PHONY: lint format type-check check all install-dev db-export db-export-railway db-export-railway-direct db-import db-backup-railway db-restore-railway

# Установка dev зависимостей
install-dev:
	pip install -r requirements-dev.txt

# Проверка стиля кода с flake8
lint:
	@echo "🔍 Запуск flake8..."
	flake8 app/ --config=.flake8
	@echo "✅ flake8 проверка завершена"

# Проверка с pylint
pylint:
	@echo "🔍 Запуск pylint..."
	pylint app/ --rcfile=.pylintrc
	@echo "✅ pylint проверка завершена"

# Проверка типов с mypy
type-check:
	@echo "🔍 Запуск mypy..."
	mypy app/ --config-file=.mypy.ini
	@echo "✅ mypy проверка завершена"

# Автоматическое форматирование кода
format:
	@echo "🎨 Форматирование кода с black..."
	black app/ --config=pyproject.toml
	@echo "📦 Сортировка импортов с isort..."
	isort app/ --settings-file=pyproject.toml
	@echo "✅ Форматирование завершено"

# Проверка форматирования (без изменений)
format-check:
	@echo "🔍 Проверка форматирования..."
	black app/ --check --config=pyproject.toml
	isort app/ --check-only --settings-file=pyproject.toml
	@echo "✅ Форматирование корректно"

# Все проверки
check: lint pylint type-check format-check
	@echo "✅ Все проверки пройдены!"

# Быстрая проверка (только flake8)
quick-check:
	@echo "⚡ Быстрая проверка..."
	flake8 app/ --config=.flake8 --count --statistics
	@echo "✅ Быстрая проверка завершена"

# Экспорт базы данных
db-export:
	@echo "📦 Экспорт базы данных..."
	@chmod +x export_db.sh
	./export_db.sh

# Экспорт базы данных через Railway (для внутренних адресов)
db-export-railway:
	@echo "🚇 Экспорт базы данных через Railway..."
	@chmod +x export_db_railway.sh
	./export_db_railway.sh

# Экспорт базы данных через Railway CLI напрямую (рекомендуется для Railway)
db-export-railway-direct:
	@echo "🚇 Экспорт базы данных через Railway CLI..."
	@chmod +x export_db_railway_direct.sh
	./export_db_railway_direct.sh

# Импорт базы данных
# Использование: make db-import FILE=backups/miniapp_20240101_120000.tar.gz
# Или с удалением существующих коллекций: make db-import FILE=backups/miniapp_20240101_120000.tar.gz DROP=true
db-import:
	@if [ -z "$(FILE)" ]; then \
		echo "❌ Ошибка: не указан файл бэкапа"; \
		echo "Использование: make db-import FILE=backups/miniapp_20240101_120000.tar.gz"; \
		exit 1; \
	fi
	@echo "📥 Импорт базы данных..."
	@chmod +x import_db.sh
	@if [ "$(DROP)" = "true" ]; then \
		./import_db.sh "$(FILE)" --drop; \
	else \
		./import_db.sh "$(FILE)"; \
	fi

# ============================================
# Бэкапы Railway (рекомендуется)
# ============================================

# Экспорт БД из Railway (простой способ)
# Использование: make db-backup-railway
# Требуется: railway service <имя_сервиса> (не MongoDB)
db-backup-railway:
	@echo "🚇 Экспорт БД из Railway..."
	@chmod +x backup_railway.sh
	./backup_railway.sh

# Импорт БД в Railway
# Использование: make db-restore-railway FILE=backups/miniapp_railway_20240101_120000.archive
# Или с удалением существующих коллекций: make db-restore-railway FILE=backups/miniapp_railway_20240101_120000.archive DROP=true
# Требуется: railway service <имя_сервиса> (не MongoDB)
db-restore-railway:
	@if [ -z "$(FILE)" ]; then \
		echo "❌ Ошибка: не указан файл бэкапа"; \
		echo "Использование: make db-restore-railway FILE=backups/miniapp_railway_20240101_120000.archive"; \
		exit 1; \
	fi
	@echo "🚇 Импорт БД в Railway..."
	@chmod +x restore_railway.sh
	@if [ "$(DROP)" = "true" ]; then \
		./restore_railway.sh "$(FILE)" --drop; \
	else \
		./restore_railway.sh "$(FILE)"; \
	fi

