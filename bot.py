import asyncio
import logging
import sqlite3
import datetime
import os
import requests
import sys
from typing import List, Optional, Union

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
# 0. КОНФИГУРАЦИЯ И НАСТРОЙКИ (ОБНОВЛЕНО ДЛЯ DOCKER)
# ==============================================================================

API_TOKEN = '8524498099:AAHTXkBHz3KDS-ux820VLjQP3N1vjKbBPtw'
ADMIN_IDS = [883119315, 424647161] 

# --- НАСТРОЙКА ПУТИ К БАЗЕ ДЛЯ DOCKER ---
# Мы используем папку "data", которую в Dokploy подключили через Bind Mount (/app/data)
DATA_DIR = "data"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# Полный путь к файлу базы данных
DB_NAME = os.path.join(DATA_DIR, "label_system_pro.db")

# Яндекс.Диск
YANDEX_DISK_TOKEN = "y0__xD1sf2lqveAAhi1rjsg_bvwghVVrb4S_mJF7NDv90XWdC0AbRPkyQ"
YANDEX_API_URL = "https://cloud-api.yandex.net/v1/disk/resources"
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
# 1. МОДУЛЬ РАБОТЫ С ЯНДЕКС.ДИСКОМ
# ==============================================================================
class YandexDiskService:
    def __init__(self, token, folder_name):
        self.token = token
        self.headers = {"Authorization": f"OAuth {token}"}
        self.folder_name = folder_name
        self._ensure_folder_exists()

    def _ensure_folder_exists(self):
        url = f"{YANDEX_API_URL}?path={self.folder_name}"
        try: requests.put(url, headers=self.headers)
        except: pass

    def upload_and_publish(self, file_bytes, file_name):
        try:
            full_path = f"{self.folder_name}/{file_name}"
            upload_req_url = f"{YANDEX_API_URL}/upload?path={full_path}&overwrite=true"
            res_url = requests.get(upload_req_url, headers=self.headers)
            if res_url.status_code != 200: return None
            
            upload_link = res_url.json().get('href')
            res_upload = requests.put(upload_link, files={'file': file_bytes})
            if res_upload.status_code != 201: return None
            
            requests.put(f"{YANDEX_API_URL}/publish?path={full_path}", headers=self.headers)
            res_meta = requests.get(f"{YANDEX_API_URL}?path={full_path}", headers=self.headers)
            
            if res_meta.status_code == 200:
                return res_meta.json().get('public_url')
            return None
        except Exception as e:
            logger.error(f"YD Error: {e}")
            return None

ydisk = YandexDiskService(YANDEX_DISK_TOKEN, YANDEX_UPLOAD_FOLDER)

# ==============================================================================
# 2. МОДУЛЬ БАЗЫ ДАННЫХ
# ==============================================================================
class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row 
        self.cursor = self.conn.cursor()
        self._init_tables()

    def _init_tables(self):
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY, name TEXT, role TEXT)""")
        
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, manager_id INTEGER, first_release_date TEXT,
            flag_contract INTEGER DEFAULT 0, flag_mm_profile INTEGER DEFAULT 0,
            flag_mm_verify INTEGER DEFAULT 0, flag_yt_note INTEGER DEFAULT 0, flag_yt_link INTEGER DEFAULT 0
        )""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, artist_id INTEGER, type TEXT, release_date TEXT, created_by INTEGER
        )""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, assigned_to INTEGER, created_by INTEGER,
            release_id INTEGER, parent_task_id INTEGER, deadline TEXT, status TEXT DEFAULT 'pending',
            requires_file INTEGER DEFAULT 0, file_url TEXT, comment TEXT
        )""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, report_date TEXT, text TEXT
        )""")
        self.conn.commit()
        self._seed_admins()

    def _seed_admins(self):
        for uid in ADMIN_IDS:
            if not self.get_user(uid):
                self.add_user(uid, "Founder", "founder")

    def get_user(self, uid): return self.cursor.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    def add_user(self, uid, name, role): 
        self.cursor.execute("INSERT OR REPLACE INTO users (telegram_id, name, role) VALUES (?,?,?)", (uid, name, role))
        self.conn.commit()
    def delete_user(self, uid):
        self.cursor.execute("DELETE FROM users WHERE telegram_id=?", (uid,))
        self.conn.commit()
    def get_all_users(self): return self.cursor.execute("SELECT * FROM users ORDER BY role").fetchall()
    
    def delete_release_cascade(self, release_id):
        self.cursor.execute("DELETE FROM tasks WHERE release_id=?", (release_id,))
        self.cursor.execute("DELETE FROM releases WHERE id=?", (release_id,))
        self.conn.commit()

    def delete_task(self, task_id):
        self.cursor.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.conn.commit()

    def get_user_link(self, uid):
        u = self.get_user(uid)
        if u: return f"<a href='tg://user?id={uid}'>{u['name']}</a>"
        return f"ID:{uid}"

