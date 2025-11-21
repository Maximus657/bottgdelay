import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F, types, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sqlalchemy import select, func
from dotenv import load_dotenv

# Импортируем базу данных из соседнего файла
from database import (
    engine, async_session, Base, 
    User, Artist, Release, Task, Report,
    UserRole, TaskStatus, ReleaseType
)

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id_str) for id_str in os.getenv("ADMIN_IDS", "").split(",") if id_str]
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- СЕРВИСЫ ---

class YandexDiskService:
    """Простая обертка для загрузки файлов"""
    BASE_URL = "https://cloud-api.yandex.net/v1/disk/resources"

    @staticmethod
    async def upload_file(file_url: str, destination_path: str, bot: Bot):
        # Заглушка, если токен не настроен, чтобы бот не падал
        if not YandexDisk_TOKEN or "ваш_токен" in YandexDisk_TOKEN:
            return f"mock_yandex_path/{destination_path}"
            
        headers = {"Authorization": f"OAuth {YandexDisk_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            params = {"path": f"MusicAlligatorBot/{destination_path}", "overwrite": "true"}
            async with session.get(f"{YandexDiskService.BASE_URL}/upload", headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"Yandex Disk Error: {await resp.text()}")
                    return None
                data = await resp.json()
                upload_href = data['href']
            
            file_info = await bot.get_file(file_url)
            file_stream = await bot.download_file(file_info.file_path)

            async with session.put(upload_href, data=file_stream) as resp:
                if resp.status == 201:
                    return f"MusicAlligatorBot/{destination_path}"
        return None

# --- ШАБЛОНЫ ЗАДАЧ ---
RELEASE_TEMPLATES = {
    "all": [
        {"title": "Загрузить на площадки", "role": UserRole.AR_MANAGER, "delta_days": -14, "file": False},
        {"title": "Сделать обложку", "role": UserRole.DESIGNER, "delta_days": -10, "file": True},
        {"title": "Запросить текст", "role": UserRole.AR_MANAGER, "delta_days": -15, "file": False},
        {"title": "Проверить копирайты", "role": UserRole.FOUNDER, "delta_days": -5, "file": False}
    ],
    "pitching": {"title": "Питчинг в Spotify", "role": UserRole.AR_MANAGER, "delta_days": -14, "file": False}
}

# --- FSM SATES ---
class ReleaseState(StatesGroup):
    waiting_for_artist = State()
    waiting_for_title = State()
    waiting_for_type = State()
    waiting_for_date = State()
    confirm = State()

class TaskState(StatesGroup):
    waiting_for_title = State()
    waiting_for_desc = State()
    waiting_for_role = State()
    waiting_for_deadline = State()
    waiting_for_file = State()

class ArtistState(StatesGroup):
    waiting_for_name = State()

# --- БОТ И ДИСПЕТЧЕР ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# --- КЛАВИАТУРЫ ---
def get_main_menu(role: str):
    builder = ReplyKeyboardBuilder()
    if role == UserRole.FOUNDER:
        builder.row(KeyboardButton(text="👥 Управление командой"), KeyboardButton(text="📊 Статистика"))
        builder.row(KeyboardButton(text="📀 Все релизы"), KeyboardButton(text="➕ Создать задачу"))
    elif role == UserRole.AR_MANAGER:
        builder.row(KeyboardButton(text="🎤 Мои Артисты"), KeyboardButton(text="➕ Новый Релиз"))
        builder.row(KeyboardButton(text="➕ Добавить Артиста"))
    elif role == UserRole.DESIGNER:
        builder.row(KeyboardButton(text="🎨 Задачи по обложкам"))
    elif role == UserRole.SMM:
        builder.row(KeyboardButton(text="📝 Отправить отчет"), KeyboardButton(text="📅 История отчетов"))
    
    builder.row(KeyboardButton(text="📋 Мои Задачи"))
    return builder.as_markup(resize_keyboard=True)

def get_task_actions(task_id: int, status: str, needs_file: bool):
    builder = InlineKeyboardBuilder()
    if status != TaskStatus.DONE:
        builder.button(text="✅ Завершить", callback_data=f"complete_{task_id}")
    return builder.as_markup()

def get_onboarding_kb(artist_id: int, check_type: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data=f"onb_{artist_id}_{check_type}_yes")
    builder.button(text="❌ Нет", callback_data=f"onb_{artist_id}_{check_type}_no")
    return builder.as_markup()

# --- HANDLERS: AUTH & MENU ---

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    async with async_session() as session:
        # Авто-регистрация админов
        if user_id in ADMIN_IDS:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                user = User(id=user_id, username=message.from_user.username, full_name=message.from_user.full_name, role=UserRole.FOUNDER)
                session.add(user)
                await session.commit()
                await message.answer("👋 Привет, Основатель! Вы зарегистрированы.")
        
        # Проверка обычного пользователя
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user or not user.is_active:
            await message.answer("⛔ Доступ запрещен. Обратитесь к администратору.")
            return

        await message.answer(f"Добро пожаловать, {user.role}!", reply_markup=get_main_menu(user.role))

# --- HANDLERS: TASKS ---

@router.message(F.text == "📋 Мои Задачи")
async def show_my_tasks(message: types.Message):
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.assignee_id == message.from_user.id).where(Task.status != TaskStatus.DONE).order_by(Task.deadline)
        )
        tasks = result.scalars().all()
        
        if not tasks:
            await message.answer("🎉 У вас нет активных задач!")
            return
            
        for task in tasks:
            deadline_str = task.deadline.strftime("%d.%m %H:%M")
            emoji = "🔴" if task.status == TaskStatus.OVERDUE else "🟡"
            text = f"{emoji} <b>{task.title}</b>\n📄 {task.description or ''}\n⏰ Дедлайн: {deadline_str}"
            await message.answer(text, parse_mode="HTML", reply_markup=get_task_actions(task.id, task.status, task.needs_file))

@router.callback_query(F.data.startswith("complete_"))
async def process_complete_task(callback: types.CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            await callback.answer("Задача не найдена")
            return
            
        if task.needs_file:
            await state.update_data(task_id=task_id)
            await state.set_state(TaskState.waiting_for_file)
            await callback.message.answer("📂 Для завершения этой задачи прикрепите файл (документ или фото).")
            await callback.answer()
        else:
            task.status = TaskStatus.DONE
            await session.commit()
            await callback.message.edit_text(f"✅ Задача '{task.title}' выполнена!")
            # Уведомление создателю
            if task.creator_id != task.assignee_id:
                try:
                    await bot.send_message(task.creator_id, f"✅ Пользователь {callback.from_user.full_name} выполнил задачу: {task.title}")
                except: pass

@router.message(TaskState.waiting_for_file, F.document | F.photo)
async def process_task_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data['task_id']
    
    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    msg = await message.answer("⏳ Загрузка на Яндекс.Диск...")
    
    async with async_session() as session:
        task = await session.get(Task, task_id)
        yandex_path = await YandexDiskService.upload_file(file_id, f"task_{task_id}_{message.message_id}", bot)
        
        if yandex_path:
            task.file_url = yandex_path
            task.status = TaskStatus.DONE
            await session.commit()
            await msg.edit_text(f"✅ Файл загружен! Задача '{task.title}' выполнена.")
            try:
                if task.creator_id != task.assignee_id:
                    await bot.send_message(task.creator_id, f"✅ Пользователь {message.from_user.full_name} выполнил задачу с файлом: {task.title}")
            except: pass
        else:
            await msg.edit_text("❌ Ошибка загрузки файла.")
    await state.clear()

# --- HANDLERS: RELEASES & WORKFLOW ---

@router.message(F.text == "➕ Новый Релиз")
async def new_release_start(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await session.get(User, message.from_user.id)
        if user.role not in [UserRole.AR_MANAGER, UserRole.FOUNDER]:
            return
        artists = (await session.execute(select(Artist))).scalars().all()
        if not artists:
            await message.answer("Сначала добавьте артистов!")
            return

        kb = ReplyKeyboardBuilder()
        for artist in artists:
            kb.button(text=artist.name)
        kb.adjust(2)
        
        await message.answer("Выберите основного артиста:", reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True))
        await state.set_state(ReleaseState.waiting_for_artist)

@router.message(ReleaseState.waiting_for_artist)
async def release_artist_chosen(message: types.Message, state: FSMContext):
    async with async_session() as session:
        artist = (await session.execute(select(Artist).where(Artist.name == message.text))).scalar_one_or_none()
        if not artist:
            await message.answer("Артист не найден.")
            return
        await state.update_data(artist_id=artist.id)
        await message.answer("Введите название релиза:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(ReleaseState.waiting_for_title)

@router.message(ReleaseState.waiting_for_title)
async def release_title_chosen(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    kb = ReplyKeyboardBuilder()
    for t in ReleaseType:
        kb.button(text=t.value)
    kb.adjust(1)
    await message.answer("Выберите тип релиза:", reply_markup=kb.as_markup(resize_keyboard=True))
    await state.set_state(ReleaseState.waiting_for_type)

@router.message(ReleaseState.waiting_for_type)
async def release_type_chosen(message: types.Message, state: FSMContext):
    await state.update_data(r_type=message.text)
    await message.answer("Введите дату релиза (ДД.ММ.ГГГГ):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_date)

@router.message(ReleaseState.waiting_for_date)
async def release_date_chosen(message: types.Message, state: FSMContext):
    try:
        date = datetime.strptime(message.text, "%d.%m.%Y")
    except ValueError:
        await message.answer("Неверный формат. Используйте ДД.ММ.ГГГГ")
        return
    data = await state.get_data()
    
    async with async_session() as session:
        new_release = Release(
            title=data['title'], release_type=data['r_type'], artist_id=data['artist_id'],
            release_date=date, created_by=message.from_user.id
        )
        session.add(new_release)
        await session.flush()
        
        # Генерация задач
        designers = (await session.execute(select(User.id).where(User.role == UserRole.DESIGNER))).scalars().all()
        founders = (await session.execute(select(User.id).where(User.role == UserRole.FOUNDER))).scalars().all()
        
        assignee_map = {
            UserRole.AR_MANAGER: message.from_user.id,
            UserRole.DESIGNER: designers[0] if designers else message.from_user.id,
            UserRole.FOUNDER: founders[0] if founders else message.from_user.id
        }

        for tmpl in RELEASE_TEMPLATES["all"]:
            deadline = date + timedelta(days=tmpl['delta_days'])
            task = Task(
                title=f"{tmpl['title']} ({data['title']})",
                description=f"Автоматическая задача", status=TaskStatus.PENDING,
                deadline=deadline, assignee_id=assignee_map.get(tmpl['role'], message.from_user.id),
                creator_id=message.from_user.id, release_id=new_release.id, needs_file=tmpl['file']
            )
            session.add(task)

        if (date - datetime.now()).days > 14:
             pt = RELEASE_TEMPLATES["pitching"]
             task = Task(
                title=f"{pt['title']} ({data['title']})", description="Критически важно!",
                status=TaskStatus.PENDING, deadline=date + timedelta(days=pt['delta_days']),
                assignee_id=message.from_user.id, creator_id=message.from_user.id, release_id=new_release.id
             )
             session.add(task)

        await session.commit()
        await message.answer(f"✅ Релиз '{data['title']}' создан!")
    await state.clear()

# --- HANDLERS: ONBOARDING ARTIST ---
@router.message(F.text == "➕ Добавить Артиста")
async def add_artist_start(message: types.Message, state: FSMContext):
    await message.answer("Введите имя артиста:")
    await state.set_state(ArtistState.waiting_for_name)

@router.message(ArtistState.waiting_for_name)
async def add_artist_finish(message: types.Message, state: FSMContext):
    async with async_session() as session:
        artist = Artist(name=message.text, ar_manager_id=message.from_user.id)
        session.add(artist)
        await session.commit()
    await message.answer(f"✅ Артист {message.text} добавлен.")
    await state.clear()

@router.callback_query(F.data.startswith("onb_"))
async def process_onboarding_response(callback: types.CallbackQuery):
    _, artist_id, check_type, answer = callback.data.split("_")
    if answer == "no":
        await callback.message.edit_text("Ок, напомню позже.")
        return
    async with async_session() as session:
        artist = await session.get(Artist, int(artist_id))
        if not artist: return
        
        msg = "✅ Обновлено"
        if check_type == "contract": artist.contract_signed = True
        elif check_type == "mm_create": artist.musixmatch_profile = True
        elif check_type == "mm_verify": artist.musixmatch_verified = True
        elif check_type == "yt_note": artist.youtube_note = True
        elif check_type == "yt_bind": artist.youtube_binding = True
        
        await session.commit()
        await callback.message.edit_text(msg)

# --- ADMIN HANDLERS ---
@router.message(F.text == "👥 Управление командой")
async def team_management(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with async_session() as session:
        users = (await session.execute(select(User))).scalars().all()
        text = "Команда:\n" + "\n".join([f"ID: `{u.id}` | {u.full_name} | {u.role}" for u in users])
        text += "\n\nКоманда: /setrole ID РОЛЬ"
    await message.answer(text, parse_mode="Markdown")

@router.message(Command("setrole"))
async def set_role_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    try:
        _, target_id, role_text = message.text.split(maxsplit=2)
        if role_text not in [r.value for r in UserRole]:
            await message.answer("Неверная роль.")
            return
        async with async_session() as session:
            user = await session.get(User, int(target_id))
            if user:
                user.role = role_text
                await session.commit()
                await message.answer(f"✅ Роль изменена на {role_text}")
                try: await bot.send_message(int(target_id), f"🔄 Ваша роль: {role_text}. Напишите /start")
                except: pass
            else:
                await message.answer("Пользователь не найден.")
    except:
        await message.answer("Ошибка. Пример: /setrole 123456789 Дизайнер")

# --- SCHEDULER ---
async def check_overdue_tasks():
    async with async_session() as session:
        tasks = (await session.execute(select(Task).where(Task.deadline < datetime.now(), Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])))).scalars().all()
        for task in tasks:
            task.status = TaskStatus.OVERDUE
            try: await bot.send_message(task.assignee_id, f"⚠️ <b>ПРОСРОЧЕНО!</b>\n{task.title}", parse_mode="HTML")
            except: pass
        await session.commit()

async def check_onboarding():
    async with async_session() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
        for artist in artists:
            if not artist.contract_signed:
                await bot.send_message(artist.ar_manager_id, f"📝 Контракт с {artist.name} подписан?", reply_markup=get_onboarding_kb(artist.id, "contract"))

# --- MAIN ---
async def main():
    # 1. Работа с БД
    async with engine.begin() as conn:
        # !!! ВОТ ЭТО ИСПРАВЛЯЕТ ОШИБКУ !!!
        # Эта команда удалит старые таблицы и создаст новые с правильной колонкой is_active
        await conn.run_sync(Base.metadata.drop_all) 
        await conn.run_sync(Base.metadata.create_all)
        print("✅ База данных пересоздана успешно.")

    # 2. Запуск планировщика
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_overdue_tasks, IntervalTrigger(hours=1))
    scheduler.add_job(check_onboarding, CronTrigger(hour=15))
    scheduler.start()
    
    # 3. Запуск бота
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32':
         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())