# Используем официальный легкий образ Python 3.11
FROM python:3.11-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Задаем переменные окружения:
# PYTHONDONTWRITEBYTECODE 1 — не создавать .pyc файлы
# PYTHONUNBUFFERED 1 — чтобы логи сразу летели в консоль (важно для Dokploy)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Устанавливаем системные зависимости (нужны для сборки некоторых python-библиотек)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Копируем весь код проекта в контейнер
COPY . .

# Команда запуска бота
CMD ["python", "bot.py"]