import asyncio
import logging
import os
import io
from datetime import datetime, timedelta
from enum import Enum

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import requests
from dotenv import load_dotenv

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, select, func, BigInteger, delete
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

API_TOKEN = os.getenv("BOT_TOKEN")
# Админы из ENV (резервный способ входа)
admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in admin_env.split(",") if id.strip().isdigit()]
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and not DATABASE_URL.startswith("postgresql+asyncpg"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ ---
Base = declarative_base()

class UserRole(str, Enum):
    FOUNDER = "founder"
    AR = "ar"
    DESIGNER = "designer"
    SMM = "smm"

class User(Base):
    __tablename__ = 'users'
    id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    role = Column(String)
    full_name = Column(String, nullable=True)

class Artist(Base):
    __tablename__ = 'artists'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    manager_id = Column(BigInteger, ForeignKey('users.id'))
    
    # Полный цикл онбординга
    contract_signed = Column(Boolean, default=False)
    musixmatch_created = Column(Boolean, default=False)
    musixmatch_verified = Column(Boolean, default=False)
    youtube_note = Column(Boolean, default=False)
    youtube_channel_linked = Column(Boolean, default=False)
    
    first_release_date = Column(DateTime(timezone=False), nullable=True)

class Release(Base):
    __tablename__ = 'releases'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String)
    artist_id = Column(Integer, ForeignKey('artists.id'))
    co_artists = Column(String, nullable=True) # Со-артисты текстом
    release_type = Column(String)
    release_date = Column(DateTime(timezone=False))
    created_by = Column(BigInteger, ForeignKey('users.id'))
    
    tasks = relationship("Task", back_populates="release", cascade="all, delete-orphan")

class Task(Base):
    __tablename__ = 'tasks'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String)
    description = Column(String, nullable=True)
    status = Column(String, default="pending") # pending, in_progress, done, overdue
    deadline = Column(DateTime(timezone=False))
    
    assigned_to = Column(BigInteger, ForeignKey('users.id'))
    created_by = Column(BigInteger, ForeignKey('users.id'))
    release_id = Column(Integer, ForeignKey('releases.id'), nullable=True)
    parent_task_id = Column(Integer, ForeignKey('tasks.id'), nullable=True) # Иерархия
    
    requires_file = Column(Boolean, default=False)
    file_url = Column(String, nullable=True)
    comment = Column(Text, nullable=True)
    
    release = relationship("Release", back_populates="tasks")

class SmmReport(Base):
    __tablename__ = 'smm_reports'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'))
    text = Column(Text)
    created_at = Column(DateTime(timezone=False), default=datetime.now)

# Init DB
if not DATABASE_URL:
    logger.error("No DATABASE_URL")
    exit(1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# --- ВНЕШНИЕ СЕРВИСЫ (YANDEX) ---
async def upload_file_to_yandex(bot: Bot, file_id: str, remote_name: str):
    """Скачивает файл из ТГ и грузит на Яндекс.Диск"""
    if not YANDEX_DISK_TOKEN:
        return f"local_tg_{file_id}" # Fallback
    
    try:
        # 1. Скачиваем из ТГ
        file_info = await bot.get_file(file_id)
        file_bytes = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=file_bytes)
        file_bytes.seek(0)
        
        # 2. Получаем URL загрузки от Яндекса
        headers = {'Authorization': f'OAuth {YANDEX_DISK_TOKEN}'}
        path = f"/MusicLabelBot/{remote_name}"
        resp_get = requests.get(
            'https://cloud-api.yandex.net/v1/disk/resources/upload',
            params={'path': path, 'overwrite': 'true'},
            headers=headers
        )
        if resp_get.status_code != 200:
            logger.error(f"Yandex Error: {resp_get.text}")
            return None
            
        upload_url = resp_get.json().get('href')
        
        # 3. Грузим
        requests.put(upload_url, files={'file': file_bytes})
        return f"https://disk.yandex.ru/client/disk/MusicLabelBot/{remote_name}"
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return None

# --- FSM ---
class ReleaseForm(StatesGroup):
    artist = State()
    co_artists = State() # Новое: Со-артисты
    title = State()
    type = State()
    date = State()

class TaskCompletion(StatesGroup):
    file = State()
    comment = State()

class NewArtist(StatesGroup):
    name = State()
    date = State()

class CustomTask(StatesGroup): # Новое: Ручные задачи
    title = State()
    assignee_role = State()
    deadline = State()

class AddUser(StatesGroup): # Новое: Управление командой
    id = State()
    role = State()
    name = State()

class SmmReportState(StatesGroup):
    text = State()

