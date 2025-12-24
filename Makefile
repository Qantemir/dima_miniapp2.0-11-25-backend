.PHONY: lint format type-check check all install-dev

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

