import os
import logging
import sys

# ==============================================================================
# КОНФИГУРАЦИЯ ПРОЕКТА
# ==============================================================================

# Токен бота
API_TOKEN = os.getenv('API_TOKEN')

# ID администраторов (парсинг из строки через запятую)
admin_ids_str = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = [int(x) for x in admin_ids_str.split(',')] if admin_ids_str else []

# URL базы данных (PostgreSQL)
DATABASE_URL = os.getenv('DATABASE_URL')

# Токен Яндекс.Диска
YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN')
YANDEX_UPLOAD_FOLDER = "label_bot_files"

# Роли пользователей
ROLES_MAP = {
    "👑 Основатель": "founder",
    "🎧 A&R Менеджер": "anr",
    "🎨 Дизайнер": "designer",
    "📱 SMM Специалист": "smm"
}
ROLES_DISPLAY = {v: k for k, v in ROLES_MAP.items()}

# Настройка логгирования
def setup_logging():
    """
    Настраивает базовое логгирование для проекта.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout
    )

logger = logging.getLogger("LabelBot")
