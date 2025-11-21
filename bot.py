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
from aiogram.types import (
    KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, 
    InlineKeyboardMarkup, CallbackQuery, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sqlalchemy import select, func, desc, delete
from dotenv import load_dotenv

# Импорт БД
from database import (
    engine, async_session, Base, 
    User, Artist, Release, Task, Report,
    UserRole, TaskStatus, ReleaseType
)

# --- CONFIG ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id_str) for id_str in os.getenv("ADMIN_IDS", "").split(",") if id_str]
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- YANDEX DISK SERVICE ---
class YandexDiskService:
    BASE_URL = "https://cloud-api.yandex.net/v1/disk/resources"

    @staticmethod
    async def upload_file(file_url: str, filename: str, bot: Bot):
        """Загружает файл на Яндекс.Диск и возвращает публичную ссылку (или путь)"""
        if not YandexDisk_TOKEN or len(YandexDisk_TOKEN) < 10:
            # Fallback если токена нет
            logger.warning("Yandex Disk Token missing. Using mock.")
            return f"mock_storage/{filename}"
            
        headers = {"Authorization": f"OAuth {YandexDisk_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            # 1. Получаем URL для загрузки
            path = f"MusicAlligatorBot/{filename}"
            params = {"path": path, "overwrite": "true"}
            async with session.get(f"{YandexDiskService.BASE_URL}/upload", headers=headers, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"YD Get Upload URL Error: {await resp.text()}")
                    return None
                data = await resp.json()
                upload_href = data['href']
            
            # 2. Скачиваем файл из Telegram
            file_info = await bot.get_file(file_url)
            file_stream = await bot.download_file(file_info.file_path)

            # 3. Загружаем на Яндекс
            async with session.put(upload_href, data=file_stream) as resp:
                if resp.status != 201:
                    logger.error(f"YD Upload Error: {resp.status}")
                    return None
                
            # 4. Публикуем (опционально, для ссылки) или просто возвращаем путь
            return path

# --- STATES ---
class ReleaseState(StatesGroup):
    waiting_for_artist = State()
    waiting_for_title = State()
    waiting_for_type = State()
    waiting_for_date = State()

class CustomTaskState(StatesGroup):
    waiting_for_title = State()
    waiting_for_desc = State()
    waiting_for_assignee = State()
    waiting_for_deadline = State()

class TaskCompletionState(StatesGroup):
    waiting_for_file = State()

class ArtistState(StatesGroup):
    waiting_for_name = State()

class RoleState(StatesGroup):
    waiting_for_id = State()
    waiting_for_role_choice = State()

class SMMReportState(StatesGroup):
    waiting_for_text = State()

# --- BOT SETUP ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# --- TEMPLATES ---
RELEASE_TEMPLATES = {
    "all": [
        {"title": "📝 Загрузить на площадки", "role": UserRole.AR_MANAGER, "delta": -14, "file": False},
        {"title": "🎨 Сделать обложку", "role": UserRole.DESIGNER, "delta": -10, "file": True},
        {"title": "🎤 Запросить текст", "role": UserRole.AR_MANAGER, "delta": -15, "file": False},
        {"title": "⚖️ Проверить копирайты", "role": UserRole.FOUNDER, "delta": -5, "file": False}
    ],
    "pitching": {"title": "🚀 Питчинг в Spotify", "role": UserRole.AR_MANAGER, "delta": -14, "file": False}
}

# --- KEYBOARDS ---
def get_main_menu(role: str):
    builder = ReplyKeyboardBuilder()
    if role == UserRole.FOUNDER:
        builder.row(KeyboardButton(text="👥 Команда"), KeyboardButton(text="📊 Статистика"))
        builder.row(KeyboardButton(text="📀 Релизы"), KeyboardButton(text="➕ Создать задачу"))
    elif role == UserRole.AR_MANAGER:
        builder.row(KeyboardButton(text="🎤 Артисты"), KeyboardButton(text="📀 Релизы"))
        builder.row(KeyboardButton(text="➕ Новый Релиз"), KeyboardButton(text="➕ Добавить Артиста"))
        builder.row(KeyboardButton(text="➕ Создать задачу"))
    elif role == UserRole.DESIGNER:
        builder.row(KeyboardButton(text="🎨 Задачи по обложкам"))
    elif role == UserRole.SMM:
        builder.row(KeyboardButton(text="📝 Отчет за сегодня"), KeyboardButton(text="📅 Архив отчетов"))
    
    builder.row(KeyboardButton(text="📋 Мои Задачи"))
    return builder.as_markup(resize_keyboard=True)

# --- HANDLERS: START ---
@router.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    async with async_session() as session:
        if user_id in ADMIN_IDS:
            u = await session.get(User, user_id)
            if not u:
                session.add(User(id=user_id, full_name=message.from_user.full_name, role=UserRole.FOUNDER))
                await session.commit()
        
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            await message.answer("⛔ Нет доступа. ID: " + str(user_id))
            return
        
        await message.answer(f"👋 Привет, {user.full_name} ({user.role})", reply_markup=get_main_menu(user.role))

# --- TASK MANAGEMENT (MANUAL) ---
@router.message(F.text == "➕ Создать задачу")
async def create_task_start(message: types.Message, state: FSMContext):
    # Только для Основателей и AR
    async with async_session() as session:
        user = await session.get(User, message.from_user.id)
        if user.role not in [UserRole.FOUNDER, UserRole.AR_MANAGER]:
            await message.answer("⛔ Вам недоступно создание произвольных задач.")
            return
    
    await message.answer("✍️ Введите название задачи:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CustomTaskState.waiting_for_title)

@router.message(CustomTaskState.waiting_for_title)
async def custom_task_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("✍️ Введите описание задачи (или минус '-' если нет):")
    await state.set_state(CustomTaskState.waiting_for_desc)

@router.message(CustomTaskState.waiting_for_desc)
async def custom_task_desc(message: types.Message, state: FSMContext):
    desc_text = message.text if message.text != "-" else None
    await state.update_data(desc=desc_text)
    
    # Выбор исполнителя кнопками
    async with async_session() as session:
        users = (await session.execute(select(User).where(User.is_active == True))).scalars().all()
        kb = InlineKeyboardBuilder()
        for u in users:
            kb.button(text=f"{u.full_name} ({u.role})", callback_data=f"assign_{u.id}")
        kb.adjust(1)
        await message.answer("👤 Выберите исполнителя:", reply_markup=kb.as_markup())
        await state.set_state(CustomTaskState.waiting_for_assignee)

@router.callback_query(CustomTaskState.waiting_for_assignee, F.data.startswith("assign_"))
async def custom_task_assignee(callback: CallbackQuery, state: FSMContext):
    assignee_id = int(callback.data.split("_")[1])
    await state.update_data(assignee_id=assignee_id)
    await callback.message.edit_text("📅 Введите дедлайн (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.set_state(CustomTaskState.waiting_for_deadline)

@router.message(CustomTaskState.waiting_for_deadline)
async def custom_task_finish(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer("❌ Формат: ДД.ММ.ГГГГ ЧЧ:ММ (например 25.12.2025 18:00)")
        return

    data = await state.get_data()
    async with async_session() as session:
        task = Task(
            title=data['title'],
            description=data['desc'],
            status=TaskStatus.PENDING,
            deadline=dt,
            assignee_id=data['assignee_id'],
            creator_id=message.from_user.id
        )
        session.add(task)
        await session.commit()
        
        # Уведомление исполнителю
        try:
            await bot.send_message(data['assignee_id'], f"🆕 <b>Новая задача!</b>\n{data['title']}\nДедлайн: {dt}", parse_mode="HTML")
        except: pass
        
        user = await session.get(User, message.from_user.id)
        await message.answer("✅ Задача создана!", reply_markup=get_main_menu(user.role))
    await state.clear()

# --- TASK VIEWING & COMPLETION ---
@router.message(F.text.in_({"📋 Мои Задачи", "🎨 Задачи по обложкам"}))
async def show_tasks(message: types.Message):
    async with async_session() as session:
        # Меню фильтрации
        kb = InlineKeyboardBuilder()
        kb.button(text="🔥 Просроченные", callback_data="filter_overdue")
        kb.button(text="🟡 В работе", callback_data="filter_pending")
        kb.adjust(2)
        await message.answer("🔍 Какие задачи показать?", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("filter_"))
async def filter_tasks(callback: CallbackQuery):
    f_type = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    async with async_session() as session:
        query = select(Task).where(Task.assignee_id == user_id)
        
        if f_type == "overdue":
            query = query.where(Task.status == TaskStatus.OVERDUE)
        elif f_type == "pending":
            query = query.where(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]))
            
        query = query.order_by(Task.deadline)
        tasks = (await session.execute(query)).scalars().all()
        
        if not tasks:
            await callback.message.edit_text("🎉 Задач в этой категории нет!")
            return

        await callback.message.delete() # Удаляем меню фильтров
        
        for task in tasks:
            emoji = "🔴" if task.status == TaskStatus.OVERDUE else "🟡"
            desc_str = f"\n📄 {task.description}" if task.description else ""
            text = f"{emoji} <b>{task.title}</b>{desc_str}\n⏰ {task.deadline.strftime('%d.%m %H:%M')}"
            
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Завершить", callback_data=f"complete_{task.id}")
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("complete_"))
async def complete_task_click(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task: return
        
        if task.needs_file:
            await state.update_data(task_id=task_id)
            await state.set_state(TaskCompletionState.waiting_for_file)
            await callback.message.answer("📂 Прикрепите файл/картинку для завершения:")
            await callback.answer()
        else:
            task.status = TaskStatus.DONE
            await session.commit()
            await callback.message.edit_text(f"✅ Задача '{task.title}' закрыта!")
            # Уведомление создателю
            if task.creator_id != task.assignee_id:
                try: await bot.send_message(task.creator_id, f"✅ {callback.from_user.full_name} выполнил: {task.title}")
                except: pass

@router.message(TaskCompletionState.waiting_for_file, F.document | F.photo)
async def task_file_upload(message: types.Message, state: FSMContext):
    data = await state.get_data()
    task_id = data['task_id']
    
    file_obj = message.document or message.photo[-1]
    file_id = file_obj.file_id
    file_name = message.document.file_name if message.document else f"photo_{task_id}.jpg"
    
    msg = await message.answer("⏳ Выгрузка на Яндекс.Диск...")
    
    async with async_session() as session:
        task = await session.get(Task, task_id)
        
        # Загрузка
        yandex_path = await YandexDiskService.upload_file(file_id, file_name, bot)
        
        if yandex_path:
            task.file_url = yandex_path
            task.status = TaskStatus.DONE
            await session.commit()
            await msg.edit_text("✅ Файл загружен, задача выполнена!")
             # Уведомление создателю
            if task.creator_id != task.assignee_id:
                try: await bot.send_message(task.creator_id, f"✅ {message.from_user.full_name} прикрепил файл к задаче: {task.title}")
                except: pass
        else:
            await msg.edit_text("⚠️ Ошибка загрузки файла, но задачу пометим выполненной.")
            task.status = TaskStatus.DONE
            await session.commit()
            
    await state.clear()

# --- RELEASES & WORKFLOW ---
@router.message(F.text.in_({"📀 Релизы", "📀 Все релизы"}))
async def list_releases(message: types.Message):
    async with async_session() as session:
        # Список релизов с возможностью удаления (для основателя)
        query = select(Release, Artist.name).join(Artist).order_by(Release.release_date)
        result = (await session.execute(query)).all()
        
        if not result:
            await message.answer("📭 Релизов пока нет.")
            return

        user = await session.get(User, message.from_user.id)
        can_delete = user.role == UserRole.FOUNDER

        for rel, art_name in result:
            status = "✅ Вышел" if rel.release_date < datetime.now() else "⏳ Ожидается"
            txt = f"📀 <b>{art_name} - {rel.title}</b>\n📅 {rel.release_date.strftime('%d.%m.%Y')} | {status}"
            
            kb = InlineKeyboardBuilder()
            if can_delete:
                kb.button(text="🗑 Удалить", callback_data=f"delrel_{rel.id}")
            
            await message.answer(txt, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("delrel_"))
async def delete_release(callback: CallbackQuery):
    rel_id = int(callback.data.split("_")[1])
    async with async_session() as session:
        rel = await session.get(Release, rel_id)
        if rel:
            title = rel.title
            # Благодаря cascade="all, delete-orphan" в БД, задачи удалятся сами
            await session.delete(rel)
            await session.commit()
            await callback.answer("Релиз удален")
            await callback.message.edit_text(f"❌ Релиз '{title}' и все его задачи удалены.")
        else:
            await callback.answer("Релиз уже удален")

@router.message(F.text == "➕ Новый Релиз")
async def create_release_flow(message: types.Message, state: FSMContext):
    async with async_session() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
        if not artists:
            await message.answer("⚠️ Нет артистов. Сначала '➕ Добавить Артиста'")
            return
        
        kb = ReplyKeyboardBuilder()
        for a in artists: kb.button(text=a.name)
        kb.adjust(2)
        await message.answer("👤 Выберите артиста:", reply_markup=kb.as_markup(resize_keyboard=True))
        await state.set_state(ReleaseState.waiting_for_artist)

@router.message(ReleaseState.waiting_for_artist)
async def rel_artist(message: types.Message, state: FSMContext):
    async with async_session() as session:
        a = (await session.execute(select(Artist).where(Artist.name == message.text))).scalar_one_or_none()
        if not a:
            await message.answer("Выберите кнопкой!")
            return
        await state.update_data(aid=a.id)
    
    await message.answer("💿 Название релиза:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_title)

@router.message(ReleaseState.waiting_for_title)
async def rel_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    kb = ReplyKeyboardBuilder()
    for t in ReleaseType: kb.button(text=t.value)
    kb.adjust(1)
    await message.answer("💿 Тип релиза:", reply_markup=kb.as_markup(resize_keyboard=True))
    await state.set_state(ReleaseState.waiting_for_type)

@router.message(ReleaseState.waiting_for_type)
async def rel_type(message: types.Message, state: FSMContext):
    await state.update_data(rtype=message.text)
    await message.answer("📅 Дата (ДД.ММ.ГГГГ):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_date)

@router.message(ReleaseState.waiting_for_date)
async def rel_date(message: types.Message, state: FSMContext):
    try:
        d = datetime.strptime(message.text, "%d.%m.%Y")
    except:
        await message.answer("❌ Ошибка формата. Надо ДД.ММ.ГГГГ")
        return
    
    data = await state.get_data()
    async with async_session() as session:
        # 1. Создаем релиз
        rel = Release(title=data['title'], release_type=data['rtype'], artist_id=data['aid'], release_date=d, created_by=message.from_user.id)
        session.add(rel)
        await session.flush()
        
        # 2. Раздаем задачи
        designers = (await session.execute(select(User).where(User.role == UserRole.DESIGNER))).scalars().all()
        founders = (await session.execute(select(User).where(User.role == UserRole.FOUNDER))).scalars().all()
        
        def get_assignee(role):
            if role == UserRole.DESIGNER: return designers[0].id if designers else message.from_user.id
            if role == UserRole.FOUNDER: return founders[0].id if founders else message.from_user.id
            return message.from_user.id # AR Manager (себе)

        for t in RELEASE_TEMPLATES["all"]:
            deadline = d + timedelta(days=t['delta'])
            task = Task(
                title=f"{t['title']} - {data['title']}", description="Авто-задача релиза",
                status=TaskStatus.PENDING, deadline=deadline,
                assignee_id=get_assignee(t['role']), creator_id=message.from_user.id,
                release_id=rel.id, needs_file=t['file']
            )
            session.add(task)
            
        # Питчинг
        if (d - datetime.now()).days > 14:
            pt = RELEASE_TEMPLATES["pitching"]
            session.add(Task(
                title=f"{pt['title']} - {data['title']}", description="🔥 СРОЧНО",
                status=TaskStatus.PENDING, deadline=d + timedelta(days=pt['delta']),
                assignee_id=message.from_user.id, creator_id=message.from_user.id, release_id=rel.id
            ))
            
        await session.commit()
        
        u = await session.get(User, message.from_user.id)
        await message.answer(f"✅ Релиз '{data['title']}' создан, задачи розданы.", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- TEAM MANAGEMENT ---
@router.message(F.text.in_({"👥 Команда", "👥 Управление командой"}))
async def team_manage(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    
    async with async_session() as session:
        users = (await session.execute(select(User).order_by(User.role))).scalars().all()
        text = "🏢 <b>Состав команды:</b>\n\n"
        kb = InlineKeyboardBuilder()
        
        for u in users:
            text += f"👤 {u.full_name} — <b>{u.role}</b> (ID: {u.id})\n"
            kb.button(text=f"✏️ {u.full_name}", callback_data=f"editrole_{u.id}")
        
        kb.adjust(1)
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("editrole_"))
async def edit_role_ask(callback: CallbackQuery, state: FSMContext):
    uid = int(callback.data.split("_")[1])
    await state.update_data(uid=uid)
    
    kb = InlineKeyboardBuilder()
    for r in UserRole:
        kb.button(text=r.value, callback_data=f"setrole_{r.value}")
    kb.adjust(1)
    await callback.message.edit_text(f"Выберите новую роль для ID {uid}:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("setrole_"))
async def set_role_fin(callback: CallbackQuery, state: FSMContext):
    role = callback.data.split("_")[1]
    data = await state.get_data()
    
    async with async_session() as session:
        u = await session.get(User, data['uid'])
        if u:
            u.role = role
            await session.commit()
            await callback.message.edit_text(f"✅ Роль обновлена: {role}")
            try: await bot.send_message(u.id, f"🔄 Вам назначена новая роль: {role}. Нажмите /start")
            except: pass
    await state.clear()

# --- SMM REPORTS ---
@router.message(F.text == "📝 Отчет за сегодня")
async def smm_report_start(message: types.Message, state: FSMContext):
    await message.answer("✍️ Напишите отчет (что сделано):")
    await state.set_state(SMMReportState.waiting_for_text)

@router.message(SMMReportState.waiting_for_text)
async def smm_report_save(message: types.Message, state: FSMContext):
    async with async_session() as session:
        session.add(Report(user_id=message.from_user.id, text=message.text))
        await session.commit()
    await message.answer("✅ Отчет принят!")
    await state.clear()

@router.message(F.text == "📅 Архив отчетов")
async def smm_history(message: types.Message):
    await show_report_page(message, 0)

async def show_report_page(message, page):
    LIMIT = 5
    async with async_session() as session:
        offset = page * LIMIT
        reports = (await session.execute(
            select(Report).where(Report.user_id == message.from_user.id)
            .order_by(desc(Report.created_at)).offset(offset).limit(LIMIT)
        )).scalars().all()
        
        if not reports and page == 0:
            await message.answer("📭 Отчетов нет.")
            return
            
        text = f"📅 <b>Ваши отчеты (Стр. {page+1}):</b>\n\n"
        for r in reports:
            text += f"🔹 <i>{r.created_at.strftime('%d.%m %H:%M')}</i>: {r.text[:50]}...\n"
            
        kb = InlineKeyboardBuilder()
        if page > 0: kb.button(text="⬅️ Назад", callback_data=f"reppage_{page-1}")
        if len(reports) == LIMIT: kb.button(text="Вперед ➡️", callback_data=f"reppage_{page+1}")
        
        if isinstance(message, types.Message):
            await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("reppage_"))
async def smm_pagination(callback: CallbackQuery):
    page = int(callback.data.split("_")[1])
    await show_report_page(callback.message, page)

# --- ARTISTS ONBOARDING & STATS ---
@router.message(F.text == "➕ Добавить Артиста")
async def add_artist(message: types.Message, state: FSMContext):
    await message.answer("Имя артиста:")
    await state.set_state(ArtistState.waiting_for_name)

@router.message(ArtistState.waiting_for_name)
async def save_artist(message: types.Message, state: FSMContext):
    async with async_session() as session:
        session.add(Artist(name=message.text, ar_manager_id=message.from_user.id))
        await session.commit()
        u = await session.get(User, message.from_user.id)
    await message.answer("✅ Артист добавлен", reply_markup=get_main_menu(u.role))
    await state.clear()

@router.message(F.text == "📊 Статистика")
async def stats_view(message: types.Message):
    async with async_session() as session:
        rels = await session.scalar(select(func.count(Release.id)))
        tasks_done = await session.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.DONE))
        tasks_act = await session.scalar(select(func.count(Task.id)).where(Task.status != TaskStatus.DONE))
        await message.answer(f"📊 <b>Статистика:</b>\n📀 Релизов: {rels}\n✅ Закрыто задач: {tasks_done}\n⏳ В работе: {tasks_act}", parse_mode="HTML")

@router.callback_query(F.data.startswith("onb_"))
async def onb_answer(callback: CallbackQuery):
    _, aid, ctype, ans = callback.data.split("_")
    if ans == "yes":
        async with async_session() as session:
            a = await session.get(Artist, int(aid))
            if ctype == "contract": a.contract_signed = True
            elif ctype == "mm_create": a.musixmatch_profile = True
            elif ctype == "mm_verify": a.musixmatch_verified = True
            elif ctype == "yt_note": a.youtube_note = True
            await session.commit()
        await callback.message.edit_text("✅ Отмечено!")
    else:
        await callback.message.edit_text("🔔 Напомню позже")

# --- SCHEDULER JOBS ---
async def hourly_check():
    """Проверка просрочек и уведомления"""
    async with async_session() as session:
        # 1. Overdue
        overdue = (await session.execute(select(Task).where(Task.deadline < datetime.now(), Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])))).scalars().all()
        for t in overdue:
            t.status = TaskStatus.OVERDUE
            try: await bot.send_message(t.assignee_id, f"⚠️ <b>ПРОСРОЧЕНО!</b>\n{t.title}", parse_mode="HTML")
            except: pass
        await session.commit()

async def daily_checks():
    """Ежедневные проверки: Релизы, Онбординг, SMM"""
    async with async_session() as session:
        now = datetime.now()
        
        # 1. Релизы (1 и 2 дня до)
        upcoming = (await session.execute(select(Release).where(Release.release_date > now))).scalars().all()
        for r in upcoming:
            days = (r.release_date - now).days
            if days in [0, 1]: # 0 = завтра (если < 24ч), 1 = послезавтра
                msg = f"⏰ <b>Скоро релиз!</b>\n{r.title} через {days+1} дн."
                try: await bot.send_message(r.created_by, msg, parse_mode="HTML")
                except: pass

            # Питчинг алерт (3 дня)
            if days == 2: 
                pitch_task = (await session.execute(select(Task).where(Task.release_id == r.id, Task.title.like("%Питчинг%"), Task.status != TaskStatus.DONE))).scalar_one_or_none()
                if pitch_task:
                    founders = (await session.execute(select(User).where(User.role == UserRole.FOUNDER))).scalars().all()
                    for f in founders:
                        try: await bot.send_message(f.id, f"🔥 <b>АЛЕРТ ПИТЧИНГА!</b>\n{r.title} через 3 дня, питчинг не сдан!", parse_mode="HTML")
                        except: pass

        # 2. Онбординг
        artists = (await session.execute(select(Artist))).scalars().all()
        for a in artists:
            kb = InlineKeyboardBuilder()
            if not a.contract_signed:
                kb.button(text="Да", callback_data=f"onb_{a.id}_contract_yes")
                kb.button(text="Нет", callback_data=f"onb_{a.id}_contract_no")
                try: await bot.send_message(a.ar_manager_id, f"📝 Подписан контракт с {a.name}?", reply_markup=kb.as_markup())
                except: pass

# --- MAIN ---
async def main():
    # await engine.begin() ... # Раскомментировать если нужен сброс БД
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(hourly_check, IntervalTrigger(hours=1))
    scheduler.add_job(daily_checks, CronTrigger(hour=12)) # В 12:00 каждый день
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())