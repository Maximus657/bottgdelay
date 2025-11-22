import asyncio
import logging
import datetime
import os
import sys
import psycopg2
import aiohttp
from psycopg2.extras import DictCursor
from typing import List, Optional, Union, Dict, Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (ReplyKeyboardMarkup, KeyboardButton, 
                           InlineKeyboardMarkup, InlineKeyboardButton, 
                           CallbackQuery, ReplyKeyboardRemove, InputFile, FSInputFile)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ==============================================================================
# 0. КОНФИГУРАЦИЯ И ЛОГГИРОВАНИЕ
# ==============================================================================

API_TOKEN = os.getenv('API_TOKEN')

# Парсим ID админов
admin_ids_str = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = [int(x) for x in admin_ids_str.split(',')] if admin_ids_str else []

DATABASE_URL = os.getenv('DATABASE_URL')

YANDEX_DISK_TOKEN = os.getenv('YANDEX_DISK_TOKEN')
YANDEX_UPLOAD_FOLDER = "label_bot_files"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LabelBot")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

ROLES_MAP = {
    "👑 Основатель": "founder",
    "🎧 A&R Менеджер": "anr",
    "🎨 Дизайнер": "designer",
    "📱 SMM Специалист": "smm"
}
ROLES_DISPLAY = {v: k for k, v in ROLES_MAP.items()}

# ==============================================================================
# 1. СЕРВИС YANDEX DISK (ASYNC)
# ==============================================================================
class AsyncYandexDisk:
    def __init__(self, token, folder_name):
        self.token = token
        self.headers = {"Authorization": f"OAuth {token}"}
        self.folder_name = folder_name
        self.api_url = "https://cloud-api.yandex.net/v1/disk/resources"

    async def _ensure_folder(self, session):
        url = f"{self.api_url}?path={self.folder_name}"
        async with session.put(url, headers=self.headers) as resp:
            pass # Игнорируем ошибку, если папка уже есть

    async def upload_file(self, file_bytes, file_name):
        """
        Асинхронная загрузка файла.
        file_bytes: байты файла или поток (BytesIO)
        file_name: имя файла для сохранения
        """
        async with aiohttp.ClientSession() as session:
            try:
                # 1. Убедимся, что папка существует
                await self._ensure_folder(session)

                full_path = f"{self.folder_name}/{file_name}"
                
                # 2. Получаем ссылку для загрузки (GET request)
                upload_req_url = f"{self.api_url}/upload"
                params = {"path": full_path, "overwrite": "true"}
                
                async with session.get(upload_req_url, headers=self.headers, params=params) as resp:
                    if resp.status != 200:
                        logger.error(f"YD Get Link Error: {await resp.text()}")
                        return None
                    data = await resp.json()
                    upload_link = data.get('href')

                # 3. Загружаем сам файл (PUT request)
                async with session.put(upload_link, data=file_bytes) as upload_resp:
                    if upload_resp.status != 201:
                        logger.error(f"YD Upload Error: {upload_resp.status}")
                        return None

                # 4. Публикуем (делаем файл доступным)
                publish_url = f"{self.api_url}/publish"
                async with session.put(publish_url, headers=self.headers, params={"path": full_path}) as pub_resp:
                    pass 

                # 5. Получаем публичную ссылку
                async with session.get(self.api_url, headers=self.headers, params={"path": full_path}) as meta_resp:
                    if meta_resp.status == 200:
                        meta = await meta_resp.json()
                        return meta.get('public_url')
                    return None
            except Exception as e:
                logger.error(f"YD Exception: {e}")
                return None

ydisk = AsyncYandexDisk(YANDEX_DISK_TOKEN, YANDEX_UPLOAD_FOLDER)

# ==============================================================================
# 2. БАЗА ДАННЫХ (POSTGRESQL)
# ==============================================================================
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        self.init_db()

    def get_cursor(self):
        return self.conn.cursor(cursor_factory=DictCursor)

    def init_db(self):
        with self.get_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    name TEXT,
                    username TEXT,
                    role TEXT
                )
            """)
            # Добавляем колонку username, если её нет (миграция для старой базы)
            try:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
            except: 
                pass

            cur.execute("""
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS releases (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    artist_id INTEGER,
                    type TEXT,
                    release_date TEXT,
                    created_by BIGINT
                )
            """)
            cur.execute("""
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    report_date TEXT,
                    text TEXT
                )
            """)
        self._seed_admins()

    def _seed_admins(self):
        for uid in ADMIN_IDS:
            if not self.get_user(uid):
                # username не знаем, ставим None
                self.add_user(uid, "Founder", "founder", None)

    def get_user(self, uid):
        with self.get_cursor() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id=%s", (uid,))
            return cur.fetchone()
    
    def add_user(self, uid, name, role, username=None):
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO users (telegram_id, name, role, username) VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role, username = EXCLUDED.username
            """, (uid, name, role, username))

    def delete_user(self, uid):
        with self.get_cursor() as cur:
            cur.execute("DELETE FROM users WHERE telegram_id=%s", (uid,))

    def get_all_users(self):
        with self.get_cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY role")
            return cur.fetchall()
    
    def delete_release_cascade(self, release_id):
        with self.get_cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE release_id=%s", (release_id,))
            cur.execute("DELETE FROM releases WHERE id=%s", (release_id,))

    def delete_task(self, task_id):
        with self.get_cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))

    def get_user_link(self, uid):
        u = self.get_user(uid)
        if u: 
            if u.get('username'):
                return f"<a href='tg://user?id={uid}'>{u['name']}</a> (@{u['username']})"
            return f"<a href='tg://user?id={uid}'>{u['name']}</a>"
        return f"ID:{uid}"
    
    def create_task(self, title, desc, assigned, created, rel_id, deadline, req_file=0, parent_id=None):
        with self.get_cursor() as cur:
            cur.execute("""
                INSERT INTO tasks (title, description, assigned_to, created_by, release_id, deadline, requires_file, parent_task_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (title, desc, assigned, created, rel_id, deadline, req_file, parent_id))

    def get_tasks_active_founder(self):
        with self.get_cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE status NOT IN ('done', 'rejected') ORDER BY deadline")
            return cur.fetchall()

    def get_tasks_active_user(self, uid):
        with self.get_cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE assigned_to=%s AND status NOT IN ('done', 'rejected') ORDER BY deadline", (uid,))
            return cur.fetchall()

    def get_task_by_id(self, tid):
        with self.get_cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s", (tid,))
            return cur.fetchone()

    def update_task_status(self, tid, status, file_url=None, comment=None):
        with self.get_cursor() as cur:
            if file_url or comment:
                cur.execute("UPDATE tasks SET status=%s, file_url=%s, comment=%s WHERE id=%s", (status, file_url, comment, tid))
            else:
                cur.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, tid))

    # --- PAGING SUPPORT ---
    def get_releases_paginated(self, user_role, user_id, page=0, limit=5):
        offset = page * limit
        with self.get_cursor() as cur:
            if user_role == 'founder':
                # Считаем общее кол-во
                cur.execute("SELECT COUNT(*) FROM releases")
                total = cur.fetchone()[0]
                
                cur.execute("""
                    SELECT r.*, u.name as creator_name FROM releases r
                    LEFT JOIN users u ON r.created_by = u.telegram_id
                    ORDER BY r.release_date DESC LIMIT %s OFFSET %s
                """, (limit, offset))
            else:
                cur.execute("SELECT COUNT(*) FROM releases WHERE created_by = %s", (user_id,))
                total = cur.fetchone()[0]
                
                cur.execute("""
                    SELECT * FROM releases WHERE created_by = %s 
                    ORDER BY release_date DESC LIMIT %s OFFSET %s
                """, (user_id, limit, offset))
            
            return cur.fetchall(), total

db = Database(DATABASE_URL)

# ==============================================================================
# 3. FSM STATES
# ==============================================================================
class AddUser(StatesGroup): tg_id=State(); name=State(); role=State()
class CreateRelease(StatesGroup): artist_str=State(); title=State(); rtype=State(); has_cover=State(); date=State()
class CreateTask(StatesGroup): title=State(); desc=State(); assignee=State(); deadline=State(); req_file=State()
class FinishTask(StatesGroup): file=State(); comment=State()
class SMMReportState(StatesGroup): text=State()

# ==============================================================================
# 4. UTILS
# ==============================================================================
def get_cancel_kb(): 
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)

def get_main_kb(role):
    kb = []
    if role == 'founder':
        kb = [
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="➕ Добавить юзера")],
            [KeyboardButton(text="🗑 Удалить юзера"), KeyboardButton(text="💿 Все релизы")],
            [KeyboardButton(text="💿 Создать релиз"), KeyboardButton(text="➕ Создать задачу")],
            [KeyboardButton(text="📋 Активные задачи"), KeyboardButton(text="📜 История всех задач")]
        ]
    elif role == 'anr':
        kb = [
            [KeyboardButton(text="💿 Создать релиз"), KeyboardButton(text="💿 Мои релизы")],
            [KeyboardButton(text="➕ Создать задачу"), KeyboardButton(text="📋 Мои задачи")],
            [KeyboardButton(text="📜 История")]
        ]
    elif role == 'designer':
        kb = [[KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📜 История")], [KeyboardButton(text="🕰 Просроченные")]]
    elif role == 'smm':
        kb = [[KeyboardButton(text="📝 Написать отчет"), KeyboardButton(text="📅 Мои отчеты")],
              [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📜 История")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def notify_user(uid, text, reply_markup=None):
    try: await bot.send_message(uid, text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e: logger.warning(f"Failed to notify {uid}: {e}")

# ==============================================================================
# 5. HANDLERS
# ==============================================================================

# --- MIDDLEWARES ---
@dp.message.outer_middleware
async def auth_middleware(handler, event: types.Message, data):
    if event.text == "/start": return await handler(event, data)
    if event.from_user:
        user = db.get_user(event.from_user.id)
        if not user:
            await event.answer("⛔️ <b>Доступ запрещен.</b>\nОбратитесь к администратору.", parse_mode="HTML")
            return
    return await handler(event, data)

@dp.callback_query.outer_middleware
async def auth_middleware_callbacks(handler, event: types.CallbackQuery, data):
    if event.from_user:
        if not db.get_user(event.from_user.id):
            await event.answer("⛔️ Доступ запрещен.", show_alert=True)
            return
    return await handler(event, data)

# --- START & COMMON ---
@dp.message(F.text == "🔙 Отмена")
async def cancel_handler(m: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(m.from_user.id)
    await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    user = db.get_user(m.from_user.id)
    if not user: return await m.answer("⛔️ Вас нет в системе. Попросите администратора добавить ваш ID.")
    
    # Обновляем username если он изменился или не был задан
    if m.from_user.username:
        db.add_user(m.from_user.id, user['name'], user['role'], m.from_user.username)

    role_name = ROLES_DISPLAY.get(user['role'], user['role'])
    await m.answer(f"👋 Привет, <b>{user['name']}</b>!\nРоль: <code>{role_name}</code>", reply_markup=get_main_kb(user['role']), parse_mode="HTML")

# --- USERS ---
@dp.message(F.text == "👥 Пользователи")
async def list_users(m: types.Message):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    users = db.get_all_users()
    text = "👥 <b>Команда лейбла:</b>\n\n"
    for u in users:
        role_nice = ROLES_DISPLAY.get(u['role'], u['role'])
        un = f"(@{u['username']})" if u.get('username') else ""
        text += f"🔹 <a href='tg://user?id={u['telegram_id']}'>{u['name']}</a> {un} — <code>{role_nice}</code>\n"
    await m.answer(text, parse_mode="HTML")

@dp.message(F.text == "➕ Добавить юзера")
async def add_user_step1(m: types.Message, state: FSMContext):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    await m.answer("🆔 Введите <b>Telegram ID</b>:", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(AddUser.tg_id)

@dp.message(AddUser.tg_id)
async def add_user_step2(m: types.Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ ID должен быть числом.")
    await state.update_data(uid=m.text)
    await m.answer("👤 Введите <b>Имя сотрудника</b>:", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(AddUser.name)

@dp.message(AddUser.name)
async def add_user_step3(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text)
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👑 Основатель"), KeyboardButton(text="🎧 A&R Менеджер")],
        [KeyboardButton(text="🎨 Дизайнер"), KeyboardButton(text="📱 SMM Специалист")],
        [KeyboardButton(text="🔙 Отмена")]
    ], resize_keyboard=True)
    await m.answer("🎭 Выберите <b>Роль</b>:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(AddUser.role)

@dp.message(AddUser.role)
async def add_user_finish(m: types.Message, state: FSMContext):
    role_code = ROLES_MAP.get(m.text)
    if not role_code: return await m.answer("⚠️ Выберите роль кнопкой.")
    data = await state.get_data()
    # username пока Null, обновится при первом /start сотрудника
    db.add_user(int(data['uid']), data['name'], role_code)
    await m.answer(f"✅ <b>{data['name']}</b> добавлен!", reply_markup=get_main_kb('founder'), parse_mode="HTML")
    await notify_user(int(data['uid']), f"🎉 <b>Добро пожаловать!</b>\nРоль: {m.text}\nНажмите /start для начала работы.")
    await state.clear()

@dp.message(F.text == "🗑 Удалить юзера")
async def delete_user_start(m: types.Message):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE role != 'founder'")
        users = cur.fetchall()
    if not users: return await m.answer("Удалять некого.")
    kb = InlineKeyboardBuilder()
    for u in users: kb.button(text=f"❌ {u['name']}", callback_data=f"rm_usr_{u['telegram_id']}")
    kb.adjust(1)
    await m.answer("Кого удалить?", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("rm_usr_"))
async def delete_user_confirm(c: CallbackQuery):
    uid = int(c.data.split("_")[2])
    db.delete_user(uid)
    await c.message.edit_text("🗑 Пользователь удален.")

# --- RELEASES ---
@dp.message(F.text == "💿 Создать релиз")
async def create_release_start(m: types.Message, state: FSMContext):
    if db.get_user(m.from_user.id)['role'] not in ['founder', 'anr']: return
    await m.answer("🎤 <b>Артист(ы):</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateRelease.artist_str)

@dp.message(CreateRelease.artist_str)
async def create_release_title(m: types.Message, state: FSMContext):
    await state.update_data(artist=m.text)
    await m.answer("💿 <b>Название релиза:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateRelease.title)

@dp.message(CreateRelease.title)
async def create_release_type(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Сингл"), KeyboardButton(text="Альбом")], [KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)
    await m.answer("📼 <b>Тип:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateRelease.rtype)

@dp.message(CreateRelease.rtype)
async def create_release_cover(m: types.Message, state: FSMContext):
    await state.update_data(type=m.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Есть"), KeyboardButton(text="❌ Нужно сделать")], [KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)
    await m.answer("🎨 <b>Обложка готова?</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateRelease.has_cover)

@dp.message(CreateRelease.has_cover)
async def create_release_date(m: types.Message, state: FSMContext):
    need_cover = True if m.text == "❌ Нужно сделать" else False
    await state.update_data(need_cover=need_cover)
    await m.answer("📅 <b>Дата (YYYY-MM-DD):</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateRelease.date)

@dp.message(CreateRelease.date)
async def create_release_finish(m: types.Message, state: FSMContext):
    try:
        clean_date = m.text.replace(".", "-").replace("/", "-")
        datetime.datetime.strptime(clean_date, "%Y-%m-%d")
    except: return await m.answer("⛔️ Формат: YYYY-MM-DD")

    data = await state.get_data()
    manager_id = m.from_user.id
    
    with db.get_cursor() as cur:
        # Проверяем или создаем артиста
        cur.execute("SELECT id FROM artists WHERE name=%s", (data['artist'],))
        artist = cur.fetchone()
        if not artist:
            cur.execute("INSERT INTO artists (name, manager_id, first_release_date) VALUES (%s, %s, %s) RETURNING id", 
                        (data['artist'], manager_id, clean_date))
            artist_id = cur.fetchone()[0]
        else: artist_id = artist['id']
        
        # Создаем релиз
        cur.execute("INSERT INTO releases (title, artist_id, type, release_date, created_by) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (data['title'], artist_id, data['type'], clean_date, manager_id))
        rel_id = cur.fetchone()[0]
    
    await generate_release_tasks(rel_id, data['title'], clean_date, manager_id, data['artist'], data['need_cover'])
    await m.answer(f"🚀 <b>Релиз создан!</b>\n🎶 {data['artist']} — {data['title']}", reply_markup=get_main_kb(db.get_user(manager_id)['role']), parse_mode="HTML")
    await state.clear()

async def generate_release_tasks(rel_id, title, r_date, manager_id, artist_name, need_cover):
    # Поиск дизайнера
    with db.get_cursor() as cur:
        cur.execute("SELECT telegram_id FROM users WHERE role='designer'")
        designer = cur.fetchone()
    
    if designer:
        designer_id = designer['telegram_id']
        designer_note = ""
    else:
        designer_id = manager_id
        designer_note = " (Fallback: нет дизайнера)"

    tasks = []
    if need_cover: tasks.append(("🎨 Обложка", f"Сделать обложку: {artist_name} - {title}{designer_note}", designer_id, 14, 1))
    tasks.append(("📤 Дистрибуция", f"Загрузить трек: {artist_name} - {title}", manager_id, 10, 0))
    tasks.append(("📝 Питчинг", f"Форма питчинга: {artist_name} - {title}", manager_id, 7, 0))
    tasks.append(("📱 Сниппет", f"Видео-сниппет: {artist_name} - {title}{designer_note}", designer_id, 3, 1))
    
    r_dt = datetime.datetime.strptime(r_date, "%Y-%m-%d")
    for t_name, t_desc, assignee, days, req in tasks:
        dl = (r_dt - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        db.create_task(f"{t_name} | {artist_name}", t_desc, assignee, manager_id, rel_id, dl, req)

# --- RELEASES LIST (PAGINATION) ---
@dp.message(F.text.in_({"💿 Релизы", "💿 Все релизы", "💿 Мои релизы"}))
async def list_releases_handler(m: types.Message):
    await show_releases_page(m, 0)

async def show_releases_page(message_or_call, page):
    # Определяем ID пользователя и метод ответа
    if isinstance(message_or_call, types.Message):
        uid = message_or_call.from_user.id
        reply_func = message_or_call.answer
    else:
        uid = message_or_call.from_user.id
        reply_func = message_or_call.message.edit_text

    user = db.get_user(uid)
    if user['role'] not in ['founder', 'anr']: return

    rels, total_count = db.get_releases_paginated(user['role'], uid, page=page, limit=5)
    
    header = "💿 <b>Все релизы:</b>" if user['role'] == 'founder' else "💿 <b>Ваши релизы:</b>"
    
    if not rels:
        text = f"{header}\n📭 Список пуст."
        kb = None
    else:
        text = f"{header} (Всего: {total_count})\n\n"
        for r in rels:
            c_info = f"👤 От: {r['creator_name']}\n" if user['role'] == 'founder' and 'creator_name' in r else ""
            text += f"🎶 <b>{r['title']}</b> ({r['type']})\n📅 {r['release_date']}\n{c_info}🆔 ID: <code>{r['id']}</code>\n➖➖➖➖➖➖\n"
        
        # Кнопки пагинации
        kb_build = InlineKeyboardBuilder()
        if page > 0:
            kb_build.button(text="⬅️ Назад", callback_data=f"relpage_{page-1}")
        
        if (page + 1) * 5 < total_count:
            kb_build.button(text="Вперед ➡️", callback_data=f"relpage_{page+1}")
        
        kb = kb_build.as_markup()

    if isinstance(message_or_call, types.CallbackQuery):
        await reply_func(text, reply_markup=kb, parse_mode="HTML")
    else:
        await reply_func(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("relpage_"))
async def releases_page_callback(c: CallbackQuery):
    page = int(c.data.split("_")[1])
    await show_releases_page(c, page)

@dp.message(F.text == "🗑 Удалить релиз")
async def delete_rel_start(m: types.Message):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM releases ORDER BY release_date DESC LIMIT 10") # Only last 10 for simplicity in deletion
        rels = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rels: kb.button(text=f"❌ {r['title']}", callback_data=f"del_rel_{r['id']}")
    kb.adjust(1)
    await m.answer("Выберите релиз для удаления (показаны последние 10):", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("del_rel_"))
async def delete_rel_confirm(c: CallbackQuery):
    rid = int(c.data.split("_")[2])
    db.delete_release_cascade(rid)
    await c.message.edit_text("🗑 Релиз и задачи удалены.")

# --- TASKS ---
@dp.message(F.text == "➕ Создать задачу")
async def manual_task_start(m: types.Message, state: FSMContext):
    await m.answer("📝 <b>Заголовок задачи:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateTask.title)

@dp.message(CreateTask.title)
async def manual_task_desc(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text)
    await m.answer("📝 <b>Описание задачи:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateTask.desc)

@dp.message(CreateTask.desc)
async def manual_task_assign(m: types.Message, state: FSMContext):
    await state.update_data(desc=m.text)
    users = db.get_all_users()
    kb = InlineKeyboardBuilder()
    for u in users: 
        r = ROLES_DISPLAY.get(u['role'], u['role'])
        # Добавляем имя и роль
        kb.button(text=f"{u['name']} ({r})", callback_data=f"assign_{u['telegram_id']}")
    kb.adjust(2)
    await m.answer("👤 <b>Исполнитель:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.set_state(CreateTask.assignee)

@dp.callback_query(CreateTask.assignee)
async def manual_task_deadline(c: CallbackQuery, state: FSMContext):
    await state.update_data(assignee=int(c.data.split("_")[1]))
    await c.message.answer("📅 <b>Дедлайн (YYYY-MM-DD):</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateTask.deadline)

@dp.message(CreateTask.deadline)
async def manual_task_req(m: types.Message, state: FSMContext):
    try:
        cl = m.text.replace(".", "-").replace("/", "-")
        datetime.datetime.strptime(cl, "%Y-%m-%d")
        await state.update_data(deadline=cl)
    except: return await m.answer("⛔️ Формат: YYYY-MM-DD")
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")], [KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)
    await m.answer("📎 <b>Нужен файл при сдаче?</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateTask.req_file)

@dp.message(CreateTask.req_file)
async def manual_task_fin(m: types.Message, state: FSMContext):
    req = 1 if m.text == "Да" else 0
    d = await state.get_data()
    db.create_task(d['title'], d['desc'], d['assignee'], m.from_user.id, None, d['deadline'], req)
    
    creator_link = db.get_user_link(m.from_user.id)
    msg = f"🔔 <b>НОВАЯ ЗАДАЧА</b>\n📌 {d['title']}\n📄 {d['desc']}\n🗓 {d['deadline']}\n👤 От: {creator_link}"
    await notify_user(d['assignee'], msg)
    await m.answer("✅ Задача назначена!", reply_markup=get_main_kb(db.get_user(m.from_user.id)['role']))
    await state.clear()

@dp.message(F.text.in_({"📋 Активные задачи", "📋 Мои задачи"}))
async def view_tasks(m: types.Message):
    uid = m.from_user.id
    user = db.get_user(uid)
    
    if user['role'] == 'founder' and "Активные" in m.text:
        tasks = db.get_tasks_active_founder()
        header = "📋 <b>Все активные задачи:</b>"
    else:
        tasks = db.get_tasks_active_user(uid)
        header = "📋 <b>Ваши задачи:</b>"
        
    if not tasks: return await m.answer("🎉 Задач нет!")
    
    await m.answer(header, parse_mode="HTML")
    
    for t in tasks:
        icon = "🔥" if t['status'] == 'overdue' else "⏳"
        creator = db.get_user_link(t['created_by'])
        txt = f"{icon} <b>{t['title']}</b>\n━━━━━━━━━━━━━━━━\n📄 {t['description']}\n\n🗓 <code>{t['deadline']}</code>\n👤 От: {creator}"
        
        kb = InlineKeyboardBuilder()
        if t['assigned_to'] == uid:
            kb.button(text="✅ Выполнить", callback_data=f"fin_{t['id']}")
            kb.button(text="⛔️ Отказаться", callback_data=f"rej_{t['id']}")
        if user['role'] == 'founder':
            kb.button(text="🗑 Удалить", callback_data=f"admdel_{t['id']}")
        kb.adjust(2)    
        await m.answer(txt, reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("admdel_"))
async def admin_del_task_ask(c: CallbackQuery):
    tid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"confdel_{tid}")
    kb.button(text="Отмена", callback_data="ignore_cb")
    await c.message.edit_text("⚠️ <b>Удалить задачу?</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("confdel_"))
async def admin_del_task_confirm(c: CallbackQuery):
    tid = int(c.data.split("_")[1])
    task = db.get_task_by_id(tid)
    if task:
        await notify_user(task['assigned_to'], f"🗑 <b>Задача аннулирована:</b>\n{task['title']}")
        db.delete_task(tid)
        await c.message.edit_text("🗑 Удалена.")
    else: await c.answer("Уже удалена.")

@dp.callback_query(F.data.startswith("rej_"))
async def reject_ask(c: CallbackQuery):
    tid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отказаться", callback_data=f"confrej_{tid}")
    kb.button(text="Вернуться", callback_data="ignore_cb")
    await c.message.edit_text("⚠️ <b>Отказаться?</b>\nАдминистраторы получат уведомление.", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("confrej_"))
async def reject_confirm(c: CallbackQuery):
    tid = int(c.data.split("_")[1])
    task = db.get_task_by_id(tid)
    if task:
        db.update_task_status(tid, 'rejected')
        rejector = db.get_user_link(c.from_user.id)
        alert = f"⛔️ <b>ОТКАЗ:</b> {task['title']}\n👤 {rejector}"
        for admin_id in ADMIN_IDS: await notify_user(admin_id, alert)
        await c.message.edit_text("❌ Отказано.")
    else: await c.answer("Ошибка")

@dp.callback_query(F.data == "ignore_cb")
async def ignore_cb(c: CallbackQuery): await c.message.delete()

# --- HISTORY ---
@dp.message(F.text.in_({"📜 История всех задач", "📜 История"}))
async def history(m: types.Message):
    uid = m.from_user.id
    role = db.get_user(uid)['role']
    
    with db.get_cursor() as cur:
        if role == 'founder':
            cur.execute("SELECT * FROM tasks WHERE status='done' ORDER BY deadline DESC LIMIT 20")
            header = "📜 <b>Глобальная история (последние 20):</b>"
        else:
            cur.execute("SELECT * FROM tasks WHERE status='done' AND assigned_to=%s ORDER BY deadline DESC LIMIT 20", (uid,))
            header = "📜 <b>Ваша история:</b>"
        tasks = cur.fetchall()
        
    if not tasks: return await m.answer("📭 Пусто.")
    txt = f"{header}\n\n"
    for t in tasks:
        user_link = db.get_user_link(t['assigned_to'])
        txt += f"✅ <b>{t['title']}</b>\n👤 {user_link}\n🗓 {t['deadline']}\n"
        if t['file_url']: 
            txt += "📎 Файл (TG)\n" if "tg:" in t['file_url'] else f"💾 <a href='{t['file_url']}'>Файл (Диск)</a>\n"
        txt += "━━━━━━━━━━━━━━━━\n"
    await m.answer(txt, parse_mode="HTML", disable_web_page_preview=True)

# --- FINISH & UPLOAD (UPDATED ASYNC) ---
@dp.callback_query(F.data.startswith("fin_"))
async def fin_start(c: CallbackQuery, state: FSMContext):
    tid = int(c.data.split("_")[1])
    task = db.get_task_by_id(tid)
    if not task or task['status'] == 'done': return await c.answer("Уже выполнено.")
    
    await state.update_data(tid=tid, creator=task['created_by'], title=task['title'])
    if task['requires_file']:
        await c.message.answer("📎 <b>Пришлите файл/фото:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
        await state.set_state(FinishTask.file)
    else:
        await c.message.answer("💬 <b>Комментарий:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
        await state.set_state(FinishTask.comment)

@dp.message(FinishTask.file)
async def fin_file(m: types.Message, state: FSMContext):
    if m.text == "🔙 Отмена": return await cancel_handler(m, state)
    if not (m.document or m.photo): return await m.answer("📎 Жду файл (Документ или Фото).")
    
    msg = await m.answer("⏳ Загрузка... (0%)")
    
    # Определяем ID и имя файла
    if m.document: 
        fid = m.document.file_id
        fname = m.document.file_name or f"file_{fid}"
        ftype = "doc"
    else: 
        fid = m.photo[-1].file_id
        fname = f"photo_{fid}.jpg"
        ftype = "photo"

    pub_url = None
    try:
        f_info = await bot.get_file(fid)
        # Если файл меньше 200МБ (лимит бота), пробуем грузить
        # ВНИМАНИЕ: Для очень больших файлов нужен Local Bot API, но для облака пойдет
        
        # Скачиваем файл в поток (BytesIO)
        import io
        file_stream = io.BytesIO()
        await bot.download_file(f_info.file_path, destination=file_stream)
        file_stream.seek(0) # Сброс указателя в начало

        await msg.edit_text("⏳ Загрузка... (Отправка на Яндекс)")
        # Асинхронная загрузка
        pub_url = await ydisk.upload_file(file_stream, fname)
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await msg.edit_text(f"⚠️ Ошибка загрузки: {e}")

    if pub_url:
        await msg.edit_text("✅ Загружено на Диск!")
        await state.update_data(f_val=pub_url)
    else:
        await msg.edit_text("⚠️ Не удалось загрузить на Диск. Сохранена ссылка на TG.")
        await state.update_data(f_val=f"tg:{ftype}:{fid}")
    
    await m.answer("💬 Комментарий к задаче:", reply_markup=get_cancel_kb())
    await state.set_state(FinishTask.comment)

@dp.message(FinishTask.comment)
async def fin_commit(m: types.Message, state: FSMContext):
    if m.text == "🔙 Отмена": return await cancel_handler(m, state)
    d = await state.get_data()
    db.update_task_status(d['tid'], 'done', d.get('f_val'), m.text)
    
    perf = db.get_user_link(m.from_user.id)
    txt = f"✅ <b>Выполнено!</b>\n📌 {d['title']}\n👤 {perf}\n💬 {m.text}"
    
    try:
        # Уведомляем создателя
        if d.get('f_val') and "tg:" in d['f_val']:
            txt += "\n📎 Файл ниже"
            await notify_user(d['creator'], txt)
            _, type_, fid = d['f_val'].split(":", 2)
            if type_ == "photo": await bot.send_photo(d['creator'], fid)
            else: await bot.send_document(d['creator'], fid)
        elif d.get('f_val'):
            txt += f"\n💾 <a href='{d['f_val']}'>Файл (Диск)</a>"
            await notify_user(d['creator'], txt)
        else:
            await notify_user(d['creator'], txt)
    except: pass

    await m.answer("👍 Готово.", reply_markup=get_main_kb(db.get_user(m.from_user.id)['role']))
    await state.clear()

# --- SMM & CRON ---
@dp.message(F.text == "📝 Написать отчет")
async def smm_start(m: types.Message, state: FSMContext):
    await m.answer("✍️ Текст:", reply_markup=get_cancel_kb())
    await state.set_state(SMMReportState.text)

@dp.message(SMMReportState.text)
async def smm_save(m: types.Message, state: FSMContext):
    if m.text == "🔙 Отмена": return await cancel_handler(m, state)
    
    with db.get_cursor() as cur:
        cur.execute("INSERT INTO reports (user_id, report_date, text) VALUES (%s, %s, %s)", 
                    (m.from_user.id, datetime.date.today(), m.text))
    
    reporter = db.get_user_link(m.from_user.id)
    report_msg = (
        f"📊 <b>НОВЫЙ SMM ОТЧЕТ</b>\n"
        f"👤 От: {reporter}\n"
        f"📅 Дата: {datetime.date.today()}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{m.text}"
    )
    for admin_id in ADMIN_IDS:
        await notify_user(admin_id, report_msg)

    await m.answer("✅ Отчет сохранен и отправлен руководству.", reply_markup=get_main_kb('smm'))
    await state.clear()

@dp.message(F.text == "📅 Мои отчеты")
async def smm_list(m: types.Message):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM reports WHERE user_id=%s ORDER BY id DESC LIMIT 20", (m.from_user.id,))
        reps = cur.fetchall()
    await m.answer("\n".join([f"📅 <b>{r['report_date']}</b>: {r['text']}" for r in reps]) if reps else "Пусто.", parse_mode="HTML")

async def job_check_overdue():
    today = datetime.date.today().strftime("%Y-%m-%d")
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM tasks WHERE deadline < %s AND status != 'done'", (today,))
        tasks = cur.fetchall()
        for t in tasks:
            if t['status'] != 'overdue':
                cur.execute("UPDATE tasks SET status='overdue' WHERE id=%s", (t['id'],))
            await notify_user(t['assigned_to'], f"⚠️ <b>ПРОСРОЧЕНО!</b>\n📌 {t['title']}")

async def job_deadline_alerts():
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM tasks WHERE deadline = %s AND status != 'done'", (tomorrow,))
        for t in cur.fetchall(): await notify_user(t['assigned_to'], f"⏰ <b>Дедлайн < 24ч!</b>\n📌 {t['title']}")

async def job_onboarding():
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM artists WHERE flag_contract=0")
        for a in cur.fetchall():
            kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_cont_{a['id']}").button(text="Позже", callback_data="ign")
            await notify_user(a['manager_id'], f"📝 Контракт с <b>{a['name']}</b> подписан?", kb.as_markup())

@dp.callback_query(F.data.startswith("onb_"))
async def onb_act(c: CallbackQuery):
    col = {'cont': 'flag_contract'}.get(c.data.split("_")[1])
    if col:
        with db.get_cursor() as cur: cur.execute(f"UPDATE artists SET {col}=1 WHERE id=%s", (int(c.data.split("_")[2]),))
        await c.message.edit_text("✅ Обновлено!")

@dp.callback_query(F.data == "ign")
async def ign(c: CallbackQuery): await c.message.delete()

async def main():
    # Запускаем задачи
    scheduler.add_job(job_check_overdue, CronTrigger(minute=0)) # Раз в час проверять просрочку
    scheduler.add_job(job_deadline_alerts, CronTrigger(hour='10,18')) # Уведомления утром и вечером
    scheduler.add_job(job_onboarding, CronTrigger(hour=15))
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    print("BOT STARTED (ASYNC V2)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass