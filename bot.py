import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, date

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
        if not YandexDisk_TOKEN or len(YandexDisk_TOKEN) < 5: return f"mock_storage/{filename}"
        headers = {"Authorization": f"OAuth {YandexDisk_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            params = {"path": f"MusicAlligatorBot/{filename}", "overwrite": "true"}
            async with session.get(f"{YandexDiskService.BASE_URL}/upload", headers=headers, params=params) as resp:
                if resp.status != 200: return None
                data = await resp.json()
                href = data['href']
            f_info = await bot.get_file(file_url)
            stream = await bot.download_file(f_info.file_path)
            async with session.put(href, data=stream) as resp:
                if resp.status != 201: return None
            return f"MusicAlligatorBot/{filename}"

# --- STATES ---
class ReleaseState(StatesGroup):
    waiting_for_artist_name = State()
    waiting_for_feat = State()
    waiting_for_title = State()
    waiting_for_type = State()
    waiting_for_date = State()
    waiting_for_cover_status = State()

class CustomTaskState(StatesGroup):
    waiting_for_title = State()
    waiting_for_desc = State()
    waiting_for_assignee = State()
    waiting_for_deadline = State()

class TaskCompletionState(StatesGroup):
    waiting_for_file = State()
    waiting_for_comment = State()

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
RELEASE_TEMPLATES = [
    {"title": "🎨 Создать обложку", "role": UserRole.DESIGNER, "delta": -14, "file": True, "condition": "no_cover"},
    {"title": "🎥 Создать Canvas", "role": UserRole.DESIGNER, "delta": -10, "file": True, "condition": "always"},
    {"title": "📤 Загрузить обложку (Диск)", "role": UserRole.AR_MANAGER, "delta": -13, "file": True, "condition": "has_cover"},
    {"title": "📤 Загрузить на площадки", "role": UserRole.AR_MANAGER, "delta": -14, "file": False, "condition": "always"},
    {"title": "📝 Запросить текст", "role": UserRole.AR_MANAGER, "delta": -15, "file": False, "condition": "always"},
    {"title": "⚖️ Проверить копирайты", "role": UserRole.FOUNDER, "delta": -5, "file": False, "condition": "always"}
]
PITCHING_TEMPLATE = {"title": "🚀 Питчинг в Spotify", "role": UserRole.AR_MANAGER, "delta": -14, "file": False}

SMM_DAILY_TEMPLATES = ["📲 Выложить сторис", "💬 Проверить директ", "📈 Анализ статистики"]

# --- MENU ---
def get_main_menu(role: str):
    builder = ReplyKeyboardBuilder()
    if role == UserRole.FOUNDER:
        builder.row(KeyboardButton(text="👥 Команда"), KeyboardButton(text="📊 Статистика"))
        builder.row(KeyboardButton(text="📀 Релизы"), KeyboardButton(text="➕ Создать задачу"))
        builder.row(KeyboardButton(text="➕ Новый Релиз"))
    elif role == UserRole.AR_MANAGER:
        builder.row(KeyboardButton(text="📀 Релизы"), KeyboardButton(text="➕ Новый Релиз"))
        builder.row(KeyboardButton(text="➕ Создать задачу"))
    elif role == UserRole.DESIGNER:
        builder.row(KeyboardButton(text="🎨 Задачи по дизайну"))
    elif role == UserRole.SMM:
        builder.row(KeyboardButton(text="📝 Отчет за сегодня"), KeyboardButton(text="📅 Архив отчетов"))
    builder.row(KeyboardButton(text="📋 Мои Задачи"))
    return builder.as_markup(resize_keyboard=True)

# --- AUTH & TEAM ---
@router.message(CommandStart())
async def cmd_start(msg: types.Message):
    user_id = msg.from_user.id
    async with async_session() as session:
        if user_id in ADMIN_IDS:
            if not await session.get(User, user_id):
                session.add(User(id=user_id, full_name=msg.from_user.full_name, role=UserRole.FOUNDER))
                await session.commit()
        u = await session.get(User, user_id)
        if not u or not u.is_active:
            await msg.answer(f"⛔ Нет доступа. Ваш ID: {user_id}")
            return
        u.full_name = msg.from_user.full_name
        u.username = msg.from_user.username
        await session.commit()
        await msg.answer(f"👋 Привет, {u.role}!", reply_markup=get_main_menu(u.role))

@router.message(F.text.in_({"👥 Команда", "👥 Управление командой"}))
async def team_view(msg: types.Message):
    async with async_session() as session:
        if (await session.get(User, msg.from_user.id)).role != UserRole.FOUNDER: return
        users = (await session.execute(select(User).order_by(User.role))).scalars().all()
        txt = "🏢 <b>Команда:</b>\n"
        kb = InlineKeyboardBuilder()
        for u in users:
            txt += f"- {u.full_name} ({u.role})\n"
            kb.button(text=f"✏️ {u.full_name}", callback_data=f"editrole_{u.id}")
        kb.button(text="➕ Добавить сотрудника", callback_data="add_new_user")
        kb.adjust(1)
        await msg.answer(txt, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data == "add_new_user")
async def add_user_s1(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("🆔 Введите ID:")
    await state.set_state(AddUserState.waiting_for_id)
    await cb.answer()

@router.message(AddUserState.waiting_for_id)
async def add_user_s2(msg: types.Message, state: FSMContext):
    try:
        await state.update_data(uid=int(msg.text))
        kb = InlineKeyboardBuilder()
        for r in UserRole: kb.button(text=r.value, callback_data=f"newrole_{r.value}")
        kb.adjust(1)
        await msg.answer("Роль:", reply_markup=kb.as_markup())
        await state.set_state(AddUserState.waiting_for_role)
    except: await msg.answer("Цифры!")

@router.callback_query(F.data.startswith("newrole_"))
async def add_user_s3(cb: CallbackQuery, state: FSMContext):
    role = cb.data.split("_")[1]
    data = await state.get_data()
    async with async_session() as session:
        u = await session.get(User, data['uid'])
        if not u: session.add(User(id=data['uid'], role=role, full_name="New User"))
        else: u.role = role; u.is_active = True
        await session.commit()
    await cb.message.edit_text(f"✅ Добавлен: {role}")
    await state.clear()

@router.callback_query(F.data.startswith("editrole_"))
async def edit_role_s1(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split("_")[1])
    await state.update_data(uid=uid)
    kb = InlineKeyboardBuilder()
    for r in UserRole: kb.button(text=r.value, callback_data=f"newrole_{r.value}")
    kb.adjust(1)
    await cb.message.edit_text("Новая роль:", reply_markup=kb.as_markup())

# --- CUSTOM TASKS ---
@router.message(F.text == "➕ Создать задачу")
async def ct_start(msg: types.Message, state: FSMContext):
    async with async_session() as session:
        if (await session.get(User, msg.from_user.id)).role not in [UserRole.FOUNDER, UserRole.AR_MANAGER]: return
    await msg.answer("✍️ Название:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CustomTaskState.waiting_for_title)

@router.message(CustomTaskState.waiting_for_title)
async def ct_title(msg: types.Message, state: FSMContext):
    await state.update_data(title=msg.text)
    await msg.answer("✍️ Описание (или '-'):")
    await state.set_state(CustomTaskState.waiting_for_desc)

@router.message(CustomTaskState.waiting_for_desc)
async def ct_desc(msg: types.Message, state: FSMContext):
    await state.update_data(desc=msg.text if msg.text != "-" else None)
    async with async_session() as session:
        users = (await session.execute(select(User).where(User.is_active==True))).scalars().all()
        kb = InlineKeyboardBuilder()
        for u in users: kb.button(text=f"{u.full_name} ({u.role})", callback_data=f"asgn_{u.id}")
        kb.adjust(1)
        await msg.answer("👤 Исполнитель:", reply_markup=kb.as_markup())
        await state.set_state(CustomTaskState.waiting_for_assignee)

@router.callback_query(F.data.startswith("asgn_"), CustomTaskState.waiting_for_assignee)
async def ct_asgn(cb: CallbackQuery, state: FSMContext):
    await state.update_data(aid=int(cb.data.split("_")[1]))
    await cb.message.edit_text("📅 Дедлайн (ДД.ММ.ГГГГ):")
    await state.set_state(CustomTaskState.waiting_for_deadline)

@router.message(CustomTaskState.waiting_for_deadline)
async def ct_fin(msg: types.Message, state: FSMContext):
    try: dt = datetime.strptime(msg.text, "%d.%m.%Y").replace(hour=23, minute=59)
    except: 
        await msg.answer("ДД.ММ.ГГГГ")
        return
    d = await state.get_data()
    async with async_session() as session:
        session.add(Task(title=d['title'], description=d['desc'], status=TaskStatus.PENDING, deadline=dt, assignee_id=d['aid'], creator_id=msg.from_user.id, is_regular=False))
        await session.commit()
        u = await session.get(User, msg.from_user.id)
        await msg.answer("✅ Создано", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- RELEASES ---
@router.message(F.text == "➕ Новый Релиз")
async def rel_start(msg: types.Message, state: FSMContext):
    async with async_session() as session:
        if (await session.get(User, msg.from_user.id)).role not in [UserRole.FOUNDER, UserRole.AR_MANAGER]: return
    await msg.answer("🎤 Имя артиста:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_artist_name)

@router.message(ReleaseState.waiting_for_artist_name)
async def rel_name(msg: types.Message, state: FSMContext):
    await state.update_data(aname=msg.text)
    await msg.answer("👯 Feat (или '-'):")
    await state.set_state(ReleaseState.waiting_for_feat)

@router.message(ReleaseState.waiting_for_feat)
async def rel_feat(msg: types.Message, state: FSMContext):
    await state.update_data(feat=msg.text if msg.text != "-" else None)
    await msg.answer("💿 Название:")
    await state.set_state(ReleaseState.waiting_for_title)

@router.message(ReleaseState.waiting_for_title)
async def rel_title(msg: types.Message, state: FSMContext):
    await state.update_data(title=msg.text)
    kb = ReplyKeyboardBuilder()
    for t in ReleaseType: kb.button(text=t.value)
    kb.adjust(1)
    await msg.answer("💿 Тип:", reply_markup=kb.as_markup(resize_keyboard=True))
    await state.set_state(ReleaseState.waiting_for_type)

@router.message(ReleaseState.waiting_for_type)
async def rel_type(msg: types.Message, state: FSMContext):
    await state.update_data(rtype=msg.text)
    await msg.answer("📅 Дата (ДД.ММ.ГГГГ):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ReleaseState.waiting_for_date)

@router.message(ReleaseState.waiting_for_date)
async def rel_date(msg: types.Message, state: FSMContext):
    try: d = datetime.strptime(msg.text, "%d.%m.%Y")
    except: 
        await msg.answer("ДД.ММ.ГГГГ")
        return
    await state.update_data(date=d)
    kb = ReplyKeyboardBuilder()
    kb.button(text="✅ Есть")
    kb.button(text="❌ Нет")
    kb.adjust(2)
    await msg.answer("🎨 Обложка готова?", reply_markup=kb.as_markup(resize_keyboard=True))
    await state.set_state(ReleaseState.waiting_for_cover_status)

@router.message(ReleaseState.waiting_for_cover_status)
async def rel_fin(msg: types.Message, state: FSMContext):
    has_cov = msg.text == "✅ Есть"
    data = await state.get_data()
    async with async_session() as session:
        # Артист (Онбординг старт)
        art = (await session.execute(select(Artist).where(Artist.name==data['aname']))).scalar_one_or_none()
        if not art:
            session.add(Artist(name=data['aname'], created_by_id=msg.from_user.id))
            await session.flush()
        
        # Релиз
        rel = Release(title=data['title'], artist_name=data['aname'], feat_artists=data['feat'], release_type=data['rtype'], release_date=data['date'], created_by=msg.from_user.id, cover_provided=has_cov)
        session.add(rel)
        await session.flush()
        
        # Задачи
        des = (await session.execute(select(User).where(User.role==UserRole.DESIGNER))).scalars().all()
        des_id = des[0].id if des else msg.from_user.id
        full = f"{data['aname']} - {data['title']}"

        for tmpl in RELEASE_TEMPLATES:
            if tmpl.get("condition") == "no_cover" and has_cov: continue
            if tmpl.get("condition") == "has_cover" and not has_cov: continue
            
            aid = des_id if tmpl['role'] == UserRole.DESIGNER else msg.from_user.id
            # Создаем задачу
            t = Task(title=f"{tmpl['title']} | {full}", status=TaskStatus.PENDING, deadline=data['date']+timedelta(days=tmpl['delta']), assignee_id=aid, creator_id=msg.from_user.id, release_id=rel.id, needs_file=tmpl['file'], is_regular=False)
            session.add(t)
            await session.flush()
            
            # Иерархия (обложка A&R -> обложка Designer)
            if tmpl['role'] == UserRole.AR_MANAGER and "обложка" in tmpl['title'].lower() and not has_cov:
                # Если A&R создает, делаем подзадачу дизайнеру
                session.add(Task(title=f"🎨 Сделать обложку (Саб-задача)", status=TaskStatus.PENDING, deadline=t.deadline-timedelta(days=2), assignee_id=des_id, creator_id=msg.from_user.id, release_id=rel.id, needs_file=True, parent_id=t.id, is_regular=False))

        # Питчинг
        if (data['date'] - datetime.now()).days > 14:
            session.add(Task(title=f"{PITCHING_TEMPLATE['title']} | {full}", status=TaskStatus.PENDING, deadline=data['date']+timedelta(days=PITCHING_TEMPLATE['delta']), assignee_id=msg.from_user.id, creator_id=msg.from_user.id, release_id=rel.id, is_regular=False))
            
        await session.commit()
        u = await session.get(User, msg.from_user.id)
        await msg.answer("✅ Релиз создан!", reply_markup=get_main_menu(u.role))
    await state.clear()

# --- TASKS VIEW & COMPLETE ---
@router.message(F.text.in_({"📋 Мои Задачи", "🎨 Задачи по дизайну"}))
async def view_tasks(msg: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Просрочено", callback_data="f_ov")
    kb.button(text="🟡 Активные", callback_data="f_act")
    kb.adjust(2)
    await msg.answer("Фильтр:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("f_"))
async def f_cb(cb: CallbackQuery):
    ft = cb.data
    async with async_session() as session:
        q = select(Task).where(Task.assignee_id==cb.from_user.id)
        q = q.where(Task.status==TaskStatus.OVERDUE) if ft=="f_ov" else q.where(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]))
        tasks = (await session.execute(q.order_by(Task.deadline))).scalars().all()
        if not tasks: return await cb.message.edit_text("🎉 Пусто")
        await cb.message.delete()
        for t in tasks:
            kb = InlineKeyboardBuilder(); kb.button(text="✅", callback_data=f"fin_{t.id}")
            await cb.message.answer(f"{'🔴' if t.status==TaskStatus.OVERDUE else '🟡'} <b>{t.title}</b>\n⏰ {t.deadline.strftime('%d.%m')}", parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("fin_"))
async def fin_task(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split("_")[1])
    async with async_session() as session:
        t = await session.get(Task, tid)
        await state.update_data(tid=tid)
        if t.needs_file:
            await state.set_state(TaskCompletionState.waiting_for_file)
            await cb.message.answer("📂 Прикрепите файл:")
        else:
            await state.set_state(TaskCompletionState.waiting_for_comment)
            await cb.message.answer("💬 Напишите комментарий (или '-'):")
        await cb.answer()

@router.message(TaskCompletionState.waiting_for_file, F.document | F.photo)
async def fin_file(msg: types.Message, state: FSMContext):
    d = await state.get_data()
    f = msg.document or msg.photo[-1]
    m = await msg.answer("⏳ Загрузка...")
    async with async_session() as session:
        t = await session.get(Task, d['tid'])
        p = await YandexDiskService.upload_file(f.file_id, f"task_{t.id}", bot)
        t.file_url = p; t.status = TaskStatus.DONE
        if msg.caption: t.description = (t.description or "") + f"\nКоммент: {msg.caption}"
        await session.commit()
        await m.edit_text("✅ Задача закрыта.")
        if t.creator_id != t.assignee_id:
            try: await bot.send_message(t.creator_id, f"✅ Задача {t.title} выполнена (файл).")
            except: pass
    await state.clear()

@router.message(TaskCompletionState.waiting_for_comment)
async def fin_comm(msg: types.Message, state: FSMContext):
    d = await state.get_data()
    comm = msg.text if msg.text != "-" else ""
    async with async_session() as session:
        t = await session.get(Task, d['tid'])
        t.status = TaskStatus.DONE
        if comm: t.description = (t.description or "") + f"\nКоммент: {comm}"
        await session.commit()
        await msg.answer("✅")
        if t.creator_id != t.assignee_id:
            try: await bot.send_message(t.creator_id, f"✅ Задача {t.title} выполнена.\n{comm}")
            except: pass
    await state.clear()

# --- SMM REPORTS (PAGINATION) ---
@router.message(F.text == "📝 Отчет за сегодня")
async def smm_rep(msg: types.Message, state: FSMContext):
    await msg.answer("✍️ Текст:")
    await state.set_state(SMMReportState.waiting_for_text)

@router.message(SMMReportState.waiting_for_text)
async def smm_save(msg: types.Message, state: FSMContext):
    async with async_session() as session:
        session.add(Report(user_id=msg.from_user.id, text=msg.text))
        await session.commit()
    await msg.answer("✅")
    await state.clear()

@router.message(F.text == "📅 Архив отчетов")
async def smm_hist_start(msg: types.Message):
    await show_reports(msg, 0)

async def show_reports(msg_or_cb, page):
    async with async_session() as session:
        uid = msg_or_cb.from_user.id
        reps = (await session.execute(select(Report).where(Report.user_id==uid).order_by(desc(Report.created_at)).offset(page*5).limit(5))).scalars().all()
        
        if not reps and page==0: 
            if isinstance(msg_or_cb, types.CallbackQuery): await msg_or_cb.message.edit_text("📭 Пусто")
            else: await msg_or_cb.answer("📭 Пусто")
            return

        txt = f"📜 <b>Отчеты (Стр. {page+1}):</b>\n\n"
        for r in reps: txt += f"🔹 {r.created_at.strftime('%d.%m %H:%M')}: {r.text[:40]}...\n"
        
        kb = InlineKeyboardBuilder()
        if page > 0: kb.button(text="⬅️", callback_data=f"rpage_{page-1}")
        if len(reps) == 5: kb.button(text="➡️", callback_data=f"rpage_{page+1}")
        
        if isinstance(msg_or_cb, types.CallbackQuery): await msg_or_cb.message.edit_text(txt, parse_mode="HTML", reply_markup=kb.as_markup())
        else: await msg_or_cb.answer(txt, parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("rpage_"))
async def smm_page(cb: CallbackQuery):
    await show_reports(cb, int(cb.data.split("_")[1]))

@router.message(F.text == "📀 Релизы")
async def list_rel(msg: types.Message):
    async with async_session() as session:
        rels = (await session.execute(select(Release).order_by(Release.release_date))).scalars().all()
        if not rels: await msg.answer("📭")
        u = await session.get(User, msg.from_user.id)
        for r in rels:
            kb = InlineKeyboardBuilder()
            if u.role == UserRole.FOUNDER: kb.button(text="🗑", callback_data=f"delrel_{r.id}")
            await msg.answer(f"💿 <b>{r.artist_name} - {r.title}</b>\n📅 {r.release_date.strftime('%d.%m.%Y')}", parse_mode="HTML", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("delrel_"))
async def del_rel(cb: CallbackQuery):
    async with async_session() as session:
        r = await session.get(Release, int(cb.data.split("_")[1]))
        if r: await session.delete(r); await session.commit()
    await cb.message.edit_text("❌ Удалено")

# --- SCHEDULER (FULL SPEC) ---
async def jobs():
    async with async_session() as session:
        now = datetime.now()
        today_date = now.date()
        
        # 1. SMM ГЕНЕРАЦИЯ (10:00)
        if now.hour == 10:
            smms = (await session.execute(select(User).where(User.role == UserRole.SMM))).scalars().all()
            for smm in smms:
                for tmpl in SMM_DAILY_TEMPLATES:
                    exists = (await session.execute(select(Task).where(Task.assignee_id==smm.id, Task.title==tmpl, func.date(Task.deadline)==today_date))).scalar_one_or_none()
                    if not exists: session.add(Task(title=tmpl, status=TaskStatus.PENDING, deadline=now.replace(hour=23,minute=59), assignee_id=smm.id, creator_id=smm.id, is_regular=True))
        
        # 2. РЕЛИЗЫ УВЕДОМЛЕНИЯ (10:00) - ВОССТАНОВЛЕНО!
        if now.hour == 10:
            # 1 и 2 дня до релиза
            rels = (await session.execute(select(Release))).scalars().all()
            for r in rels:
                days = (r.release_date.date() - today_date).days
                if days in [1, 2]:
                    try: await bot.send_message(r.created_by, f"🔔 Релиз {r.title} через {days} дн!")
                    except: pass
                # Питчинг Алерт (3 дня)
                if days == 3:
                    pt = (await session.execute(select(Task).where(Task.release_id==r.id, Task.title.like("%Питчинг%"), Task.status!=TaskStatus.DONE))).scalar_one_or_none()
                    if pt:
                        founders = (await session.execute(select(User).where(User.role == UserRole.FOUNDER))).scalars().all()
                        for f in founders:
                            try: await bot.send_message(f.id, f"🔥 Питчинг для {r.title} не готов! Релиз через 3 дня.")
                            except: pass

        # 3. ОНБОРДИНГ (15:00)
        if now.hour == 15:
            # Контракт и YouTube привязка
            arts = (await session.execute(select(Artist).where(Artist.contract_signed == False))).scalars().all()
            for a in arts:
                kb = InlineKeyboardBuilder(); kb.button(text="✅", callback_data=f"onb_{a.id}_contract_yes"); kb.button(text="❌", callback_data=f"onb_{a.id}_contract_no")
                try: await bot.send_message(a.created_by_id, f"📝 Контракт {a.name}?", reply_markup=kb.as_markup())
                except: pass
            
            # YouTube Нотка (В ДЕНЬ РЕЛИЗА) - ВОССТАНОВЛЕНО!
            rels_today = (await session.execute(select(Release).where(func.date(Release.release_date) == today_date))).scalars().all()
            for r in rels_today:
                a = await session.get(Artist, (await session.execute(select(Artist).where(Artist.name==r.artist_name))).scalar_one().id)
                if not a.youtube_note:
                    try: await bot.send_message(r.created_by, f"📺 Релиз сегодня! Подай на Нотку для {a.name}")
                    except: pass

        # 4. MUSIXMATCH (Понедельник)
        if now.weekday() == 0 and now.hour == 14:
            arts = (await session.execute(select(Artist).where(Artist.musixmatch_profile == False))).scalars().all()
            for a in arts:
                try: await bot.send_message(a.created_by_id, f"🔔 Musixmatch профиль {a.name}?")
                except: pass

        # 5. ТЕКУЩИЕ ЗАДАЧИ (Каждый час)
        # Просрочка
        over = (await session.execute(select(Task).where(Task.deadline < now, Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])))).scalars().all()
        for t in over:
            t.status = TaskStatus.OVERDUE
            try: await bot.send_message(t.assignee_id, f"⚠️ ПРОСРОЧЕНО: {t.title}")
            except: pass
        
        # Дедлайн 6ч
        near = (await session.execute(select(Task).where(Task.deadline > now, Task.deadline < now + timedelta(hours=24), Task.status!=TaskStatus.DONE))).scalars().all()
        for t in near:
            h = (t.deadline - now).total_seconds() / 3600
            if 5 < h < 6:
                try: await bot.send_message(t.assignee_id, f"⏰ Скоро дедлайн: {t.title}")
                except: pass

        await session.commit()

@router.callback_query(F.data.startswith("onb_"))
async def onb_cb(cb: CallbackQuery):
    _, aid, typ, ans = cb.data.split("_")
    if ans == "no": return await cb.message.edit_text("🕐 Позже")
    async with async_session() as session:
        a = await session.get(Artist, int(aid))
        if typ=="contract": a.contract_signed=True
        # ... (остальные типы)
        await session.commit()
    await cb.message.edit_text("✅")

async def main():
    await init_db_and_clean()
    print("✅ DB READY")
    s = AsyncIOScheduler()
    s.add_job(jobs, IntervalTrigger(hours=1))
    s.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())