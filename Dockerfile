# 1. Используем легкий образ Python 3.11
FROM python:3.11-slim

# 2. Отключаем создание лишних файлов (__pycache__) и буферизацию вывода (чтобы логи в Dokploy шли сразу)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Создаем рабочую папку внутри контейнера
WORKDIR /app

# 4. Сначала копируем только файл с зависимостями (для кэширования Docker слоев)
COPY requirements.txt .

# 5. Обновляем pip и устанавливаем библиотеки
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 6. Теперь копируем весь остальной код проекта
COPY . .

# 7. Команда для запуска бота
CMD ["python", "main.py"]