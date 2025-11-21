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
    engine, async_session, Base, init_db_and_clean,
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
        if not YandexDisk_TOKEN or len(YandexDisk_TOKEN) < 5:
            return f"mock_storage/{filename}" # Заглушка если нет токена
            
        headers = {"Authorization": f"OAuth {YandexDisk_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            path = f"MusicAlligatorBot/{filename}"
            params = {"path": path, "overwrite": "true"}
            
            # 1. Получаем ссылку
            async with session.get(f"{YandexDiskService.BASE_URL}/upload", headers=headers, params=params) as resp:
                if resp.status != 200: 
                    logger.error(f"YD Error: {await resp.text()}")
                    return None
                data = await resp.json()
                upload_href = data['href']
            
            # 2. Качаем и заливаем
            file_info = await bot.get_file(file_url)
            file_stream = await bot.download_file(file_info.file_path)
            async with session.put(upload_href, data=file_stream) as resp:
                if resp.status != 201: return None
            return path

# --- STATES ---
class ReleaseState(StatesGroup):
    waiting_for_artist = State()
    waiting_for_feat = State()
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

# --- TEMPLATES (По ТЗ) ---
RELEASE_TEMPLATES = {
    "all": [
        # Задача для AR (Родительская для обложки)
        {"title": "Контроль обложки", "role": UserRole.AR_MANAGER, "delta": -12, "file": False, "is_parent_for_cover": True},
        {"title": "Загрузить на площадки", "role": UserRole.AR_MANAGER, "delta": -14, "file": False},
        {"title": "Запросить текст", "role": UserRole.AR_MANAGER, "delta": -15, "file": False},
        {"title": "Проверить копирайты", "role": UserRole.FOUNDER, "delta": -5, "file": False}
    ],
    "pitching": {"title": "Питчинг в Spotify", "role": UserRole.AR_MANAGER, "delta": -14, "file": False}
}

# --- MENUS ---
def get_main_menu(role: str):
    builder = ReplyKeyboardBuilder()
    if role == UserRole.FOUNDER:
        builder.row(KeyboardButton(text="👥 Команда"), KeyboardButton(text="📊 Статистика"))
        builder.row(KeyboardButton(text="📀 Релизы"), KeyboardButton(text="➕ Создать задачу"))
        builder.row(KeyboardButton(text="➕ Новый Релиз"), KeyboardButton(text="➕ Добавить Артиста")) # CEO тоже может
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

# --- AUTH & START ---
@router.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    async with async_session() as session:
        # Авто-добавление Админов
        if user_id in ADMIN_IDS:
            u = await session.get(User, user_id)
            if not u:
                session.add(User(id=user_id, full_name=message.from_user.full_name, role=UserRole.FOUNDER))
                await session.commit()
        
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            await message.answer(f"⛔ Нет доступа. ID: <code>{user_id}</code>. Передайте ID админу.", parse_mode="HTML")
            return
        
        # Обновляем имя
        user.full_name = message.from_user.full_name
        user.username = message.from_user.username
        await session.commit()
        
        await message.answer(f"👋 Привет, {user.role}!", reply_markup=get_main_menu(user.role))

# --- TEAM MANAGEMENT ---
@router.message(F.text.in_({"👥 Команда", "👥 Управление командой"}))
async def team_list(message: types.Message):
    async with async_session() as session:
        u = await session.get(User, message.from_user.id)
        if u.role != UserRole.FOUNDER: return
        
        users = (await session.execute(select(User).order_by(User.role))).scalars().all()
        text = "🏢 <b>Команда:</b>\n"
        kb = InlineKeyboardBuilder()
        
        for x in users:
            text += f"- {x.full_name} ({x.role})\n"
            kb.button(text=f"✏️ {x.full_name}", callback_data=f"editrole_{x.id}")
            
        kb.button(text="➕ Добавить сотрудника", callback_data="add_new_user")
        kb.adjust(1)
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data == "add_new_user")
async def add_user_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🆔 Введите Telegram ID (цифры):")
    await state.set_state(AddUserState.waiting_for_id)
    await callback.answer()

@router.message(AddUserState.waiting_for_id)
async def add_user_id(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text)
        await state.update_data(uid=uid)
        kb = InlineKeyboardBuilder()
        for r in UserRole: kb.button(text=r.value, callback_data=f"newrole_{r.value}")
        kb.adjust(1)
        await message.answer("👤 Выберите роль:", reply_markup=kb.as_markup())
        await state.set_state(AddUserState.waiting_for_role)
    except:
        await message.answer("❌ Только цифры.")

@router.callback_query(F.data.startswith("newrole_"), AddUserState.waiting_for_role)
async def add_user_fin(callback: CallbackQuery, state: FSMContext):
    role = callback.data.split("_")[1]
    data = await state.get_data()
    async with async_session() as session:
        # Upsert
        u = await session.get(User, data['uid'])
        if not u:
            session.add(User(id=data['uid'], role=role, full_name="Новый"))
        else:
            u.role = role
            u.is_active = True
        await session.commit()
    await callback.message.edit_text(f"✅ Пользователь добавлен/обновлен. Роль: {role}")
    await state.clear()

# --- CUSTOM TASKS ---
@router.message(F.text == "➕ Создать задачу")
async def task_create(message: types.Message, state: FSMContext):
    async with async_session() as session:
        u = await session.get(User, message.from_user.id)
        if u.role not in [UserRole.FOUNDER, UserRole.AR_MANAGER]: return
    
    await message.answer("✍️ Название:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CustomTaskState.waiting_for_title)

@router.message(CustomTaskState.waiting_for_title)
async def task_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("✍️ Описание (или '-'):")
    await state.set_state(CustomTaskState.waiting_for_desc)

@router.message(CustomTaskState.waiting_for_desc)
async def task_desc(message: types.Message, state: FSMContext):
    d = message.text if message.text != "-" else None
    await state.update_data(desc=d)
    
    async with async_session() as session:
        users = (await session.execute(select(User).where(User.is_active==True))).scalars().all()
        kb = InlineKeyboardBuilder()
        for u in users: kb.button(text=f"{u.full_name} ({u.role})", callback_data=f"asgn_{u.id}")
        kb.adjust(1)
        await message.answer("👤 Исполнитель:", reply_markup=kb.as_markup())
        await state.set_state(CustomTaskState.waiting_for_assignee)

@router.callback_query(F.data.startswith("asgn_"), CustomTaskState.waiting_for_assignee)
async def task_assign(callback: CallbackQuery, state: FSMContext):
    aid = int(callback.data.split("_")[1])
    await state.update_data(aid=aid)
    await callback.message.edit_text("📅 Дедлайн (ДД.ММ.ГГГГ):")
    await state.set_state(CustomTaskState.waiting_for_deadline)

@router.message(CustomTaskState.waiting_for_deadline)
async def task_fin(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text, "%d.%m.%Y").replace(hour=23, minute=59)
    except:
        await message.answer("❌ Формат ДД.ММ.ГГГГ")
        return
    
    data = await state.get_data()
    async with async_session() as session:
        # FIX: Явно передаем is_regular=False чтобы избежать null ошибки
        t = Task(
            title=data['title'], description=data['desc'], status=TaskStatus.PENDING,
            deadline=dt, assignee_id=data['aid'], creator_id=message.from_user.id,
            is_regular=False 
        )
        session.add(t)
        await session.commit()
        try: await bot.send_message(data['aid'], f"🆕 Задача: {data['title']}\n📅 {message.text}")
        except: pass
        
        u = await session.get(User, message.from_user.id)
        await message.answer("✅ Создано!", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- RELEASES (WITH FOUNDER ACCESS & FEAT) ---
@router.message(F.text == "➕ Новый Релиз")
async def rel_start(message: types.Message, state: FSMContext):
    async with async_session() as session:
        # Проверка прав: Основатель ИЛИ AR
        u = await session.get(User, message.from_user.id)
        if u.role not in [UserRole.FOUNDER, UserRole.AR_MANAGER]:
            await message.answer("⛔ Нет прав.")
            return
            
        artists = (await session.execute(select(Artist))).scalars().all()
        if not artists:
            await message.answer("⚠️ Нет артистов.")
            return
            
        kb = ReplyKeyboardBuilder()
        for a in artists: kb.button(text=a.name)
        kb.adjust(2)
        await message.answer("👤 Артист:", reply_markup=kb.as_markup(resize_keyboard=True))
        await state.set_state(ReleaseState.waiting_for_artist)

@router.message(ReleaseState.waiting_for_artist)
async def rel_artist(message: types.Message, state: FSMContext):
    async with async_session() as session:
        a = (await session.execute(select(Artist).where(Artist.name==message.text))).scalar_one_or_none()
        if not a: return
        await state.update_data(aid=a.id)
    await message.answer("👯 Feat (Со-артисты) или '-':", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_feat)

@router.message(ReleaseState.waiting_for_feat)
async def rel_feat(message: types.Message, state: FSMContext):
    ft = message.text if message.text != "-" else None
    await state.update_data(feat=ft)
    await message.answer("💿 Название:")
    await state.set_state(ReleaseState.waiting_for_title)

@router.message(ReleaseState.waiting_for_title)
async def rel_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    kb = ReplyKeyboardBuilder()
    for t in ReleaseType: kb.button(text=t.value)
    kb.adjust(1)
    await message.answer("💿 Тип:", reply_markup=kb.as_markup(resize_keyboard=True))
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
        await message.answer("❌ Формат ДД.ММ.ГГГГ")
        return
    
    data = await state.get_data()
    async with async_session() as session:
        rel = Release(
            title=data['title'], feat_artists=data['feat'], release_type=data['rtype'],
            artist_id=data['aid'], release_date=d, created_by=message.from_user.id
        )
        session.add(rel)
        await session.flush()
        
        # Иерархия задач и шаблоны
        designers = (await session.execute(select(User).where(User.role==UserRole.DESIGNER))).scalars().all()
        designer_id = designers[0].id if designers else message.from_user.id
        
        title_full = f"{data['title']}"
        if data['feat']: title_full += f" (feat. {data['feat']})"

        for t in RELEASE_TEMPLATES["all"]:
            dl = d + timedelta(days=t['delta'])
            # Создаем задачу
            task = Task(
                title=f"{t['title']} - {title_full}", 
                status=TaskStatus.PENDING, deadline=dl,
                assignee_id=message.from_user.id if t['role'] != UserRole.DESIGNER else designer_id,
                creator_id=message.from_user.id, release_id=rel.id, needs_file=t['file'],
                is_regular=False
            )
            session.add(task)
            await session.flush()

            # Иерархия: Если это "Контроль обложки" (A&R), создаем дочернюю "Сделать обложку" (Дизайнер)
            if t.get("is_parent_for_cover"):
                child = Task(
                    title=f"🎨 Сделать обложку - {title_full}",
                    status=TaskStatus.PENDING, deadline=dl - timedelta(days=2), # Дизайнер сдает раньше
                    assignee_id=designer_id, creator_id=message.from_user.id,
                    release_id=rel.id, parent_id=task.id, needs_file=True, is_regular=False
                )
                session.add(child)
        
        # Питчинг
        if (d - datetime.now()).days > 14:
            pt = RELEASE_TEMPLATES["pitching"]
            session.add(Task(
                title=f"{pt['title']} - {title_full}", status=TaskStatus.PENDING,
                deadline=d + timedelta(days=pt['delta']), assignee_id=message.from_user.id,
                creator_id=message.from_user.id, release_id=rel.id, is_regular=False
            ))
            
        await session.commit()
        u = await session.get(User, message.from_user.id)
        await message.answer("✅ Релиз создан, задачи (и подзадачи) распределены.", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- VIEW TASKS & COMPLETE ---
@router.message(F.text.in_({"📋 Мои Задачи", "🎨 Задачи по обложкам"}))
async def view_tasks(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Просрочка", callback_data="flt_over")
    kb.button(text="🟡 Активные", callback_data="flt_pend")
    kb.adjust(2)
    await message.answer("🔍 Фильтр:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("flt_"))
async def filter_cb(callback: CallbackQuery):
    ft = callback.data.split("_")[1]
    async with async_session() as session:
        q = select(Task).where(Task.assignee_id==callback.from_user.id)
        if ft == "over": q = q.where(Task.status==TaskStatus.OVERDUE)
        else: q = q.where(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]))
        
        tasks = (await session.execute(q.order_by(Task.deadline))).scalars().all()
        if not tasks:
            await callback.message.edit_text("🎉 Пусто!")
            return
        
        await callback.message.delete()
        for t in tasks:
            icon = "🔴" if t.status == TaskStatus.OVERDUE else "🟡"
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Завершить", callback_data=f"fin_{t.id}")
            await callback.message.answer(f"{icon} <b>{t.title}</b>\n⏰ {t.deadline.strftime('%d.%m')}", parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("fin_"))
async def fin_task(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split("_")[1])
    async with async_session() as session:
        t = await session.get(Task, tid)
        if not t: return
        
        if t.needs_file:
            await state.update_data(tid=tid)
            await state.set_state(TaskCompletionState.waiting_for_file)
            await callback.message.answer("📂 Пришлите файл:")
            await callback.answer()
        else:
            t.status = TaskStatus.DONE
            await session.commit()
            await callback.message.edit_text(f"✅ Готово: {t.title}")
            # Логика Родитель-Ребенок
            if t.parent_id:
                parent = await session.get(Task, t.parent_id)
                if parent:
                    try: await bot.send_message(parent.assignee_id, f"👶 Дочерняя задача '{t.title}' выполнена!\nМожно проверять.")
                    except: pass

@router.message(TaskCompletionState.waiting_for_file, F.document | F.photo)
async def file_upload(message: types.Message, state: FSMContext):
    data = await state.get_data()
    fobj = message.document or message.photo[-1]
    msg = await message.answer("⏳ Загрузка...")
    
    async with async_session() as session:
        t = await session.get(Task, data['tid'])
        path = await YandexDiskService.upload_file(fobj.file_id, f"task_{t.id}_{fobj.file_unique_id}", bot)
        t.file_url = path
        t.status = TaskStatus.DONE
        await session.commit()
        await msg.edit_text("✅ Файл принят, задача закрыта.")
    await state.clear()

# --- ONBOARDING & ARTISTS ---
@router.message(F.text == "➕ Добавить Артиста")
async def add_art(message: types.Message, state: FSMContext):
    await message.answer("Имя:")
    await state.set_state(ArtistState.waiting_for_name)

@router.message(ArtistState.waiting_for_name)
async def save_art(message: types.Message, state: FSMContext):
    async with async_session() as session:
        session.add(Artist(name=message.text, ar_manager_id=message.from_user.id))
        await session.commit()
    await message.answer("✅ Артист добавлен.")
    await state.clear()

@router.callback_query(F.data.startswith("onb_"))
async def onb_ans(callback: CallbackQuery):
    _, aid, typ, ans = callback.data.split("_")
    if ans == "no": 
        await callback.message.edit_text("🕐 Ок, позже.")
        return
    async with async_session() as session:
        a = await session.get(Artist, int(aid))
        if typ == "contract": a.contract_signed = True
        elif typ == "yt_note": a.youtube_note = True
        # ... остальные проверки
        await session.commit()
    await callback.message.edit_text("✅ Статус обновлен.")

# --- SCHEDULER (FULL LOGIC) ---
async def scheduler_jobs():
    async with async_session() as session:
        now = datetime.now()
        
        # 1. Просрочка (Ежечасно)
        over = (await session.execute(select(Task).where(Task.deadline < now, Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])))).scalars().all()
        for t in over:
            t.status = TaskStatus.OVERDUE
            try: await bot.send_message(t.assignee_id, f"⚠️ ПРОСРОЧЕНО: {t.title}")
            except: pass
            
        # 2. Дедлайны (каждые 6ч - но проверяем попадание в диапазон)
        near = (await session.execute(select(Task).where(Task.deadline > now, Task.deadline < now + timedelta(hours=24), Task.status!=TaskStatus.DONE))).scalars().all()
        for t in near:
             # Чтобы не спамить каждый час, можно проверять время (упростим: шлем если 23-24ч осталось или 5-6ч)
             h = (t.deadline - now).total_seconds() / 3600
             if 23 < h < 24 or 5 < h < 6:
                 try: await bot.send_message(t.assignee_id, f"⏰ Скоро дедлайн: {t.title}")
                 except: pass
        
        # 3. YouTube Нотка (День релиза + Еженедельно)
        # Логика: Ищем артистов, у которых есть релиз СЕГОДНЯ, и нотки еще нет
        today_releases = (await session.execute(select(Release).where(func.date(Release.release_date) == func.date(now)))).scalars().all()
        for r in today_releases:
            art = await session.get(Artist, r.artist_id)
            if not art.youtube_note:
                kb = InlineKeyboardBuilder()
                kb.button(text="Да", callback_data=f"onb_{art.id}_yt_note_yes")
                kb.button(text="Нет", callback_data=f"onb_{art.id}_yt_note_no")
                try: await bot.send_message(art.ar_manager_id, f"📺 День релиза! Подали заявку на нотку для {art.name}?", reply_markup=kb.as_markup())
                except: pass
                
        # 4. Питчинг Алерт (3 дня)
        crit_rels = (await session.execute(select(Release).where(func.date(Release.release_date) == func.date(now + timedelta(days=3))))).scalars().all()
        founders = (await session.execute(select(User).where(User.role == UserRole.FOUNDER))).scalars().all()
        for r in crit_rels:
            pt = (await session.execute(select(Task).where(Task.release_id==r.id, Task.title.like("%Питчинг%"), Task.status!=TaskStatus.DONE))).scalar_one_or_none()
            if pt:
                msg = f"🔥 СРОЧНО! Питчинг для {r.title} не сдан (3 дня до релиза)!"
                for f in founders:
                    try: await bot.send_message(f.id, msg)
                    except: pass
        
        await session.commit()

async def main():
    # АВТО-ЧИСТКА БАЗЫ ДАННЫХ ПРИ ЗАПУСКЕ (Решает все конфликты схем)
    await init_db_and_clean()
    print("✅ База данных очищена и пересоздана.")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduler_jobs, IntervalTrigger(hours=1))
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())