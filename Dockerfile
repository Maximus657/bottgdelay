# Используем легкий образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем часовой пояс (ОБЯЗАТЕЛЬНО, иначе scheduler будет работать по UTC)
# Замените Europe/Moscow на свой пояс, если нужно
ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Копируем файл зависимостей и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код бота
COPY . .

# Создаем папку для данных (для базы данных)
RUN mkdir -p /app/data

# Команда запуска
CMD ["sleep", "infinity"]