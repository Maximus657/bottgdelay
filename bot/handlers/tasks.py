import datetime
import io
from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import db
from bot.states import CreateTask, FinishTask
from bot.keyboards.builders import get_cancel_kb, get_main_kb
from bot.config import ROLES_DISPLAY, ADMIN_IDS, YANDEX_DISK_TOKEN, YANDEX_UPLOAD_FOLDER
from bot.utils import notify_user
from bot.services.yandex_disk import AsyncYandexDisk

router = Router()
ydisk = AsyncYandexDisk(YANDEX_DISK_TOKEN, YANDEX_UPLOAD_FOLDER)

# --- CREATION ---
@router.message(F.text == "➕ Создать задачу")
async def manual_task_start(m: types.Message, state: FSMContext):
    """Начало создания задачи вручную."""
    await m.answer("📝 <b>Введите заголовок задачи:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateTask.title)

@router.message(CreateTask.title)
async def manual_task_desc(m: types.Message, state: FSMContext):
    """Ввод описания задачи."""
    await state.update_data(title=m.text)
    await m.answer("📝 <b>Введите описание задачи:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateTask.desc)

@router.message(CreateTask.desc)
async def manual_task_assign(m: types.Message, state: FSMContext):
    """Выбор исполнителя задачи."""
    await state.update_data(desc=m.text)
    users = await db.get_all_users()
    kb = InlineKeyboardBuilder()
    for u in users: 
        r = ROLES_DISPLAY.get(u['role'], u['role'])
        # Добавляем имя и роль
        kb.button(text=f"{u['name']} ({r})", callback_data=f"assign_{u['telegram_id']}")
    kb.adjust(2)
    await m.answer("👤 <b>Исполнитель:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.set_state(CreateTask.assignee)

@router.callback_query(CreateTask.assignee)
async def manual_task_deadline(c: CallbackQuery, state: FSMContext):
    """Ввод дедлайна задачи."""
    await state.update_data(assignee=int(c.data.split("_")[1]))
    await c.message.answer("📅 <b>Введите дедлайн (YYYY-MM-DD):</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateTask.deadline)

@router.message(CreateTask.deadline)
async def manual_task_req(m: types.Message, state: FSMContext):
    """Вопрос о необходимости файла."""
    try:
        cl = m.text.replace(".", "-").replace("/", "-")
        datetime.datetime.strptime(cl, "%Y-%m-%d")
        await state.update_data(deadline=cl)
    except: return await m.answer("⛔️ Формат: YYYY-MM-DD")
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")], [KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)
    await m.answer("📎 <b>Нужен файл при сдаче?</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateTask.req_file)

@router.message(CreateTask.req_file)
async def manual_task_fin(m: types.Message, state: FSMContext, bot: Bot):
    """Завершение создания задачи."""
    req = 1 if m.text == "Да" else 0
    d = await state.get_data()
    await db.create_task(d['title'], d['desc'], d['assignee'], m.from_user.id, None, d['deadline'], req)
    
    creator_link = await db.get_user_link(m.from_user.id)
    msg = f"🔔 <b>НОВАЯ ЗАДАЧА</b>\n📌 {d['title']}\n📄 {d['desc']}\n🗓 {d['deadline']}\n👤 От: {creator_link}"
    await notify_user(bot, d['assignee'], msg)
    
    user = await db.get_user(m.from_user.id)
    await m.answer("✅ Задача назначена!", reply_markup=get_main_kb(user['role']))
    await state.clear()

# --- VIEWING ---
@router.message(F.text.in_({"📋 Активные задачи", "📋 Мои задачи"}))
async def view_tasks(m: types.Message):
    """Просмотр активных задач."""
    uid = m.from_user.id
    user = await db.get_user(uid)
    
    if user['role'] == 'founder' and "Активные" in m.text:
        tasks = await db.get_tasks_active_founder()
        header = "📋 <b>Все активные задачи:</b>"
    else:
        tasks = await db.get_tasks_active_user(uid)
        header = "📋 <b>Ваши задачи:</b>"
        
    if not tasks: return await m.answer("🎉 Задач нет!")
    
    await m.answer(header, parse_mode="HTML")
    
    for t in tasks:
        icon = "🔥" if t['status'] == 'overdue' else "⏳"
        creator = await db.get_user_link(t['created_by'])
        txt = f"{icon} <b>{t['title']}</b>\n━━━━━━━━━━━━━━━━\n📄 {t['description']}\n\n🗓 <code>{t['deadline']}</code>\n👤 От: {creator}"
        
        kb = InlineKeyboardBuilder()
        if t['assigned_to'] == uid:
            kb.button(text="✅ Выполнить", callback_data=f"fin_{t['id']}")
            kb.button(text="⛔️ Отказаться", callback_data=f"rej_{t['id']}")
        if user['role'] == 'founder':
            kb.button(text="🗑 Удалить", callback_data=f"admdel_{t['id']}")
        kb.adjust(2)    
        await m.answer(txt, reply_markup=kb.as_markup(), parse_mode="HTML")

@router.callback_query(F.data.startswith("admdel_"))
async def admin_del_task_ask(c: CallbackQuery):
    """Подтверждение удаления задачи админом."""
    tid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"confdel_{tid}")
    kb.button(text="Отмена", callback_data="ignore_cb")
    await c.message.edit_text("⚠️ <b>Удалить задачу?</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@router.callback_query(F.data.startswith("confdel_"))
async def admin_del_task_confirm(c: CallbackQuery, bot: Bot):
    """Удаление задачи админом."""
    tid = int(c.data.split("_")[1])
    task = await db.get_task_by_id(tid)
    if task:
        await notify_user(bot, task['assigned_to'], f"🗑 <b>Задача аннулирована:</b>\n{task['title']}")
        await db.delete_task(tid)
        await c.message.edit_text("🗑 Удалена.")
    else: await c.answer("Уже удалена.")

@router.callback_query(F.data.startswith("rej_"))
async def reject_ask(c: CallbackQuery):
    """Подтверждение отказа от задачи."""
    tid = c.data.split("_")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отказаться", callback_data=f"confrej_{tid}")
    kb.button(text="Вернуться", callback_data="ignore_cb")
    await c.message.edit_text("⚠️ <b>Отказаться?</b>\nАдминистраторы получат уведомление.", reply_markup=kb.as_markup(), parse_mode="HTML")

@router.callback_query(F.data.startswith("confrej_"))
async def reject_confirm(c: CallbackQuery, bot: Bot):
    """Отказ от задачи."""
    tid = int(c.data.split("_")[1])
    task = await db.get_task_by_id(tid)
    if task:
        await db.update_task_status(tid, 'rejected')
        rejector = await db.get_user_link(c.from_user.id)
        alert = f"⛔️ <b>ОТКАЗ:</b> {task['title']}\n👤 {rejector}"
        for admin_id in ADMIN_IDS: await notify_user(bot, admin_id, alert)
        await c.message.edit_text("❌ Отказано.")
    else: await c.answer("Ошибка")

@router.callback_query(F.data == "ignore_cb")
async def ignore_cb(c: CallbackQuery): await c.message.delete()

# --- HISTORY ---
@router.message(F.text.in_({"📜 История всех задач", "📜 История"}))
async def history(m: types.Message):
    """Просмотр истории выполненных задач."""
    uid = m.from_user.id
    user = await db.get_user(uid)
    role = user['role']
    
    if role == 'founder':
        tasks = await db.get_history_founder()
        header = "📜 <b>Глобальная история (последние 20):</b>"
    else:
        tasks = await db.get_history_user(uid)
        header = "📜 <b>Ваша история:</b>"
        
    if not tasks: return await m.answer("📭 Пусто.")
    txt = f"{header}\n\n"
    for t in tasks:
        user_link = await db.get_user_link(t['assigned_to'])
        txt += f"✅ <b>{t['title']}</b>\n👤 {user_link}\n🗓 {t['deadline']}\n"
        if t['file_url']: 
            txt += "📎 Файл (TG)\n" if "tg:" in t['file_url'] else f"💾 <a href='{t['file_url']}'>Файл (Диск)</a>\n"
        txt += "━━━━━━━━━━━━━━━━\n"
    await m.answer(txt, parse_mode="HTML", disable_web_page_preview=True)

# --- FINISH & UPLOAD ---
@router.callback_query(F.data.startswith("fin_"))
async def fin_start(c: CallbackQuery, state: FSMContext):
    """Начало завершения задачи."""
    tid = int(c.data.split("_")[1])
    task = await db.get_task_by_id(tid)
    if not task or task['status'] == 'done': return await c.answer("Уже выполнено.")
    
    await state.update_data(tid=tid, creator=task['created_by'], title=task['title'])
    if task['requires_file']:
        await c.message.answer("📎 <b>Пришлите файл/фото:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
        await state.set_state(FinishTask.file)
    else:
        await c.message.answer("💬 <b>Комментарий:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
        await state.set_state(FinishTask.comment)

@router.message(FinishTask.file)
async def fin_file(m: types.Message, state: FSMContext, bot: Bot):
    """Загрузка файла при завершении задачи."""
    if m.text == "🔙 Отмена": 
        await state.clear()
        user = await db.get_user(m.from_user.id)
        await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))
        return

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
        
        # Скачиваем файл в поток (BytesIO)
        file_stream = io.BytesIO()
        await bot.download_file(f_info.file_path, destination=file_stream)
        file_stream.seek(0) # Сброс указателя в начало

        await msg.edit_text("⏳ <b>Загрузка...</b> (Отправка на Яндекс)", parse_mode="HTML")
        # Асинхронная загрузка
        pub_url = await ydisk.upload_file(file_stream, fname)
        
    except Exception as e:
        # logger.error(f"Upload error: {e}") # logger нужен
        await msg.edit_text(f"⚠️ Ошибка загрузки: {e}")

    if pub_url:
        await msg.edit_text("✅ <b>Загружено на Диск!</b>", parse_mode="HTML")
        await state.update_data(f_val=pub_url)
    else:
        await msg.edit_text("⚠️ Не удалось загрузить на Диск. Сохранена ссылка на TG.")
        await state.update_data(f_val=f"tg:{ftype}:{fid}")
    
    await m.answer("💬 <b>Напишите комментарий к задаче:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(FinishTask.comment)

@router.message(FinishTask.comment)
async def fin_commit(m: types.Message, state: FSMContext, bot: Bot):
    """Финализация задачи с комментарием."""
    if m.text == "🔙 Отмена":
        await state.clear()
        user = await db.get_user(m.from_user.id)
        await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))
        return
        
    d = await state.get_data()
    await db.update_task_status(d['tid'], 'done', d.get('f_val'), m.text)
    
    perf = await db.get_user_link(m.from_user.id)
    txt = f"✅ <b>Выполнено!</b>\n📌 {d['title']}\n👤 {perf}\n💬 {m.text}"
    
    try:
        # Уведомляем создателя
        if d.get('f_val') and "tg:" in d['f_val']:
            txt += "\n📎 Файл ниже"
            await notify_user(bot, d['creator'], txt)
            _, type_, fid = d['f_val'].split(":", 2)
            if type_ == "photo": await bot.send_photo(d['creator'], fid)
            else: await bot.send_document(d['creator'], fid)
        elif d.get('f_val'):
            txt += f"\n💾 <a href='{d['f_val']}'>Файл (Диск)</a>"
            await notify_user(bot, d['creator'], txt)
        else:
            await notify_user(bot, d['creator'], txt)
    except: pass

    user = await db.get_user(m.from_user.id)
    await m.answer("👍 <b>Задача выполнена!</b>", reply_markup=get_main_kb(user['role']), parse_mode="HTML")
    await state.clear()
