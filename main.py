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
load_dotenv()  # Загружаем .env если запускаем локально

API_TOKEN = os.getenv("BOT_TOKEN")
# Парсим админов из строки "123,456" в список чисел [123, 456]
admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in admin_env.split(",") if id.strip().isdigit()]
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Исправление URL для SQLAlchemy (нужен драйвер asyncpg)
if DATABASE_URL and not DATABASE_URL.startswith("postgresql+asyncpg"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ (PostgreSQL) ---
Base = declarative_base()

class UserRole(str, Enum):
    FOUNDER = "founder"
    AR = "ar"
    DESIGNER = "designer"
    SMM = "smm"

class User(Base):
    __tablename__ = 'users'
    # В Postgres лучше использовать BigInteger для ID телеграма, так как они большие
    id = Column(BigInteger, primary_key=True)  
    username = Column(String, nullable=True)
    role = Column(String)  # founder, ar, designer, smm
    full_name = Column(String, nullable=True)

class Artist(Base):
    __tablename__ = 'artists'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    manager_id = Column(BigInteger, ForeignKey('users.id'))
    
    # Флаги онбординга
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
    release_type = Column(String) # 80/20, 50/50
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

# Создание движка PostgreSQL
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден в .env")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# --- УТИЛИТЫ ---

async def upload_to_yandex_disk(file_path, filename):
    """Загрузка файла на Я.Диск (заглушка, если токена нет)"""
    if not YANDEX_DISK_TOKEN or "ВАШ_" in YANDEX_DISK_TOKEN:
        return f"https://fake-disk.url/{filename}"

    headers = {'Authorization': f'OAuth {YANDEX_DISK_TOKEN}'}
    try:
        # 1. Получаем URL для загрузки
        resp = requests.get(
            'https://cloud-api.yandex.net/v1/disk/resources/upload',
            params={'path': f'/MusicLabelBot/{filename}', 'overwrite': 'true'},
            headers=headers
        )
        if resp.status_code == 200:
            href = resp.json().get('href')
            # 2. Загружаем файл (в реальном коде file_path должен быть байтовым потоком)
            # Здесь упрощение для примера
            return "Файл успешно отправлен (эмуляция)"
    except Exception as e:
        logger.error(f"Ошибка Я.Диска: {e}")
    return None

# --- FSM СОСТОЯНИЯ ---
class ReleaseForm(StatesGroup):
    waiting_for_artist = State()
    waiting_for_title = State()
    waiting_for_type = State()
    waiting_for_date = State()

class TaskCompletion(StatesGroup):
    waiting_for_file = State()
    waiting_for_comment = State()

class NewArtist(StatesGroup):
    waiting_for_name = State()
    waiting_for_release_date = State()

# --- БОТ ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- MIDDLEWARE И AUTH ---

async def is_authorized(user_id):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        return user is not None

async def get_user_role(user_id):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        return user.role if user else None

# --- КЛАВИАТУРЫ ---

def get_main_menu(role):
    kb = []
    if role == UserRole.FOUNDER:
        kb = [
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Сотрудники")],
            [KeyboardButton(text="❌ Удалить релиз"), KeyboardButton(text="🚨 Алерт Питчинг")]
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
            [KeyboardButton(text="📱 Задачи SMM"), KeyboardButton(text="📝 Отчет")]
        ]
    
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- ОБРАБОТЧИКИ (HANDLERS) ---

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
                await message.answer("👑 Вы опознаны как Основатель (через ENV). Добро пожаловать.")
    
    if await is_authorized(user_id):
        role = await get_user_role(user_id)
        await message.answer(f"👋 С возвращением! Ваша роль: {role}", reply_markup=get_main_menu(role))
    else:
        await message.answer("⛔️ Доступ запрещен. Обратитесь к администратору для регистрации.")

# --- ЛОГИКА A&R (РЕЛИЗЫ) ---

@dp.message(F.text == "💿 Новый релиз")
async def start_release_creation(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    role = await get_user_role(user_id)
    
    if role != UserRole.AR and user_id not in ADMIN_IDS:
        return
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Artist))
        artists = result.scalars().all()
    
    if not artists:
        await message.answer("Сначала создайте артиста!")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a.name, callback_data=f"sel_art_{a.id}")] for a in artists
    ])
    
    await message.answer("Выберите основного артиста:", reply_markup=kb)
    await state.set_state(ReleaseForm.waiting_for_artist)

@dp.callback_query(F.data.startswith("sel_art_"))
async def process_artist_selection(callback: types.CallbackQuery, state: FSMContext):
    artist_id = int(callback.data.split("_")[2])
    await state.update_data(artist_id=artist_id)
    await callback.message.answer("Введите название релиза:")
    await state.set_state(ReleaseForm.waiting_for_title)

@dp.message(ReleaseForm.waiting_for_title)
async def process_release_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="80/20", callback_data="type_8020")],
        [InlineKeyboardButton(text="50/50", callback_data="type_5050")]
    ])
    await message.answer("Выберите тип сделки:", reply_markup=kb)
    await state.set_state(ReleaseForm.waiting_for_type)

@dp.callback_query(F.data.startswith("type_"))
async def process_release_type(callback: types.CallbackQuery, state: FSMContext):
    r_type = callback.data.split("_")[1]
    await state.update_data(release_type=r_type)
    await callback.message.answer("Введите дату релиза (формат ДД.ММ.ГГГГ):")
    await state.set_state(ReleaseForm.waiting_for_date)

@dp.message(ReleaseForm.waiting_for_date)
async def process_release_date(message: types.Message, state: FSMContext):
    try:
        date_obj = datetime.strptime(message.text, "%d.%m.%Y")
    except ValueError:
        await message.answer("Неверный формат. Попробуйте еще раз (ДД.ММ.ГГГГ).")
        return

    data = await state.get_data()
    
    async with AsyncSessionLocal() as session:
        new_release = Release(
            title=data['title'],
            artist_id=data['artist_id'],
            release_type=data['release_type'],
            release_date=date_obj,
            created_by=message.from_user.id
        )
        session.add(new_release)
        await session.flush() 
        
        # Шаблоны задач
        tasks_to_create = []
        tasks_to_create.append({
            "title": f"Загрузить трек {data['title']}", "role": UserRole.AR, 
            "delta": -14, "file": False
        })
        tasks_to_create.append({
            "title": f"Создать обложку для {data['title']}", "role": UserRole.DESIGNER, 
            "delta": -20, "file": True
        })
        
        if data['release_type'] == "8020":
            tasks_to_create.append({
                "title": f"Питчинг Spotify {data['title']}", "role": UserRole.AR, 
                "delta": -10, "file": False
            })

        for task_tmpl in tasks_to_create:
            result = await session.execute(select(User).where(User.role == task_tmpl['role']))
            worker = result.scalars().first()
            
            if worker:
                deadline = date_obj + timedelta(days=task_tmpl['delta'])
                new_task = Task(
                    title=task_tmpl['title'],
                    description="Автоматическая задача релиза",
                    status="pending",
                    deadline=deadline,
                    assigned_to=worker.id,
                    created_by=message.from_user.id,
                    release_id=new_release.id,
                    requires_file=task_tmpl['file']
                )
                session.add(new_task)
                try:
                    await bot.send_message(worker.id, f"🆕 Новая задача: {task_tmpl['title']}\nДедлайн: {deadline.strftime('%d.%m')}")
                except: pass

        await session.commit()
        
    await message.answer(f"✅ Релиз '{data['title']}' создан, задачи распределены!")
    await state.clear()

# --- УПРАВЛЕНИЕ ЗАДАЧАМИ ---

