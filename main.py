import asyncio
import logging
import os
from datetime import datetime, timedelta
from enum import Enum

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import requests
from dotenv import load_dotenv

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, select, func, BigInteger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

# --- ЗАГРУЗКА КОНФИГУРАЦИИ ---
load_dotenv()

API_TOKEN = os.getenv("BOT_TOKEN")
# Парсим админов
admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in admin_env.split(",") if id.strip().isdigit()]
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Исправление URL для SQLAlchemy
if DATABASE_URL and not DATABASE_URL.startswith("postgresql+asyncpg"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

# Логирование
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
    id = Column(BigInteger, primary_key=True)  # Telegram ID
    username = Column(String, nullable=True)
    role = Column(String)
    full_name = Column(String, nullable=True)

class Artist(Base):
    __tablename__ = 'artists'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    manager_id = Column(BigInteger, ForeignKey('users.id'))
    
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
    
    requires_file = Column(Boolean, default=False)
    file_url = Column(String, nullable=True)
    comment = Column(Text, nullable=True)
    
    release = relationship("Release", back_populates="tasks")

# Инициализация DB
if not DATABASE_URL:
    logger.error("DATABASE_URL не найден!")
    exit(1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# --- FSM СОСТОЯНИЯ ---
class ReleaseForm(StatesGroup):
    waiting_for_artist = State()
    waiting_for_title = State()
    waiting_for_type = State()
    waiting_for_date = State()

class TaskCompletion(StatesGroup):
    waiting_for_file = State()

class NewArtist(StatesGroup):
    waiting_for_name = State()
    waiting_for_release_date = State()

# --- БОТ ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- UTILS ---
async def is_authorized(user_id):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        return user is not None

async def get_user_role(user_id):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        return user.role if user else None

def get_main_menu(role):
    kb = []
    if role == UserRole.FOUNDER:
        kb = [
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Сотрудники")],
            [KeyboardButton(text="❌ Удалить релиз"), KeyboardButton(text="🚨 Алерт Питчинг")],
             [KeyboardButton(text="📋 Все задачи")]
        ]
    elif role == UserRole.AR:
        kb = [
            [KeyboardButton(text="💿 Новый релиз"), KeyboardButton(text="🎤 Новый артист")],
            [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="🆘 PANIC BUTTON")]
        ]
    elif role == UserRole.DESIGNER:
        kb = [
            [KeyboardButton(text="🎨 Задачи (Обложки)"), KeyboardButton(text="✅ Выполненные")]
        ]
    elif role == UserRole.SMM:
        kb = [
            [KeyboardButton(text="📱 Задачи SMM"), KeyboardButton(text="✅ Выполненные")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- HANDLERS: START & AUTH ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    # Авторегистрация админов
    if user_id in ADMIN_IDS:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, user_id)
            if not user:
                new_user = User(id=user_id, username=message.from_user.username, role=UserRole.FOUNDER, full_name=message.from_user.full_name)
                session.add(new_user)
                await session.commit()
                await message.answer("👑 Вы зарегистрированы как Основатель.")
    
    if await is_authorized(user_id):
        role = await get_user_role(user_id)
        await message.answer(f"👋 С возвращением! Ваша роль: {role}", reply_markup=get_main_menu(role))
    else:
        await message.answer("⛔️ Доступ запрещен. Ваш ID не найден в базе.")

# --- HANDLERS: FOUNDER (ОСНОВАТЕЛЬ) ---

@dp.message(F.text == "👥 Сотрудники")
async def list_employees(message: types.Message):
    if await get_user_role(message.from_user.id) != UserRole.FOUNDER:
        return
    
    async with AsyncSessionLocal() as session:
        users = (await session.execute(select(User))).scalars().all()
    
    text = "<b>Сотрудники:</b>\n"
    for u in users:
        text += f"👤 {u.full_name} (@{u.username}) — <b>{u.role}</b> (ID: {u.id})\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📊 Статистика")
async def show_stats(message: types.Message):
    if await get_user_role(message.from_user.id) != UserRole.FOUNDER:
        return

    async with AsyncSessionLocal() as session:
        u_count = await session.scalar(select(func.count(User.id)))
        r_count = await session.scalar(select(func.count(Release.id)))
        t_active = await session.scalar(select(func.count(Task.id)).where(Task.status.in_(['pending', 'in_progress'])))
        t_overdue = await session.scalar(select(func.count(Task.id)).where(Task.status == 'overdue'))
    
    await message.answer(
        f"📊 <b>Статистика:</b>\n\n"
        f"👥 Людей: {u_count}\n"
        f"💿 Релизов: {r_count}\n"
        f"⚡️ Активных задач: {t_active}\n"
        f"🔥 Просрочено: {t_overdue}", 
        parse_mode="HTML"
    )

@dp.message(F.text == "❌ Удалить релиз")
async def delete_release_menu(message: types.Message):
    if await get_user_role(message.from_user.id) != UserRole.FOUNDER:
        return

    async with AsyncSessionLocal() as session:
        releases = (await session.execute(select(Release))).scalars().all()
    
    if not releases:
        await message.answer("Нет релизов.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {r.title}", callback_data=f"del_rel_{r.id}")] for r in releases
    ])
    await message.answer("Выберите релиз для удаления:", reply_markup=kb)