# --- UTILS & KEYBOARDS ---
async def get_user_role(user_id):
    async with AsyncSessionLocal() as session:
        u = await session.get(User, user_id)
        return u.role if u else None

def get_menu(role):
    kb = []
    if role == UserRole.FOUNDER:
        kb = [
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Команда")],
            [KeyboardButton(text="❌ Удалить релиз"), KeyboardButton(text="🚨 Алерт Питчинг")],
            [KeyboardButton(text="➕ Создать задачу"), KeyboardButton(text="📋 Все задачи")]
        ]
    elif role == UserRole.AR:
        kb = [
            [KeyboardButton(text="💿 Новый релиз"), KeyboardButton(text="🎤 Новый артист")],
            [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="➕ Создать задачу")],
            [KeyboardButton(text="🆘 PANIC BUTTON")]
        ]
    elif role == UserRole.DESIGNER:
        kb = [[KeyboardButton(text="🎨 Задачи"), KeyboardButton(text="✅ Выполненные")]]
    elif role == UserRole.SMM:
        kb = [
            [KeyboardButton(text="📱 Задачи SMM"), KeyboardButton(text="📝 Отчет")],
            [KeyboardButton(text="🗂 История отчетов")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- БОТ ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- BASIC HANDLERS ---
@dp.message(CommandStart())
async def start(message: types.Message):
    uid = message.from_user.id
    if uid in ADMIN_IDS:
        async with AsyncSessionLocal() as session:
            if not await session.get(User, uid):
                session.add(User(id=uid, role=UserRole.FOUNDER, full_name=message.from_user.full_name))
                await session.commit()
    
    role = await get_user_role(uid)
    if role:
        await message.answer(f"Добро пожаловать, {role}!", reply_markup=get_menu(role))
    else:
        await message.answer("⛔️ Нет доступа.")

# --- 1. УПРАВЛЕНИЕ КОМАНДОЙ (FOUNDER) ---
@dp.message(F.text == "👥 Команда")
async def team_menu(message: types.Message):
    if await get_user_role(message.from_user.id) != UserRole.FOUNDER: return
    
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    
    txt = "<b>Состав команды:</b>\n"
    for u in users:
        txt += f"• {u.full_name} (ID: <code>{u.id}</code>) — {u.role}\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="add_user")],
        [InlineKeyboardButton(text="❌ Удалить сотрудника", callback_data="del_user_menu")]
    ])
    await message.answer(txt, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "add_user")
async def add_user_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите Telegram ID сотрудника:")
    await state.set_state(AddUser.id)

@dp.message(AddUser.id)
async def add_user_id(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit(): return await msg.answer("Нужно число.")
    await state.update_data(id=int(msg.text))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r.value, callback_data=f"role_{r.value}")] for r in UserRole
    ])
    await msg.answer("Выберите роль:", reply_markup=kb)
    await state.set_state(AddUser.role)

@dp.callback_query(F.data.startswith("role_"))
async def add_user_role(cb: types.CallbackQuery, state: FSMContext):
    role = cb.data.split("_")[1]
    await state.update_data(role=role)
    await cb.message.answer("Введите имя сотрудника:")
    await state.set_state(AddUser.name)

@dp.message(AddUser.name)
async def add_user_finish(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        session.add(User(id=data['id'], role=data['role'], full_name=msg.text))
        await session.commit()
    await msg.answer(f"✅ Сотрудник {msg.text} добавлен!")
    await state.clear()

@dp.callback_query(F.data == "del_user_menu")
async def del_user_menu(cb: types.CallbackQuery):
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User).where(User.role != UserRole.FOUNDER))).scalars().all()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {u.full_name}", callback_data=f"del_usr_{u.id}")] for u in users
    ])
    await cb.message.answer("Кого удалить?", reply_markup=kb)

@dp.callback_query(F.data.startswith("del_usr_"))
async def del_user_act(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        await session.execute(delete(User).where(User.id == uid))
        await session.commit()
    await cb.message.edit_text("Пользователь удален.")

# --- 2. УПРАВЛЕНИЕ РЕЛИЗАМИ И СО-АРТИСТАМИ ---
@dp.message(F.text == "💿 Новый релиз")
async def rel_start(msg: types.Message, state: FSMContext):
    if await get_user_role(msg.from_user.id) not in [UserRole.AR, UserRole.FOUNDER]: return
    async with AsyncSessionLocal() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
    if not artists: return await msg.answer("Сначала создайте артиста!")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a.name, callback_data=f"sel_art_{a.id}")] for a in artists
    ])
    await msg.answer("Выберите основного артиста:", reply_markup=kb)
    await state.set_state(ReleaseForm.artist)