@dp.message(lambda m: m.text and ("Задачи" in m.text or "Мои задачи" in m.text))
async def show_tasks(message: types.Message):
    user_id = message.from_user.id
    
    async with AsyncSessionLocal() as session:
        stmt = select(Task).where(
            Task.assigned_to == user_id,
            Task.status.in_(['pending', 'in_progress', 'overdue'])
        ).order_by(Task.deadline)
        result = await session.execute(stmt)
        tasks = result.scalars().all()
    
    if not tasks:
        await message.answer("🎉 У вас нет активных задач!")
        return
    
    for task in tasks:
        status_icon = "🔥" if task.status == "overdue" else "⏳"
        deadline_str = task.deadline.strftime('%d.%m %H:%M') if task.deadline else "Без срока"
        text = f"{status_icon} <b>{task.title}</b>\nДедлайн: {deadline_str}"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Завершить задачу", callback_data=f"done_{task.id}")]
        ])
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("done_"))
async def complete_task_start(callback: types.CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.message.answer("Задача не найдена.")
            return

        if task.requires_file:
            await state.update_data(task_id=task_id)
            await callback.message.answer("📎 Для завершения этой задачи прикрепите файл.")
            await state.set_state(TaskCompletion.waiting_for_file)
        else:
            task.status = "done"
            await session.commit()
            await callback.message.edit_text(f"✅ Задача '{task.title}' выполнена!")
            if task.created_by:
                try:
                    await bot.send_message(task.created_by, f"✅ Задача '{task.title}' выполнена.")
                except: pass

@dp.message(TaskCompletion.waiting_for_file, F.document | F.photo)
async def process_file_upload(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data['task_id']
    
    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, task_id)
        task.status = "done"
        task.file_url = f"file_id:{file_id}" 
        await session.commit()
        
        await message.answer("✅ Файл получен, задача закрыта!")
        if task.created_by:
            try:
                 await bot.send_message(task.created_by, f"✅📎 Задача '{task.title}' выполнена, файл загружен.")
            except: pass
    
    await state.clear()

# --- ОНБОРДИНГ (A&R) ---

@dp.message(F.text == "🎤 Новый артист")
async def new_artist(message: types.Message, state: FSMContext):
    await message.answer("Введите имя артиста:")
    await state.set_state(NewArtist.waiting_for_name)

@dp.message(NewArtist.waiting_for_name)
async def new_artist_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Дата первого релиза (если известна, или 01.01.2026):")
    await state.set_state(NewArtist.waiting_for_release_date)

@dp.message(NewArtist.waiting_for_release_date)
async def new_artist_finish(message: types.Message, state: FSMContext):
    try:
        date = datetime.strptime(message.text, "%d.%m.%Y")
    except:
        date = None
    
    data = await state.get_data()
    async with AsyncSessionLocal() as session:
        artist = Artist(name=data['name'], manager_id=message.from_user.id, first_release_date=date)
        session.add(artist)
        await session.commit()
    
    await message.answer(f"Артист {data['name']} добавлен.")
    await state.clear()

@dp.callback_query(F.data.startswith("onb_"))
async def onboarding_response(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    action = parts[1]
    answer = parts[2]
    artist_id = int(parts[3])
    
    if answer == "no":
        await callback.message.edit_text("Понял, напомню позже.")
        return

    async with AsyncSessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        if not artist:
            return

        msg = "OK"
        if action == "contract":
            artist.contract_signed = True
            msg = "Договор отмечен как подписанный."
        elif action == "musixcreate":
            artist.musixmatch_created = True
            msg = "Профиль Musixmatch создан."
        
        await session.commit()
        await callback.message.edit_text(f"✅ {msg}")

# --- ПЛАНИРОВЩИК ---

async def check_overdue_tasks():
    async with AsyncSessionLocal() as session:
        now = datetime.now()
        stmt = select(Task).where(Task.deadline < now, Task.status.in_(['pending', 'in_progress']))
        result = await session.execute(stmt)
        overdue_tasks = result.scalars().all()
        
        for task in overdue_tasks:
            task.status = "overdue"
            try:
                await bot.send_message(task.assigned_to, f"⚠️ <b>ПРОСРОЧЕНО!</b>\nЗадача: {task.title}")
            except: pass
        
        await session.commit()

async def check_deadlines_approaching():
    async with AsyncSessionLocal() as session:
        now = datetime.now()
        tomorrow = now + timedelta(hours=24)
        stmt = select(Task).where(Task.deadline > now, Task.deadline <= tomorrow, Task.status != 'done')
        result = await session.execute(stmt)
        tasks = result.scalars().all()
        
        for task in tasks:
            try:
                await bot.send_message(task.assigned_to, f"⏰ <b>Скоро дедлайн!</b>\nЗадача: {task.title}")
            except: pass

async def onboarding_audit():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Artist))
        artists = result.scalars().all()
        
        for art in artists:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да", callback_data=f"onb_contract_yes_{art.id}"),
                 InlineKeyboardButton(text="Нет", callback_data=f"onb_contract_no_{art.id}")]
            ])
            
            if not art.contract_signed:
                try:
                    await bot.send_message(art.manager_id, f"📝 <b>Онбординг {art.name}</b>\nПодписан ли договор?", reply_markup=kb, parse_mode="HTML")
                except: pass

async def critical_pitching_check():
    async with AsyncSessionLocal() as session:
        target_date = datetime.now().date() + timedelta(days=3)
        stmt = select(Release).where(func.date(Release.release_date) == target_date)
        releases = (await session.execute(stmt)).scalars().all()
        
        for rel in releases:
            pitch_task = (await session.execute(select(Task).where(
                Task.release_id == rel.id, 
                Task.title.like("%Питчинг%"),
                Task.status != 'done'
            ))).scalars().first()
            
            if pitch_task:
                msg = f"🔥 <b>СРОЧНО! ПИТЧИНГ НЕ ГОТОВ!</b>\nРелиз: {rel.title}"
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, msg, parse_mode="HTML")
                    except: pass

# --- MAIN ---

async def main():
    # Создаем таблицы в Postgres (если их нет)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    scheduler.add_job(check_overdue_tasks, 'interval', hours=1)
    scheduler.add_job(check_deadlines_approaching, 'interval', hours=6)
    scheduler.add_job(onboarding_audit, 'cron', hour=15, minute=0)
    scheduler.add_job(critical_pitching_check, 'cron', hour=11, minute=0)
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped!")