@dp.callback_query(F.data.startswith("del_rel_"))
async def process_delete_release(callback: types.CallbackQuery):
    rid = int(callback.data.split("_")[2])
    async with AsyncSessionLocal() as session:
        rel = await session.get(Release, rid)
        if rel:
            await session.delete(rel)
            await session.commit()
            await callback.message.edit_text(f"✅ Релиз '{rel.title}' удален.")
        else:
            await callback.message.edit_text("Релиз не найден.")

@dp.message(F.text == "🚨 Алерт Питчинг")
async def manual_pitch_alert(message: types.Message):
    await message.answer("🔄 Проверка запущена...")
    await critical_pitching_check()
    await message.answer("✅ Проверка завершена.")

# --- HANDLERS: A&R (РЕЛИЗЫ И АРТИСТЫ) ---

@dp.message(F.text == "🎤 Новый артист")
async def add_artist_start(message: types.Message, state: FSMContext):
    await message.answer("Введите имя артиста:")
    await state.set_state(NewArtist.waiting_for_name)

@dp.message(NewArtist.waiting_for_name)
async def add_artist_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Дата первого релиза (ДД.ММ.ГГГГ) или напишите 'нет':")
    await state.set_state(NewArtist.waiting_for_release_date)

@dp.message(NewArtist.waiting_for_release_date)
async def add_artist_final(message: types.Message, state: FSMContext):
    date = None
    if "нет" not in message.text.lower():
        try:
            date = datetime.strptime(message.text, "%d.%m.%Y")
        except: pass
    
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        art = Artist(name=data['name'], manager_id=message.from_user.id, first_release_date=date)
        session.add(art)
        await session.commit()
    
    await message.answer(f"✅ Артист {data['name']} создан.")
    await state.clear()

@dp.message(F.text == "💿 Новый релиз")
async def new_release_start(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
    
    if not artists:
        await message.answer("Нет артистов. Создайте сначала артиста.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a.name, callback_data=f"sel_art_{a.id}")] for a in artists
    ])
    await message.answer("Выберите артиста:", reply_markup=kb)
    await state.set_state(ReleaseForm.waiting_for_artist)

@dp.callback_query(F.data.startswith("sel_art_"))
async def new_release_artist(callback: types.CallbackQuery, state: FSMContext):
    aid = int(callback.data.split("_")[2])
    await state.update_data(artist_id=aid)
    await callback.message.answer("Название релиза:")
    await state.set_state(ReleaseForm.waiting_for_title)

@dp.message(ReleaseForm.waiting_for_title)
async def new_release_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="80/20", callback_data="type_8020")],
        [InlineKeyboardButton(text="50/50", callback_data="type_5050")]
    ])
    await message.answer("Тип сделки:", reply_markup=kb)
    await state.set_state(ReleaseForm.waiting_for_type)

