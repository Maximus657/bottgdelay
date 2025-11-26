import asyncpg
import logging
from bot.config import DATABASE_URL, ADMIN_IDS

logger = logging.getLogger(__name__)

class Database:
    """
    Класс для асинхронной работы с базой данных PostgreSQL через asyncpg.
    """
    def __init__(self, dsn):
        """
        Инициализация класса базы данных.
        :param dsn: Строка подключения к БД (Data Source Name).
        """
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        """
        Создает пул соединений с базой данных и инициализирует таблицы.
        """
        try:
            self.pool = await asyncpg.create_pool(self.dsn)
            logger.info("Успешное подключение к базе данных.")
            await self.init_db()
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise e

    async def close(self):
        """
        Закрывает пул соединений.
        """
        if self.pool:
            await self.pool.close()

    async def init_db(self):
        """
        Создает необходимые таблицы в базе данных, если они не существуют.
        """
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    name TEXT,
                    username TEXT,
                    role TEXT
                )
            """)
            # Миграция: добавляем колонку username, если её нет
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
            except Exception:
                pass

            # Таблица артистов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS artists (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    manager_id BIGINT,
                    first_release_date TEXT,
                    flag_contract INTEGER DEFAULT 0,
                    flag_mm_profile INTEGER DEFAULT 0,
                    flag_mm_verify INTEGER DEFAULT 0,
                    flag_yt_note INTEGER DEFAULT 0,
                    flag_yt_link INTEGER DEFAULT 0
                )
            """)
            # Таблица релизов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS releases (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    artist_id INTEGER,
                    type TEXT,
                    release_date TEXT,
                    created_by BIGINT
                )
            """)
            # Таблица задач
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    description TEXT,
                    assigned_to BIGINT,
                    created_by BIGINT,
                    release_id INTEGER,
                    parent_task_id INTEGER,
                    deadline TEXT,
                    status TEXT DEFAULT 'pending',
                    requires_file INTEGER DEFAULT 0,
                    file_url TEXT,
                    comment TEXT
                )
            """)
            # Таблица отчетов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    report_date TEXT,
                    text TEXT
                )
            """)
            await self._seed_admins(conn)

    async def _seed_admins(self, conn):
        """
        Добавляет администраторов в базу данных, если их там нет.
        """
        for uid in ADMIN_IDS:
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", uid)
            if not user:
                await conn.execute("""
                    INSERT INTO users (telegram_id, name, role, username) VALUES ($1, $2, $3, $4)
                    ON CONFLICT (telegram_id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role
                """, uid, "Founder", "founder", None)

    async def get_user(self, uid):
        """
        Получает пользователя по Telegram ID.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1", uid)

    async def add_user(self, uid, name, role, username=None):
        """
        Добавляет или обновляет пользователя.
        """
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, name, role, username) VALUES ($1, $2, $3, $4)
                ON CONFLICT (telegram_id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role, username = EXCLUDED.username
            """, uid, name, role, username)

    async def delete_user(self, uid):
        """
        Удаляет пользователя по ID.
        """
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE telegram_id=$1", uid)

    async def get_all_users(self):
        """
        Возвращает список всех пользователей, отсортированных по роли.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM users ORDER BY role")

    async def delete_release_cascade(self, release_id):
        """
        Каскадное удаление релиза и связанных задач.
        """
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM tasks WHERE release_id=$1", release_id)
            await conn.execute("DELETE FROM releases WHERE id=$1", release_id)

    async def delete_task(self, task_id):
        """
        Удаляет задачу по ID.
        """
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM tasks WHERE id=$1", task_id)

    async def get_user_link(self, uid):
        """
        Генерирует HTML-ссылку на пользователя.
        """
        u = await self.get_user(uid)
        if u:
            if u.get('username'):
                return f"<a href='tg://user?id={uid}'>{u['name']}</a> (@{u['username']})"
            return f"<a href='tg://user?id={uid}'>{u['name']}</a>"
        return f"ID:{uid}"

    async def create_task(self, title, desc, assigned, created, rel_id, deadline, req_file=0, parent_id=None):
        """
        Создает новую задачу.
        """
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO tasks (title, description, assigned_to, created_by, release_id, deadline, requires_file, parent_task_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """, title, desc, assigned, created, rel_id, deadline, req_file, parent_id)

    async def get_tasks_active_founder(self):
        """
        Возвращает активные задачи для основателя (все).
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tasks WHERE status NOT IN ('done', 'rejected') ORDER BY deadline")

    async def get_tasks_active_user(self, uid):
        """
        Возвращает активные задачи для конкретного пользователя.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tasks WHERE assigned_to=$1 AND status NOT IN ('done', 'rejected') ORDER BY deadline", uid)

    async def get_task_by_id(self, tid):
        """
        Получает задачу по ID.
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)

    async def update_task_status(self, tid, status, file_url=None, comment=None):
        """
        Обновляет статус задачи, добавляет файл или комментарий.
        """
        async with self.pool.acquire() as conn:
            if file_url or comment:
                await conn.execute("UPDATE tasks SET status=$1, file_url=$2, comment=$3 WHERE id=$4", status, file_url, comment, tid)
            else:
                await conn.execute("UPDATE tasks SET status=$1 WHERE id=$2", status, tid)

    async def get_releases_paginated(self, user_role, user_id, page=0, limit=5):
        """
        Возвращает список релизов с пагинацией.
        """
        offset = page * limit
        async with self.pool.acquire() as conn:
            if user_role == 'founder':
                total = await conn.fetchval("SELECT COUNT(*) FROM releases")
                
                rows = await conn.fetch("""
                    SELECT r.*, u.name as creator_name FROM releases r
                    LEFT JOIN users u ON r.created_by = u.telegram_id
                    ORDER BY r.release_date DESC LIMIT $1 OFFSET $2
                """, limit, offset)
            else:
                total = await conn.fetchval("SELECT COUNT(*) FROM releases WHERE created_by = $1", user_id)
                
                rows = await conn.fetch("""
                    SELECT * FROM releases WHERE created_by = $1 
                    ORDER BY release_date DESC LIMIT $2 OFFSET $3
                """, user_id, limit, offset)
            
            return rows, total

    # Методы для отчетов
    async def create_report(self, user_id, report_date, text):
        """Создает отчет пользователя."""
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO reports (user_id, report_date, text) VALUES ($1, $2, $3)", user_id, report_date, text)

    async def get_reports(self, user_id, limit=20):
        """Получает последние отчеты пользователя."""
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM reports WHERE user_id=$1 ORDER BY id DESC LIMIT $2", user_id, limit)

    # Методы для задач по расписанию
    async def get_overdue_tasks(self, today_str):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tasks WHERE deadline < $1 AND status != 'done'", today_str)

    async def mark_task_overdue(self, task_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tasks SET status='overdue' WHERE id=$1", task_id)

    async def get_deadline_tasks(self, date_str):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tasks WHERE deadline = $1 AND status != 'done'", date_str)
    
    async def get_unsigned_artists(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM artists WHERE flag_contract=0")

    async def update_artist_flag(self, artist_id, column):
        async with self.pool.acquire() as conn:
            # Внимание: имя колонки передается динамически, нужно быть осторожным.
            # Но здесь мы контролируем ввод из кода.
            # asyncpg не поддерживает динамические имена колонок в параметрах, поэтому f-string.
            await conn.execute(f"UPDATE artists SET {column}=1 WHERE id=$1", artist_id)
            
    async def get_artist_by_name(self, name):
         async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT id FROM artists WHERE name=$1", name)

    async def create_artist(self, name, manager_id, first_release_date):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO artists (name, manager_id, first_release_date) VALUES ($1, $2, $3) RETURNING id",
                name, manager_id, first_release_date
            )
            
    async def create_release(self, title, artist_id, r_type, release_date, created_by):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "INSERT INTO releases (title, artist_id, type, release_date, created_by) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                title, artist_id, r_type, release_date, created_by
            )
            
    async def get_designer(self):
        async with self.pool.acquire() as conn:
             return await conn.fetchrow("SELECT telegram_id FROM users WHERE role='designer'")

    async def get_history_founder(self, limit=20):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tasks WHERE status='done' ORDER BY deadline DESC LIMIT $1", limit)
            
    async def get_history_user(self, uid, limit=20):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM tasks WHERE status='done' AND assigned_to=$1 ORDER BY deadline DESC LIMIT $2", uid, limit)

    async def get_last_releases(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM releases ORDER BY release_date DESC LIMIT $1", limit)

# Создаем глобальный экземпляр БД
db = Database(DATABASE_URL)