db = Database(DB_NAME)

# ==============================================================================
# 3. FSM STATES
# ==============================================================================
class AddUser(StatesGroup): tg_id=State(); name=State(); role=State()
class CreateRelease(StatesGroup): artist_str=State(); title=State(); rtype=State(); has_cover=State(); date=State()
class CreateTask(StatesGroup): title=State(); desc=State(); assignee=State(); deadline=State(); req_file=State()
class FinishTask(StatesGroup): file=State(); comment=State()
class SMMReportState(StatesGroup): text=State()

# ==============================================================================
# 4. КЛАВИАТУРЫ И УТИЛИТЫ
# ==============================================================================
def get_cancel_kb(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)

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
    except: pass

# ==============================================================================
# 5. ХЕНДЛЕРЫ: ОБЩИЕ
# ==============================================================================
@dp.message.outer_middleware
async def auth_middleware(handler, event: types.Message, data):
    if event.text == "/start": return await handler(event, data)
    if event.from_user:
        user = db.get_user(event.from_user.id)
        if not user:
            await event.answer("⛔️ <b>Доступ запрещен.</b>\nВашего ID нет в системе.", parse_mode="HTML")
            return
    return await handler(event, data)

@dp.callback_query.outer_middleware
async def auth_middleware_callbacks(handler, event: types.CallbackQuery, data):
    if event.from_user:
        if not db.get_user(event.from_user.id):
            await event.answer("⛔️ Доступ запрещен.", show_alert=True)
            return
    return await handler(event, data)