@dp.callback_query(F.data.startswith("sel_art_"))
async def rel_art(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(aid=int(cb.data.split("_")[2]))
    await cb.message.answer("Есть со-артисты (Feat)? Введите имена текстом или 'нет':")
    await state.set_state(ReleaseForm.co_artists)

@dp.message(ReleaseForm.co_artists)
async def rel_co(msg: types.Message, state: FSMContext):
    co = msg.text if msg.text.lower() != "нет" else None
    await state.update_data(co=co)
    await msg.answer("Название релиза:")
    await state.set_state(ReleaseForm.title)

@dp.message(ReleaseForm.title)
async def rel_title(msg: types.Message, state: FSMContext):
    await state.update_data(title=msg.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="80/20", callback_data="tp_8020"), InlineKeyboardButton(text="50/50", callback_data="tp_5050")]
    ])
    await msg.answer("Тип сделки:", reply_markup=kb)
    await state.set_state(ReleaseForm.type)

@dp.callback_query(F.data.startswith("tp_"))
async def rel_type(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(tp=cb.data.split("_")[1])
    await cb.message.answer("Дата релиза (ДД.ММ.ГГГГ):")
    await state.set_state(ReleaseForm.date)

@dp.message(ReleaseForm.date)
async def rel_finish(msg: types.Message, state: FSMContext):
    try:
        rdate = datetime.strptime(msg.text, "%d.%m.%Y")
    except: return await msg.answer("Неверный формат.")
    
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        rel = Release(title=data['title'], artist_id=data['aid'], co_artists=data['co'], 
                      release_type=data['tp'], release_date=rdate, created_by=msg.from_user.id)
        session.add(rel)
        await session.flush()
        
        # АВТО-ЗАДАЧИ
        ar_usr = await session.scalar(select(User).where(User.role == UserRole.AR).limit(1))
        des_usr = await session.scalar(select(User).where(User.role == UserRole.DESIGNER).limit(1))
        if not ar_usr: ar_usr = await session.get(User, msg.from_user.id)
        if not des_usr: des_usr = ar_usr

        # Шаблоны
        # 1. Менеджерская (Родительская)
        main_task = Task(title=f"Подготовка {data['title']}", assigned_to=ar_usr.id, release_id=rel.id, 
                         deadline=rdate - timedelta(days=15), created_by=msg.from_user.id)
        session.add(main_task)
        await session.flush()

        # 2. Дизайнерская (Дочерняя)
        des_task = Task(title=f"Обложка {data['title']}", assigned_to=des_usr.id, release_id=rel.id,
                        deadline=rdate - timedelta(days=20), requires_file=True, 
                        parent_task_id=main_task.id, created_by=msg.from_user.id)
        session.add(des_task)
        
        # 3. Питчинг
        if data['tp'] == "8020":
            session.add(Task(title=f"Питчинг {data['title']}", assigned_to=ar_usr.id, release_id=rel.id,
                             deadline=rdate - timedelta(days=10), created_by=msg.from_user.id))

        await session.commit()
        await bot.send_message(des_usr.id, f"🆕 Вам назначена задача: Обложка {data['title']}")

    await msg.answer(f"✅ Релиз '{data['title']}' создан, задачи сгенерированы!")
    await state.clear()

# --- 3. РУЧНОЕ СОЗДАНИЕ ЗАДАЧ (CUSTOM TASKS) ---
@dp.message(F.text == "➕ Создать задачу")
async def custom_task_start(msg: types.Message, state: FSMContext):
    await msg.answer("Введите название задачи:")
    await state.set_state(CustomTask.title)

@dp.message(CustomTask.title)
async def custom_task_role(msg: types.Message, state: FSMContext):
    await state.update_data(title=msg.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r.value, callback_data=f"ct_role_{r.value}")] for r in UserRole
    ])
    await msg.answer("Для какой роли задача?", reply_markup=kb)
    await state.set_state(CustomTask.assignee_role)

@dp.callback_query(F.data.startswith("ct_role_"))
async def custom_task_dead(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(role=cb.data.split("_")[2])
    await cb.message.answer("Дедлайн (кол-во дней от сегодня, например '3'):")
    await state.set_state(CustomTask.deadline)

@dp.message(CustomTask.deadline)
async def custom_task_fin(msg: types.Message, state: FSMContext):
    days = int(msg.text) if msg.text.isdigit() else 1
    data = await state.get_data()
    
    async with AsyncSessionLocal() as session:
        # Находим любого юзера с этой ролью
        worker = await session.scalar(select(User).where(User.role == data['role']).limit(1))
        if not worker: return await msg.answer("Нет сотрудников с такой ролью.")
        
        t = Task(title=data['title'], assigned_to=worker.id, created_by=msg.from_user.id,
                 deadline=datetime.now() + timedelta(days=days), description="Ручная задача")
        session.add(t)
        await session.commit()
        await bot.send_message(worker.id, f"🆕 Ручная задача: {data['title']}")
    
    await msg.answer("✅ Задача создана.")
    await state.clear()

# --- 4. ЗАДАЧИ, ФАЙЛЫ И ВЫПОЛНЕНИЕ ---
@dp.message(F.text.contains("Задачи"))
async def list_tasks(msg: types.Message):
    uid = msg.from_user.id
    role = await get_user_role(uid)
    async with AsyncSessionLocal() as session:
        q = select(Task).where(Task.status.in_(['pending', 'in_progress', 'overdue'])).order_by(Task.deadline)
        if role != UserRole.FOUNDER: q = q.where(Task.assigned_to == uid)
        tasks = (await session.execute(q)).scalars().all()
    
    if not tasks: return await msg.answer("Задач нет.")
    for t in tasks:
        icon = "🔥" if t.status == "overdue" else "⏳"
        d = t.deadline.strftime("%d.%m %H:%M")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Завершить", callback_data=f"done_{t.id}")]])
        await msg.answer(f"{icon} <b>{t.title}</b>\nДедлайн: {d}", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("done_"))
async def done_start(cb: types.CallbackQuery, state: FSMContext):
    tid = int(cb.data.split("_")[1])
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, tid)
        if task.requires_file:
            await state.update_data(tid=tid)
            await cb.message.answer("📎 Задача требует файла. Пришлите файл:")
            await state.set_state(TaskCompletion.file)
        else:
            await cb.message.answer("Напишите комментарий (или 'нет'):")
            await state.update_data(tid=tid)
            await state.set_state(TaskCompletion.comment)

@dp.message(TaskCompletion.file, F.document | F.photo)
async def done_file(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    file_id = msg.document.file_id if msg.document else msg.photo[-1].file_id
    filename = msg.document.file_name if msg.document else f"photo_{datetime.now().timestamp()}.jpg"
    
    # ЗАГРУЗКА НА ЯНДЕКС
    await msg.answer("⏳ Загружаю на Яндекс.Диск...")
    yandex_url = await upload_file_to_yandex(bot, file_id, filename)
    
    async with AsyncSessionLocal() as session:
        t = await session.get(Task, data['tid'])
        t.status = "done"
        t.file_url = yandex_url
        await session.commit()
        if t.created_by: 
            await bot.send_message(t.created_by, f"✅ Задача '{t.title}' выполнена!\nФайл: {yandex_url}")
    
    await msg.answer("✅ Готово!")
    await state.clear()

@dp.message(TaskCompletion.comment)
async def done_comment(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        t = await session.get(Task, data['tid'])
        t.status = "done"
        t.comment = msg.text
        await session.commit()
        if t.created_by:
             await bot.send_message(t.created_by, f"✅ Задача '{t.title}' выполнена!\nКоммент: {msg.text}")
    await msg.answer("✅ Задача закрыта.")
    await state.clear()

# --- 5. ПОЛНЫЙ ОНБОРДИНГ (ЦЕПОЧКА) ---
@dp.message(F.text == "🎤 Новый артист")
async def new_art(msg: types.Message, state: FSMContext):
    await msg.answer("Имя артиста:")
    await state.set_state(NewArtist.name)

@dp.message(NewArtist.name)
async def new_art_name(msg: types.Message, state: FSMContext):
    await state.update_data(name=msg.text)
    async with AsyncSessionLocal() as session:
        session.add(Artist(name=msg.text, manager_id=msg.from_user.id))
        await session.commit()
    await msg.answer("Артист добавлен. Бот начнет онбординг.")
    await state.clear()

async def run_onboarding_check():
    """Полная цепочка проверок"""
    async with AsyncSessionLocal() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
        for a in artists:
            # Логика "Лесенки"
            msg, step = None, None
            if not a.contract_signed:
                msg, step = "📝 Подписан ли договор?", "contract"
            elif not a.musixmatch_created:
                msg, step = "🎵 Создан ли профиль Musixmatch?", "m_create"
            elif not a.musixmatch_verified:
                msg, step = "✅ Верифицирован ли Musixmatch?", "m_verify"
            elif not a.youtube_note:
                msg, step = "🎶 Подана заявка на Нотку YouTube?", "yt_note"
            elif not a.youtube_channel_linked:
                msg, step = "🔗 Привязан ли канал YouTube?", "yt_link"
            
            if msg:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Да", callback_data=f"onb_{step}_y_{a.id}"),
                     InlineKeyboardButton(text="Нет", callback_data=f"onb_{step}_n_{a.id}")]
                ])
                try: await bot.send_message(a.manager_id, f"🔔 <b>Онбординг {a.name}</b>\n{msg}", reply_markup=kb, parse_mode="HTML")
                except: pass

@dp.callback_query(F.data.startswith("onb_"))
async def onb_handler(cb: types.CallbackQuery):
    _, step, ans, aid = cb.data.split("_")
    aid = int(aid)
    if ans == "n": return await cb.message.edit_text("Ок, напомню завтра.")
    
    async with AsyncSessionLocal() as session:
        a = await session.get(Artist, aid)
        if step == "contract": a.contract_signed = True
        elif step == "m_create": a.musixmatch_created = True
        elif step == "m_verify": a.musixmatch_verified = True
        elif step == "yt_note": a.youtube_note = True
        elif step == "yt_link": a.youtube_channel_linked = True
        await session.commit()
    await cb.message.edit_text("✅ Этап пройден!")

# --- 6. SMM ОТЧЕТЫ И ЗАДАЧИ ---
@dp.message(F.text == "📝 Отчет")
async def smm_rep_start(msg: types.Message, state: FSMContext):
    await msg.answer("Напишите ваш отчет за сегодня:")
    await state.set_state(SmmReportState.text)

@dp.message(SmmReportState.text)
async def smm_rep_save(msg: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        session.add(SmmReport(user_id=msg.from_user.id, text=msg.text))
        await session.commit()
    await msg.answer("✅ Отчет сохранен.")
    await state.clear()

@dp.message(F.text == "🗂 История отчетов")
async def smm_history(msg: types.Message):
    async with AsyncSessionLocal() as session:
        reps = (await session.execute(select(SmmReport).where(SmmReport.user_id == msg.from_user.id).order_by(SmmReport.created_at.desc()).limit(5))).scalars().all()
    txt = "\n\n".join([f"📅 {r.created_at.strftime('%d.%m')}: {r.text}" for r in reps]) or "Нет отчетов."
    await msg.answer(txt)

async def daily_smm_task():
    """Генерация ежедневной задачи SMM"""
    async with AsyncSessionLocal() as session:
        smms = (await session.execute(select(User).where(User.role == UserRole.SMM))).scalars().all()
        for u in smms:
            t = Task(title="📱 Выложить контент", assigned_to=u.id, deadline=datetime.now() + timedelta(hours=12))
            session.add(t)
            try: await bot.send_message(u.id, "🆕 Ежедневная задача: Выложить контент")
            except: pass
        await session.commit()

# --- SCHEDULER JOBS ---
async def check_overdue():
    async with AsyncSessionLocal() as session:
        tasks = (await session.execute(select(Task).where(Task.deadline < datetime.now(), Task.status.in_(['pending','in_progress'])))).scalars().all()
        for t in tasks:
            t.status = "overdue"
            try: await bot.send_message(t.assigned_to, f"⚠️ ПРОСРОЧЕНО: {t.title}")
            except: pass
        await session.commit()

async def check_deadlines_24h():
    async with AsyncSessionLocal() as session:
        target = datetime.now() + timedelta(days=1)
        tasks = (await session.execute(select(Task).where(Task.deadline < target, Task.deadline > datetime.now(), Task.status != 'done'))).scalars().all()
        for t in tasks:
            try: await bot.send_message(t.assigned_to, f"⏰ Менее 24ч до дедлайна: {t.title}")
            except: pass

async def critical_pitch_check():
    async with AsyncSessionLocal() as session:
        target = datetime.now().date() + timedelta(days=3)
        rels = (await session.execute(select(Release))).scalars().all()
        for r in rels:
            if r.release_date.date() == target:
                ptask = (await session.execute(select(Task).where(Task.release_id==r.id, Task.title.like('%Питчинг%'), Task.status!='done'))).scalars().first()
                if ptask:
                    for adm in ADMIN_IDS:
                        try: await bot.send_message(adm, f"🔥 КРИТИЧЕСКИЙ АЛЕРТ: Питчинг релиза {r.title} провален!")
                        except: pass

# --- START ---
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    scheduler.add_job(check_overdue, 'interval', hours=1)
    scheduler.add_job(check_deadlines_24h, 'interval', hours=6)
    scheduler.add_job(run_onboarding_check, 'cron', hour=15)
    scheduler.add_job(daily_smm_task, 'cron', hour=10)
    scheduler.add_job(critical_pitch_check, 'cron', hour=11)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())