@dp.callback_query(F.data.startswith("type_"))
async def new_release_type(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(rtype=callback.data.split("_")[1])
    await callback.message.answer("Дата релиза (ДД.ММ.ГГГГ):")
    await state.set_state(ReleaseForm.waiting_for_date)

@dp.message(ReleaseForm.waiting_for_date)
async def new_release_finish(message: types.Message, state: FSMContext):
    try:
        rdate = datetime.strptime(message.text, "%d.%m.%Y")
    except:
        await message.answer("Ошибка формата даты.")
        return

    data = await state.get_data()
    
    async with AsyncSessionLocal() as session:
        # 1. Create Release
        rel = Release(
            title=data['title'], artist_id=data['artist_id'], 
            release_type=data['rtype'], release_date=rdate, 
            created_by=message.from_user.id
        )
        session.add(rel)
        await session.flush()

        # 2. Generate Tasks
        # Находим исполнителей (берем первых попавшихся для упрощения)
        ar_user = await session.scalar(select(User).where(User.role == UserRole.AR).limit(1))
        des_user = await session.scalar(select(User).where(User.role == UserRole.DESIGNER).limit(1))
        
        # Если нет дизайнера, назначаем A&R или Основателя
        if not des_user: des_user = ar_user

        tasks_def = [
            {"t": f"Загрузка {data['title']}", "u": ar_user, "d": -14, "f": False},
            {"t": f"Обложка {data['title']}", "u": des_user, "d": -20, "f": True},
        ]
        if data['rtype'] == "8020":
            tasks_def.append({"t": f"Питчинг {data['title']}", "u": ar_user, "d": -10, "f": False})

        for td in tasks_def:
            if td['u']:
                new_t = Task(
                    title=td['t'], description="Auto", status="pending",
                    deadline=rdate + timedelta(days=td['d']),
                    assigned_to=td['u'].id, created_by=message.from_user.id,
                    release_id=rel.id, requires_file=td['f']
                )
                session.add(new_t)
                try:
                    await bot.send_message(td['u'].id, f"🆕 Новая задача: {td['t']}")
                except: pass
        
        await session.commit()
    
    await message.answer(f"✅ Релиз '{data['title']}' создан!")
    await state.clear()

@dp.message(F.text == "🆘 PANIC BUTTON")
async def panic_button(message: types.Message):
    for aid in ADMIN_IDS:
        await bot.send_message(aid, f"🆘 <b>ТРЕВОГА от {message.from_user.full_name}!</b>\nСрочно свяжитесь!", parse_mode="HTML")
    await message.answer("Сигнал отправлен администраторам.")

# --- HANDLERS: TASKS (COMMON) ---

@dp.message(F.text.in_({"📋 Мои задачи", "🎨 Задачи (Обложки)", "📱 Задачи SMM", "📋 Все задачи"}))
async def show_my_tasks(message: types.Message):
    uid = message.from_user.id
    role = await get_user_role(uid)
    
    async with AsyncSessionLocal() as session:
        query = select(Task).where(Task.status.in_(['pending', 'in_progress', 'overdue'])).order_by(Task.deadline)
        
        # Если не основатель - видим только свои
        if role != UserRole.FOUNDER:
            query = query.where(Task.assigned_to == uid)
            
        tasks = (await session.execute(query)).scalars().all()

    if not tasks:
        await message.answer("Задач нет.")
        return

    for t in tasks:
        emoji = "🔥" if t.status == "overdue" else "⏳"
        d_str = t.deadline.strftime("%d.%m") if t.deadline else "?"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить", callback_data=f"done_{t.id}")]
        ])
        await message.answer(f"{emoji} <b>{t.title}</b>\nДедлайн: {d_str}\nСтатус: {t.status}", reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "✅ Выполненные")
async def show_done_tasks(message: types.Message):
    uid = message.from_user.id
    async with AsyncSessionLocal() as session:
        tasks = (await session.execute(select(Task).where(Task.assigned_to == uid, Task.status == "done").limit(10))).scalars().all()
    
    text = "Последние выполненные:\n" + "\n".join([f"✅ {t.title}" for t in tasks])
    await message.answer(text if tasks else "Пусто.")

@dp.callback_query(F.data.startswith("done_"))
async def done_task_click(callback: types.CallbackQuery, state: FSMContext):
    tid = int(callback.data.split("_")[1])
    
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, tid)
        if not task:
            await callback.message.answer("Задача не найдена.")
            return
        
        if task.requires_file:
            await state.update_data(tid=tid)
            await callback.message.answer("📎 Пришлите файл (фото/док) для завершения.")
            await state.set_state(TaskCompletion.waiting_for_file)
        else:
            task.status = "done"
            await session.commit()
            await callback.message.edit_text(f"✅ Задача '{task.title}' выполнена!")
            if task.created_by:
                try: await bot.send_message(task.created_by, f"✅ Задача '{task.title}' закрыта.")
                except: pass

@dp.message(TaskCompletion.waiting_for_file, F.document | F.photo)
async def task_file_upload(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    # Эмуляция загрузки
    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, data['tid'])
        task.status = "done"
        task.file_url = f"tg_file:{file_id}"
        await session.commit()
        
        await message.answer("✅ Файл принят, задача закрыта!")
        if task.created_by:
             try: await bot.send_message(task.created_by, f"✅📎 Задача '{task.title}' закрыта (файл приложен).")
             except: pass
    await state.clear()

# --- HANDLERS: ONBOARDING CALLBACKS ---
@dp.callback_query(F.data.startswith("onb_"))
async def onb_callback(callback: types.CallbackQuery):
    # Format: onb_TYPE_ANSWER_ARTID
    parts = callback.data.split("_")
    otype, ans, aid = parts[1], parts[2], int(parts[3])
    
    if ans == "no":
        await callback.message.edit_text("Понял, напомню позже.")
        return
    
    async with AsyncSessionLocal() as session:
        art = await session.get(Artist, aid)
        if not art: return

        msg = "OK"
        if otype == "contract":
            art.contract_signed = True
            msg = "Контракт подписан!"
        elif otype == "musix":
            art.musixmatch_created = True
            msg = "Musixmatch создан!"
        
        await session.commit()
        await callback.message.edit_text(f"✅ {msg}")

# --- SCHEDULER ---
async def check_overdue_tasks():
    async with AsyncSessionLocal() as session:
        now = datetime.now()
        tasks = (await session.execute(select(Task).where(Task.deadline < now, Task.status.in_(['pending', 'in_progress'])))).scalars().all()
        for t in tasks:
            t.status = "overdue"
            try: await bot.send_message(t.assigned_to, f"⚠️ ПРОСРОЧЕНО: {t.title}")
            except: pass
        await session.commit()

async def onboarding_audit():
    async with AsyncSessionLocal() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
        for a in artists:
            if not a.contract_signed:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Да", callback_data=f"onb_contract_yes_{a.id}"),
                     InlineKeyboardButton(text="Нет", callback_data=f"onb_contract_no_{a.id}")]
                ])
                try: await bot.send_message(a.manager_id, f"📝 {a.name}: Контракт подписан?", reply_markup=kb)
                except: pass

async def critical_pitching_check():
    async with AsyncSessionLocal() as session:
        target = datetime.now().date() + timedelta(days=3)
        # Сравниваем только дату через Python (для совместимости с разными БД)
        # Загружаем все активные задачи по питчингу
        tasks = (await session.execute(select(Task).where(Task.title.ilike("%питчинг%"), Task.status != "done"))).scalars().all()
        
        for t in tasks:
            if t.deadline and t.deadline.date() == target:
                for adm in ADMIN_IDS:
                    try: await bot.send_message(adm, f"🔥 АЛЕРТ: Питчинг '{t.title}' горит!")
                    except: pass

# --- MAIN ---
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    scheduler.add_job(check_overdue_tasks, 'interval', hours=1)
    scheduler.add_job(onboarding_audit, 'interval', hours=24) # В реале 'cron'
    scheduler.add_job(critical_pitching_check, 'interval', hours=12)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())