@dp.message(F.text == "🔙 Отмена")
async def cancel_handler(m: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(m.from_user.id)
    await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    user = db.get_user(m.from_user.id)
    if not user: return await m.answer("⛔️ Вас нет в системе.")
    role_name = ROLES_DISPLAY.get(user['role'], user['role'])
    await m.answer(f"👋 Привет, <b>{user['name']}</b>!\nРоль: <code>{role_name}</code>", reply_markup=get_main_kb(user['role']), parse_mode="HTML")

# ==============================================================================
# 6. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ==============================================================================
@dp.message(F.text == "👥 Пользователи")
async def list_users(m: types.Message):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    users = db.get_all_users()
    text = "👥 <b>Команда лейбла:</b>\n\n"
    for u in users:
        role_nice = ROLES_DISPLAY.get(u['role'], u['role'])
        text += f"🔹 <a href='tg://user?id={u['telegram_id']}'>{u['name']}</a> — <code>{role_nice}</code>\n"
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
    db.add_user(data['uid'], data['name'], role_code)
    await m.answer(f"✅ <b>{data['name']}</b> добавлен!", reply_markup=get_main_kb('founder'), parse_mode="HTML")
    await notify_user(data['uid'], f"🎉 <b>Добро пожаловать!</b>\nРоль: {m.text}\nНажмите /start")
    await state.clear()

@dp.message(F.text == "🗑 Удалить юзера")
async def delete_user_start(m: types.Message):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    users = db.cursor.execute("SELECT * FROM users WHERE role != 'founder'").fetchall()
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

# ==============================================================================
# 7. РЕЛИЗЫ
# ==============================================================================
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
    
    artist = db.cursor.execute("SELECT id FROM artists WHERE name=?", (data['artist'],)).fetchone()
    if not artist:
        db.cursor.execute("INSERT INTO artists (name, manager_id, first_release_date) VALUES (?,?,?)", (data['artist'], manager_id, clean_date))
        artist_id = db.cursor.lastrowid
    else: artist_id = artist['id']
    
    db.cursor.execute("INSERT INTO releases (title, artist_id, type, release_date, created_by) VALUES (?,?,?,?,?)",
                      (data['title'], artist_id, data['type'], clean_date, manager_id))
    rel_id = db.cursor.lastrowid
    db.conn.commit()
    
    await generate_release_tasks(rel_id, data['title'], clean_date, manager_id, data['artist'], data['need_cover'])
    
    await m.answer(f"🚀 <b>Релиз создан!</b>\n🎶 {data['artist']} — {data['title']}", reply_markup=get_main_kb(db.get_user(manager_id)['role']), parse_mode="HTML")
    await state.clear()

async def generate_release_tasks(rel_id, title, r_date, manager_id, artist_name, need_cover):
    designer = db.conn.execute("SELECT telegram_id FROM users WHERE role='designer'").fetchone()
    
    if designer:
        designer_id = designer['telegram_id']
        designer_note = ""
    else:
        designer_id = manager_id
        designer_note = " (Fallback: нет дизайнера)"

    tasks = []
    if need_cover: 
        tasks.append(("🎨 Обложка", f"Сделать обложку: {artist_name} - {title}{designer_note}", designer_id, 14, 1))
        
    tasks.append(("📤 Дистрибуция", f"Загрузить трек: {artist_name} - {title}", manager_id, 10, 0))
    tasks.append(("📝 Питчинг", f"Форма питчинга: {artist_name} - {title}", manager_id, 7, 0))
    tasks.append(("📱 Сниппет", f"Видео-сниппет: {artist_name} - {title}{designer_note}", designer_id, 3, 1))
    
    r_dt = datetime.datetime.strptime(r_date, "%Y-%m-%d")
    for t_name, t_desc, assignee, days, req in tasks:
        dl = (r_dt - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        create_task_in_db(f"{t_name} | {artist_name}", t_desc, assignee, manager_id, rel_id, dl, req)

# СПИСОК РЕЛИЗОВ (ИНДИВИДУАЛЬНЫЙ / ОБЩИЙ)
@dp.message(F.text.in_({"💿 Релизы", "💿 Все релизы", "💿 Мои релизы"}))
async def list_releases(m: types.Message):
    uid = m.from_user.id
    user = db.get_user(uid)

    if user['role'] not in ['founder', 'anr']: return

    if user['role'] == 'founder':
        sql = """
            SELECT r.*, u.name as creator_name
            FROM releases r
            LEFT JOIN users u ON r.created_by = u.telegram_id
            ORDER BY r.release_date DESC LIMIT 20
        """
        rels = db.cursor.execute(sql).fetchall()
        header = "💿 <b>Все релизы лейбла:</b>\n\n"
    else:
        sql = "SELECT * FROM releases WHERE created_by = ? ORDER BY release_date DESC LIMIT 20"
        rels = db.cursor.execute(sql, (uid,)).fetchall()
        header = "💿 <b>Ваши релизы:</b>\n\n"
    
    if not rels: return await m.answer("📭 Список пуст.")
    
    text = header
    for r in rels:
        creator_info = ""
        if user['role'] == 'founder':
            c_name = r['creator_name'] if 'creator_name' in r.keys() and r['creator_name'] else "Удален"
            creator_info = f"👤 От: {c_name}\n"

        text += (
            f"🎶 <b>{r['title']}</b> ({r['type']})\n"
            f"📅 {r['release_date']}\n"
            f"{creator_info}"
            f"🆔 ID: <code>{r['id']}</code>\n"
            f"➖➖➖➖➖➖\n"
        )
    await m.answer(text, parse_mode="HTML")

@dp.message(F.text == "🗑 Удалить релиз")
async def delete_rel_start(m: types.Message):
    if db.get_user(m.from_user.id)['role'] != 'founder': return
    rels = db.cursor.execute("SELECT * FROM releases ORDER BY release_date DESC").fetchall()
    kb = InlineKeyboardBuilder()
    for r in rels: kb.button(text=f"❌ {r['title']}", callback_data=f"del_rel_{r['id']}")
    kb.adjust(1)
    await m.answer("Выберите релиз для удаления:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("del_rel_"))
async def delete_rel_confirm(c: CallbackQuery):
    rid = int(c.data.split("_")[2])
    db.delete_release_cascade(rid)
    await c.message.edit_text("🗑 Релиз и задачи удалены.")

# ==============================================================================
# 8. УПРАВЛЕНИЕ ЗАДАЧАМИ
# ==============================================================================
def create_task_in_db(title, desc, assigned, created, rel_id, deadline, req_file=0, parent_id=None):
    db.cursor.execute("""INSERT INTO tasks (title, description, assigned_to, created_by, release_id, deadline, requires_file, parent_task_id)
        VALUES (?,?,?,?,?,?,?,?)""", (title, desc, assigned, created, rel_id, deadline, req_file, parent_id))
    db.conn.commit()
    
    creator_name = db.get_user(created)['name']
    msg = (
        f"🔔 <b>НОВАЯ ЗАДАЧА</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Задача:</b> {title}\n\n"
        f"📄 <b>Описание:</b>\n{desc}\n\n"
        f"🗓 <b>Дедлайн:</b> <code>{deadline}</code>\n"
        f"👤 <b>От кого:</b> {creator_name}"
    )
    asyncio.create_task(notify_user(assigned, msg))

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
    create_task_in_db(d['title'], d['desc'], d['assignee'], m.from_user.id, None, d['deadline'], req)
    await m.answer("✅ Задача назначена!", reply_markup=get_main_kb(db.get_user(m.from_user.id)['role']))
    await state.clear()

@dp.message(F.text.in_({"📋 Активные задачи", "📋 Мои задачи"}))
async def view_tasks(m: types.Message):
    uid = m.from_user.id
    user = db.get_user(uid)
    
    if user['role'] == 'founder' and "Активные" in m.text:
        tasks = db.cursor.execute("SELECT * FROM tasks WHERE status NOT IN ('done', 'rejected') ORDER BY deadline").fetchall()
        header = "📋 <b>Все активные задачи:</b>"
    else:
        tasks = db.cursor.execute("SELECT * FROM tasks WHERE assigned_to=? AND status NOT IN ('done', 'rejected') ORDER BY deadline", (uid,)).fetchall()
        header = "📋 <b>Ваши задачи:</b>"
        
    if not tasks: return await m.answer("🎉 Задач нет!")
    
    await m.answer(header, parse_mode="HTML")
    
    for t in tasks:
        icon = "🔥" if t['status'] == 'overdue' else "⏳"
        creator = db.get_user_link(t['created_by'])
        
        txt = (
            f"{icon} <b>{t['title']}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📄 {t['description']}\n\n"
            f"🗓 Дедлайн: <code>{t['deadline']}</code>\n"
            f"👤 От: {creator}"
        )
        
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
    await c.message.edit_text("⚠️ <b>Удалить эту задачу безвозвратно?</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("confdel_"))
async def admin_del_task_confirm(c: CallbackQuery):
    tid = int(c.data.split("_")[1])
    task = db.cursor.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if task:
        await notify_user(task['assigned_to'], f"🗑 <b>Задача аннулирована:</b>\n{task['title']}")
        db.delete_task(tid)
        await c.message.edit_text("🗑 Задача удалена.")
    else:
        await c.answer("Задача уже удалена.")

@dp.callback_query(F.data.startswith("rej_"))
async def reject_ask(c: CallbackQuery):
    tid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отказаться", callback_data=f"confrej_{tid}")
    kb.button(text="Нет, вернусь", callback_data="ignore_cb")
    await c.message.edit_text("⚠️ <b>Вы уверены, что хотите отказаться?</b>\nЭто отправит уведомление основателям.", reply_markup=kb.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("confrej_"))
async def reject_confirm(c: CallbackQuery):
    tid = int(c.data.split("_")[1])
    task = db.cursor.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if task:
        db.cursor.execute("UPDATE tasks SET status='rejected' WHERE id=?", (tid,))
        db.conn.commit()
        
        rejector = db.get_user_link(c.from_user.id)
        alert = (
            f"⛔️ <b>ОТКАЗ ОТ ЗАДАЧИ</b>\n"
            f"👤 Пользователь: {rejector}\n"
            f"📌 Задача: {task['title']}\n"
            f"⚠️ Статус изменен на 'rejected'."
        )
        for admin_id in ADMIN_IDS: await notify_user(admin_id, alert)
        await c.message.edit_text("❌ Вы отказались от задачи.")
    else: await c.answer("Ошибка")

@dp.callback_query(F.data == "ignore_cb")
async def ignore_callback(c: CallbackQuery):
    await c.message.delete()

# --- ИСТОРИЯ ---
@dp.message(F.text.in_({"📜 История всех задач", "📜 История"}))
async def history(m: types.Message):
    uid = m.from_user.id
    role = db.get_user(uid)['role']
    limit = 20
    
    if role == 'founder':
        tasks = db.cursor.execute("SELECT * FROM tasks WHERE status='done' ORDER BY deadline DESC LIMIT ?", (limit,)).fetchall()
        head = "📜 <b>Глобальная история:</b>"
    else:
        tasks = db.cursor.execute("SELECT * FROM tasks WHERE status='done' AND assigned_to=? ORDER BY deadline DESC LIMIT ?", (uid, limit)).fetchall()
        head = "📜 <b>Ваша история:</b>"
        
    if not tasks: return await m.answer("📭 Пусто.")
    
    txt = f"{head}\n\n"
    for t in tasks:
        user_link = db.get_user_link(t['assigned_to'])
        txt += f"✅ <b>{t['title']}</b>\n👤 {user_link}\n🗓 {t['deadline']}\n"
        if t['file_url']: 
            if "tg:" in t['file_url']: txt += "📎 Файл (TG)\n"
            else: txt += f"💾 <a href='{t['file_url']}'>Файл (Диск)</a>\n"
        txt += "━━━━━━━━━━━━━━━━\n"
    await m.answer(txt, parse_mode="HTML", disable_web_page_preview=True)

# --- ЗАВЕРШЕНИЕ ЗАДАЧИ ---
@dp.callback_query(F.data.startswith("fin_"))
async def fin_start(c: CallbackQuery, state: FSMContext):
    tid = int(c.data.split("_")[1])
    task = db.cursor.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not task or task['status'] == 'done': return await c.answer("Неактуально.")
    
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
    if not (m.document or m.photo): return await m.answer("📎 Жду файл или фото.")
    
    msg = await m.answer("⏳ Загрузка...")
    if m.document:
        fid, fname, ftype = m.document.file_id, m.document.file_name, "doc"
    else:
        fid, fname, ftype = m.photo[-1].file_id, f"photo_{m.photo[-1].file_id}.jpg", "photo"

    pub_url = None
    try:
        f_info = await bot.get_file(fid)
        if f_info.file_size < 20*1024*1024:
            f_data = await bot.download_file(f_info.file_path)
            pub_url = ydisk.upload_and_publish(f_data, fname)
    except: pass

    if pub_url:
        await msg.edit_text("✅ На Диске!")
        await state.update_data(f_val=pub_url)
    else:
        await msg.edit_text("⚠️ Сохранено в Telegram.")
        await state.update_data(f_val=f"tg:{ftype}:{fid}")
    
    await m.answer("💬 Комментарий:", reply_markup=get_cancel_kb())
    await state.set_state(FinishTask.comment)

@dp.message(FinishTask.comment)
async def fin_commit(m: types.Message, state: FSMContext):
    if m.text == "🔙 Отмена": return await cancel_handler(m, state)
    d = await state.get_data()
    f_val = d.get('f_val')
    
    db.cursor.execute("UPDATE tasks SET status='done', file_url=?, comment=? WHERE id=?", (f_val, m.text, d['tid']))
    db.conn.commit()
    
    perf = db.get_user_link(m.from_user.id)
    txt = f"✅ <b>Задача выполнена!</b>\n📌 {d['title']}\n👤 {perf}\n💬 {m.text}"
    
    try:
        if f_val and "tg:" in f_val:
            txt += "\n📎 Файл ниже"
            await notify_user(d['creator'], txt)
            _, type_, fid = f_val.split(":", 2)
            if type_ == "photo": await bot.send_photo(d['creator'], fid)
            else: await bot.send_document(d['creator'], fid)
        elif f_val:
            txt += f"\n💾 <a href='{f_val}'>Файл (Диск)</a>"
            await notify_user(d['creator'], txt)
        else:
            await notify_user(d['creator'], txt)
    except: pass

    await m.answer("👍 Готово.", reply_markup=get_main_kb(db.get_user(m.from_user.id)['role']))
    await state.clear()

# ==============================================================================
# 9. SMM И ПЛАНИРОВЩИК
# ==============================================================================
@dp.message(F.text == "📝 Написать отчет")
async def smm_start(m: types.Message, state: FSMContext):
    await m.answer("✍️ Текст отчета:", reply_markup=get_cancel_kb())
    await state.set_state(SMMReportState.text)

@dp.message(SMMReportState.text)
async def smm_save(m: types.Message, state: FSMContext):
    if m.text == "🔙 Отмена": return await cancel_handler(m, state)
    db.cursor.execute("INSERT INTO reports (user_id, report_date, text) VALUES (?,?,?)", (m.from_user.id, datetime.date.today(), m.text))
    db.conn.commit()
    await m.answer("✅ Принято.", reply_markup=get_main_kb('smm'))
    await state.clear()

@dp.message(F.text == "📅 Мои отчеты")
async def smm_list(m: types.Message):
    reps = db.cursor.execute("SELECT * FROM reports WHERE user_id=? ORDER BY id DESC LIMIT 20", (m.from_user.id,)).fetchall()
    await m.answer("\n".join([f"📅 <b>{r['report_date']}</b>: {r['text']}" for r in reps]) if reps else "Пусто.", parse_mode="HTML")

# АВТОМАТИКА
async def job_check_overdue():
    today = datetime.date.today().strftime("%Y-%m-%d")
    tasks = db.cursor.execute("SELECT * FROM tasks WHERE deadline < ? AND status != 'done'", (today,)).fetchall()
    for t in tasks:
        if t['status'] != 'overdue':
            db.cursor.execute("UPDATE tasks SET status='overdue' WHERE id=?", (t['id'],))
            db.conn.commit()
        await notify_user(t['assigned_to'], f"⚠️ <b>ПРОСРОЧЕНО!</b>\n📌 {t['title']}")

async def job_deadline_alerts():
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = db.cursor.execute("SELECT * FROM tasks WHERE deadline = ? AND status != 'done'", (tomorrow,)).fetchall()
    for t in tasks: await notify_user(t['assigned_to'], f"⏰ <b>Дедлайн < 24ч!</b>\n📌 {t['title']}")

async def job_smm_daily():
    today = datetime.date.today().strftime("%Y-%m-%d")
    for s in db.cursor.execute("SELECT telegram_id FROM users WHERE role='smm'").fetchall():
        create_task_in_db("Daily SMM", "Сториз+Пост", s['telegram_id'], ADMIN_IDS[0], None, today)

async def job_onboarding():
    for a in db.cursor.execute("SELECT * FROM artists WHERE flag_contract=0").fetchall():
        kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_cont_{a['id']}").button(text="Позже", callback_data="ign")
        await notify_user(a['manager_id'], f"📝 Контракт с <b>{a['name']}</b> подписан?", kb.as_markup())
    
    if datetime.datetime.now().weekday() == 0:
        for a in db.cursor.execute("SELECT * FROM artists WHERE flag_mm_profile=0").fetchall():
            kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_mm_{a['id']}").button(text="Позже", callback_data="ign")
            await notify_user(a['manager_id'], f"🎵 MM профиль для <b>{a['name']}</b>?", kb.as_markup())

@dp.callback_query(F.data.startswith("onb_"))
async def onb_act(c: CallbackQuery):
    col = {'cont': 'flag_contract', 'mm': 'flag_mm_profile'}.get(c.data.split("_")[1])
    if col:
        db.cursor.execute(f"UPDATE artists SET {col}=1 WHERE id=?", (int(c.data.split("_")[2]),))
        db.conn.commit()
        await c.message.edit_text("✅ Статус обновлен!")

@dp.callback_query(F.data == "ign")
async def ign(c: CallbackQuery): await c.message.delete()

async def main():
    scheduler.add_job(job_check_overdue, CronTrigger(minute=0))
    scheduler.add_job(job_deadline_alerts, CronTrigger(hour='0,6,12,18'))
    scheduler.add_job(job_smm_daily, CronTrigger(hour=9))
    scheduler.add_job(job_onboarding, CronTrigger(hour=15))
    scheduler.start()
    await bot.delete_webhook(drop_pending_updates=True)
    print("BOT STARTED")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass