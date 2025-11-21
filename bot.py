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
    InlineKeyboardMarkup, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sqlalchemy import select, func, desc
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
        if not YandexDisk_TOKEN or len(YandexDisk_TOKEN) < 10:
            return f"mock_storage/{filename}"
        headers = {"Authorization": f"OAuth {YandexDisk_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            path = f"MusicAlligatorBot/{filename}"
            params = {"path": path, "overwrite": "true"}
            async with session.get(f"{YandexDiskService.BASE_URL}/upload", headers=headers, params=params) as resp:
                if resp.status != 200: return None
                data = await resp.json()
                upload_href = data['href']
            
            file_info = await bot.get_file(file_url)
            file_stream = await bot.download_file(file_info.file_path)
            async with session.put(upload_href, data=file_stream) as resp:
                if resp.status != 201: return None
            return path

# --- STATES ---
class ReleaseState(StatesGroup):
    waiting_for_artist = State()
    waiting_for_feat = State()  # НОВОЕ
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

class AddUserState(StatesGroup):
    waiting_for_id = State()
    waiting_for_role = State()

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

# --- HANDLERS: START & AUTH ---
@router.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    async with async_session() as session:
        # Проверка админов из ENV
        if user_id in ADMIN_IDS:
            u = await session.get(User, user_id)
            if not u:
                session.add(User(id=user_id, full_name=message.from_user.full_name, role=UserRole.FOUNDER))
                await session.commit()
        
        # Поиск пользователя
        user = await session.get(User, user_id)
        
        # Если пользователя нет или он не активен
        if not user or not user.is_active:
            await message.answer(f"⛔ Доступ запрещен.\nВаш ID: <code>{user_id}</code>\nОтправьте этот ID администратору для регистрации.", parse_mode="HTML")
            return
            
        # Обновляем данные о пользователе (если сменил ник)
        user.username = message.from_user.username
        user.full_name = message.from_user.full_name
        await session.commit()
        
        await message.answer(f"👋 Добро пожаловать, {user.full_name}!", reply_markup=get_main_menu(user.role))

# --- TEAM MANAGEMENT (ADD USER FEATURE) ---
@router.message(F.text.in_({"👥 Команда", "👥 Управление командой"}))
async def team_manage(message: types.Message):
    async with async_session() as session:
        # Проверка прав
        cur_user = await session.get(User, message.from_user.id)
        if cur_user.role != UserRole.FOUNDER: return
        
        users = (await session.execute(select(User).order_by(User.role))).scalars().all()
        text = "🏢 <b>Состав команды:</b>\n\n"
        kb = InlineKeyboardBuilder()
        
        for u in users:
            text += f"👤 {u.full_name} — <b>{u.role}</b> (ID: <code>{u.id}</code>)\n"
            kb.button(text=f"✏️ Изм. роль: {u.full_name}", callback_data=f"editrole_{u.id}")
        
        # Кнопка добавления нового сотрудника
        kb.button(text="➕ Добавить сотрудника", callback_data="add_new_user")
        kb.adjust(1)
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data == "add_new_user")
async def add_user_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🆔 Введите Telegram ID нового сотрудника (цифры):")
    await state.set_state(AddUserState.waiting_for_id)
    await callback.answer()

@router.message(AddUserState.waiting_for_id)
async def add_user_id(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text)
        await state.update_data(new_uid=uid)
        
        kb = InlineKeyboardBuilder()
        for r in UserRole:
            kb.button(text=r.value, callback_data=f"newrole_{r.value}")
        kb.adjust(1)
        
        await message.answer("👤 Выберите роль для сотрудника:", reply_markup=kb.as_markup())
        await state.set_state(AddUserState.waiting_for_role)
    except ValueError:
        await message.answer("❌ ID должен состоять только из цифр.")

@router.callback_query(F.data.startswith("newrole_"), AddUserState.waiting_for_role)
async def add_user_finish(callback: CallbackQuery, state: FSMContext):
    role = callback.data.split("_")[1]
    data = await state.get_data()
    uid = data['new_uid']
    
    async with async_session() as session:
        existing = await session.get(User, uid)
        if existing:
            existing.role = role
            existing.is_active = True
            await callback.message.edit_text(f"✅ Пользователь {uid} уже был в базе. Роль обновлена на {role}.")
        else:
            # Создаем пользователя (имя обновится когда он нажмет /start)
            session.add(User(id=uid, full_name="Ожидание входа...", role=role, is_active=True))
            await callback.message.edit_text(f"✅ Пользователь {uid} добавлен с ролью {role}.\nТеперь он может нажать /start")
        await session.commit()
    await state.clear()

# Редактирование ролей (старое)
@router.callback_query(F.data.startswith("editrole_"))
async def edit_role_ask(callback: CallbackQuery, state: FSMContext):
    uid = int(callback.data.split("_")[1])
    await state.update_data(uid=uid)
    kb = InlineKeyboardBuilder()
    for r in UserRole: kb.button(text=r.value, callback_data=f"setrole_{r.value}")
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
    await state.clear()

# --- CUSTOM TASKS (MANUAL) ---
@router.message(F.text == "➕ Создать задачу")
async def create_task_start(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = await session.get(User, message.from_user.id)
        if user.role not in [UserRole.FOUNDER, UserRole.AR_MANAGER]:
            await message.answer("⛔ Нет прав.")
            return
    
    await message.answer("✍️ Название задачи:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CustomTaskState.waiting_for_title)

@router.message(CustomTaskState.waiting_for_title)
async def custom_task_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("✍️ Описание (или минус '-'):")
    await state.set_state(CustomTaskState.waiting_for_desc)

@router.message(CustomTaskState.waiting_for_desc)
async def custom_task_desc(message: types.Message, state: FSMContext):
    desc_text = message.text if message.text != "-" else None
    await state.update_data(desc=desc_text)
    
    async with async_session() as session:
        users = (await session.execute(select(User).where(User.is_active == True))).scalars().all()
        kb = InlineKeyboardBuilder()
        for u in users: kb.button(text=f"{u.full_name} ({u.role})", callback_data=f"assign_{u.id}")
        kb.adjust(1)
        await message.answer("👤 Исполнитель:", reply_markup=kb.as_markup())
        await state.set_state(CustomTaskState.waiting_for_assignee)

@router.callback_query(CustomTaskState.waiting_for_assignee, F.data.startswith("assign_"))
async def custom_task_assignee(callback: CallbackQuery, state: FSMContext):
    assignee_id = int(callback.data.split("_")[1])
    await state.update_data(assignee_id=assignee_id)
    # ИЗМЕНЕНО: ТОЛЬКО ДАТА
    await callback.message.edit_text("📅 Дедлайн (ДД.ММ.ГГГГ):") 
    await state.set_state(CustomTaskState.waiting_for_deadline)

@router.message(CustomTaskState.waiting_for_deadline)
async def custom_task_finish(message: types.Message, state: FSMContext):
    try:
        # Берем только дату и ставим время 23:59
        date_only = datetime.strptime(message.text, "%d.%m.%Y")
        dt = date_only.replace(hour=23, minute=59)
    except ValueError:
        await message.answer("❌ Формат только ДД.ММ.ГГГГ (пример: 25.12.2025)")
        return

    data = await state.get_data()
    async with async_session() as session:
        task = Task(
            title=data['title'], description=data['desc'], status=TaskStatus.PENDING,
            deadline=dt, assignee_id=data['assignee_id'], creator_id=message.from_user.id
        )
        session.add(task)
        await session.commit()
        try: await bot.send_message(data['assignee_id'], f"🆕 <b>Новая задача!</b>\n{data['title']}\n📅 {message.text}", parse_mode="HTML")
        except: pass
        
        u = await session.get(User, message.from_user.id)
        await message.answer("✅ Задача создана!", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- RELEASE WORKFLOW (WITH FEAT) ---
@router.message(F.text == "➕ Новый Релиз")
async def create_release_flow(message: types.Message, state: FSMContext):
    async with async_session() as session:
        artists = (await session.execute(select(Artist))).scalars().all()
        if not artists:
            await message.answer("⚠️ Нет артистов.")
            return
        kb = ReplyKeyboardBuilder()
        for a in artists: kb.button(text=a.name)
        kb.adjust(2)
        await message.answer("👤 Основной артист:", reply_markup=kb.as_markup(resize_keyboard=True))
        await state.set_state(ReleaseState.waiting_for_artist)

@router.message(ReleaseState.waiting_for_artist)
async def rel_artist(message: types.Message, state: FSMContext):
    async with async_session() as session:
        a = (await session.execute(select(Artist).where(Artist.name == message.text))).scalar_one_or_none()
        if not a:
            await message.answer("Выберите кнопкой!")
            return
        await state.update_data(aid=a.id)
    
    await message.answer("👯 Со-артисты (feat)? Напишите имена или '-' если нет:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_feat)

@router.message(ReleaseState.waiting_for_feat)
async def rel_feat(message: types.Message, state: FSMContext):
    feats = message.text if message.text != "-" else None
    await state.update_data(feats=feats)
    await message.answer("💿 Название релиза:")
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
        await message.answer("❌ Ошибка формата. ДД.ММ.ГГГГ")
        return
    
    data = await state.get_data()
    async with async_session() as session:
        # Создаем релиз с Feat
        rel = Release(
            title=data['title'], 
            feat_artists=data['feats'],
            release_type=data['rtype'], 
            artist_id=data['aid'], 
            release_date=d, 
            created_by=message.from_user.id
        )
        session.add(rel)
        await session.flush()
        
        # Раздаем задачи
        designers = (await session.execute(select(User).where(User.role == UserRole.DESIGNER))).scalars().all()
        founders = (await session.execute(select(User).where(User.role == UserRole.FOUNDER))).scalars().all()
        
        def get_assignee(role):
            if role == UserRole.DESIGNER: return designers[0].id if designers else message.from_user.id
            if role == UserRole.FOUNDER: return founders[0].id if founders else message.from_user.id
            return message.from_user.id

        title_full = f"{data['title']}"
        if data['feats']: title_full += f" (feat. {data['feats']})"

        for t in RELEASE_TEMPLATES["all"]:
            deadline = d + timedelta(days=t['delta'])
            task = Task(
                title=f"{t['title']} - {title_full}", description="Авто-задача",
                status=TaskStatus.PENDING, deadline=deadline,
                assignee_id=get_assignee(t['role']), creator_id=message.from_user.id,
                release_id=rel.id, needs_file=t['file']
            )
            session.add(task)
            
        if (d - datetime.now()).days > 14:
            pt = RELEASE_TEMPLATES["pitching"]
            session.add(Task(
                title=f"{pt['title']} - {title_full}", description="🔥 СРОЧНО",
                status=TaskStatus.PENDING, deadline=d + timedelta(days=pt['delta']),
                assignee_id=message.from_user.id, creator_id=message.from_user.id, release_id=rel.id
            ))
            
        await session.commit()
        u = await session.get(User, message.from_user.id)
        await message.answer(f"✅ Релиз '{title_full}' создан!", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- TASKS, SMM & REPORT (Restored Logic) ---
@router.message(F.text.in_({"📋 Мои Задачи", "🎨 Задачи по обложкам"}))
async def show_tasks(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Просроченные", callback_data="filter_overdue")
    kb.button(text="🟡 В работе", callback_data="filter_pending")
    kb.adjust(2)
    await message.answer("🔍 Фильтр:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("filter_"))
async def filter_tasks(callback: CallbackQuery):
    f_type = callback.data.split("_")[1]
    async with async_session() as session:
        q = select(Task).where(Task.assignee_id == callback.from_user.id)
        if f_type == "overdue": q = q.where(Task.status == TaskStatus.OVERDUE)
        elif f_type == "pending": q = q.where(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]))
        tasks = (await session.execute(q.order_by(Task.deadline))).scalars().all()
        
        if not tasks:
            await callback.message.edit_text("🎉 Задач нет!")
            return
        
        await callback.message.delete()
        for t in tasks:
            icon = "🔴" if t.status == TaskStatus.OVERDUE else "🟡"
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Завершить", callback_data=f"complete_{t.id}")
            await callback.message.answer(f"{icon} <b>{t.title}</b>\n⏰ {t.deadline.strftime('%d.%m')}", parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("complete_"))
async def complete_task(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split("_")[1])
    async with async_session() as session:
        t = await session.get(Task, tid)
        if not t: return
        if t.needs_file:
            await state.update_data(tid=tid)
            await state.set_state(TaskCompletionState.waiting_for_file)
            await callback.message.answer("📂 Прикрепите файл:")
            await callback.answer()
        else:
            t.status = TaskStatus.DONE
            await session.commit()
            await callback.message.edit_text(f"✅ Выполнено: {t.title}")
            if t.creator_id != t.assignee_id:
                try: await bot.send_message(t.creator_id, f"✅ Задача завершена: {t.title}")
                except: pass

@router.message(TaskCompletionState.waiting_for_file, F.document | F.photo)
async def upload_task_file(message: types.Message, state: FSMContext):
    data = await state.get_data()
    file_obj = message.document or message.photo[-1]
    msg = await message.answer("⏳ Загрузка...")
    async with async_session() as session:
        t = await session.get(Task, data['tid'])
        path = await YandexDiskService.upload_file(file_obj.file_id, f"task_{t.id}_{file_obj.file_unique_id}", bot)
        t.file_url = path
        t.status = TaskStatus.DONE
        await session.commit()
        await msg.edit_text("✅ Файл загружен, задача закрыта!")
    await state.clear()

@router.message(F.text == "📝 Отчет за сегодня")
async def smm_rep(message: types.Message, state: FSMContext):
    await message.answer("✍️ Текст отчета:")
    await state.set_state(SMMReportState.waiting_for_text)

@router.message(SMMReportState.waiting_for_text)
async def smm_save(message: types.Message, state: FSMContext):
    async with async_session() as session:
        session.add(Report(user_id=message.from_user.id, text=message.text))
        await session.commit()
    await message.answer("✅ Принято.")
    await state.clear()

@router.message(F.text == "📀 Релизы")
async def list_rels(message: types.Message):
    async with async_session() as session:
        res = (await session.execute(select(Release, Artist.name).join(Artist).order_by(Release.release_date))).all()
        if not res:
            await message.answer("📭 Пусто.")
            return
        user = await session.get(User, message.from_user.id)
        can_del = user.role == UserRole.FOUNDER
        
        for r, aname in res:
            feat_str = f" (feat. {r.feat_artists})" if r.feat_artists else ""
            kb = InlineKeyboardBuilder()
            if can_del: kb.button(text="🗑 Удалить", callback_data=f"delrel_{r.id}")
            await message.answer(f"💿 <b>{aname}{feat_str} - {r.title}</b>\n📅 {r.release_date.strftime('%d.%m.%Y')}", parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("delrel_"))
async def del_rel(callback: CallbackQuery):
    rid = int(callback.data.split("_")[1])
    async with async_session() as session:
        r = await session.get(Release, rid)
        if r:
            await session.delete(r)
            await session.commit()
            await callback.message.edit_text("❌ Релиз удален.")

@router.message(F.text == "➕ Добавить Артиста")
async def add_artist(message: types.Message, state: FSMContext):
    await message.answer("Имя артиста:")
    await state.set_state(ArtistState.waiting_for_name)

@router.message(ArtistState.waiting_for_name)
async def save_artist(message: types.Message, state: FSMContext):
    async with async_session() as session:
        session.add(Artist(name=message.text, ar_manager_id=message.from_user.id))
        await session.commit()
    await message.answer("✅ Готово.", reply_markup=get_main_menu(UserRole.AR_MANAGER))
    await state.clear()

@router.message(F.text == "📊 Статистика")
async def stats(message: types.Message):
    async with async_session() as session:
        c_rel = await session.scalar(select(func.count(Release.id)))
        c_done = await session.scalar(select(func.count(Task.id)).where(Task.status==TaskStatus.DONE))
        await message.answer(f"📊 Релизов: {c_rel}\n✅ Задач закрыто: {c_done}")

@router.callback_query(F.data.startswith("onb_"))
async def onb_cb(callback: CallbackQuery):
    _, aid, ctype, ans = callback.data.split("_")
    if ans == "yes":
        async with async_session() as session:
            a = await session.get(Artist, int(aid))
            if ctype == "contract": a.contract_signed = True
            elif ctype == "mm_create": a.musixmatch_profile = True
            elif ctype == "mm_verify": a.musixmatch_verified = True
            elif ctype == "yt_note": a.youtube_note = True
            await session.commit()
        await callback.message.edit_text("✅ Отмечено.")
    else:
        await callback.message.edit_text("🔔 Ок, позже.")

# --- SCHEDULER ---
async def checks():
    async with async_session() as session:
        now = datetime.now()
        # Просрочка
        ov = (await session.execute(select(Task).where(Task.deadline < now, Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])))).scalars().all()
        for t in ov:
            t.status = TaskStatus.OVERDUE
            try: await bot.send_message(t.assignee_id, f"⚠️ ПРОСРОЧЕНО: {t.title}")
            except: pass
        
        # Дедлайны (6 часов)
        near = (await session.execute(select(Task).where(Task.deadline > now, Task.deadline < now + timedelta(hours=24), Task.status != TaskStatus.DONE))).scalars().all()
        for t in near:
            hours = (t.deadline - now).total_seconds() / 3600
            if 5 < hours < 7: # Попадаем в окно 6 часов
                try: await bot.send_message(t.assignee_id, f"⏰ Скоро дедлайн: {t.title}")
                except: pass
                
        # Питчинг (3 дня)
        rels = (await session.execute(select(Release).where(func.date(Release.release_date) == func.date(now + timedelta(days=3))))).scalars().all()
        founders = (await session.execute(select(User).where(User.role == UserRole.FOUNDER))).scalars().all()
        for r in rels:
            pt = (await session.execute(select(Task).where(Task.release_id==r.id, Task.title.like("%Питчинг%"), Task.status!=TaskStatus.DONE))).scalar_one_or_none()
            if pt:
                msg = f"🔥 АЛЕРТ! Питчинг для {r.title} не готов (3 дня до релиза)!"
                for f in founders:
                    try: await bot.send_message(f.id, msg)
                    except: pass
        await session.commit()

async def smm_daily():
    async with async_session() as session:
        smms = (await session.execute(select(User).where(User.role == UserRole.SMM))).scalars().all()
        for s in smms:
            try: await bot.send_message(s.id, "📋 <b>SMM:</b> Не забудьте отправить ежедневный отчет!", parse_mode="HTML")
            except: pass

async def main():
    # !!! РАСКОММЕНТИРОВАТЬ 1 РАЗ ЧТОБЫ СОЗДАТЬ НОВЫЕ ПОЛЯ БД !!!
    # async with engine.begin() as conn:
    #     await conn.run_sync(Base.metadata.drop_all)
    #     await conn.run_sync(Base.metadata.create_all)
    
    s = AsyncIOScheduler()
    s.add_job(checks, IntervalTrigger(hours=1))
    s.add_job(smm_daily, CronTrigger(hour=12))
    s.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())