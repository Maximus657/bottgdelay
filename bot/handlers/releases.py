import datetime
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import db
from bot.states import CreateRelease
from bot.keyboards.builders import get_cancel_kb, get_main_kb
from bot.config import ROLES_DISPLAY

router = Router()

async def generate_release_tasks(rel_id, title, r_date, manager_id, artist_name, need_cover):
    """Генерация стандартных задач для релиза."""
    designer = await db.get_designer()
    
    if designer:
        designer_id = designer['telegram_id']
        designer_note = ""
    else:
        designer_id = manager_id
        designer_note = " (Fallback: нет дизайнера)"

    tasks = []
    if need_cover: tasks.append(("🎨 Обложка", f"Сделать обложку: {artist_name} - {title}{designer_note}", designer_id, 14, 1))
    tasks.append(("📤 Дистрибуция", f"Загрузить трек: {artist_name} - {title}", manager_id, 10, 0))
    tasks.append(("📝 Питчинг", f"Форма питчинга: {artist_name} - {title}", manager_id, 7, 0))
    tasks.append(("📱 Сниппет", f"Видео-сниппет: {artist_name} - {title}{designer_note}", designer_id, 3, 1))
    
    r_dt = datetime.datetime.strptime(r_date, "%Y-%m-%d")
    for t_name, t_desc, assignee, days, req in tasks:
        dl = (r_dt - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        await db.create_task(f"{t_name} | {artist_name}", t_desc, assignee, manager_id, rel_id, dl, req)

@router.message(F.text == "💿 Создать релиз")
async def create_release_start(m: types.Message, state: FSMContext):
    """Начало создания релиза."""
    user = await db.get_user(m.from_user.id)
    if user['role'] not in ['founder', 'anr']: return
    await m.answer("🎤 <b>Артист(ы):</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateRelease.artist_str)

@router.message(CreateRelease.artist_str)
async def create_release_title(m: types.Message, state: FSMContext):
    """Ввод названия релиза."""
    await state.update_data(artist=m.text)
    await m.answer("💿 <b>Название релиза:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateRelease.title)

@router.message(CreateRelease.title)
async def create_release_type(m: types.Message, state: FSMContext):
    """Выбор типа релиза."""
    await state.update_data(title=m.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Сингл"), KeyboardButton(text="Альбом")], [KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)
    await m.answer("📼 <b>Тип:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateRelease.rtype)

@router.message(CreateRelease.rtype)
async def create_release_cover(m: types.Message, state: FSMContext):
    """Вопрос про обложку."""
    await state.update_data(type=m.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Есть"), KeyboardButton(text="❌ Нужно сделать")], [KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)
    await m.answer("🎨 <b>Обложка готова?</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CreateRelease.has_cover)

@router.message(CreateRelease.has_cover)
async def create_release_date(m: types.Message, state: FSMContext):
    """Ввод даты релиза."""
    need_cover = True if m.text == "❌ Нужно сделать" else False
    await state.update_data(need_cover=need_cover)
    await m.answer("📅 <b>Дата (YYYY-MM-DD):</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateRelease.date)

@router.message(CreateRelease.date)
async def create_release_finish(m: types.Message, state: FSMContext):
    """Завершение создания релиза."""
    try:
        clean_date = m.text.replace(".", "-").replace("/", "-")
        datetime.datetime.strptime(clean_date, "%Y-%m-%d")
    except: return await m.answer("⛔️ Формат: YYYY-MM-DD")

    data = await state.get_data()
    manager_id = m.from_user.id
    
    # Проверяем или создаем артиста
    artist = await db.get_artist_by_name(data['artist'])
    if not artist:
        artist_id = await db.create_artist(data['artist'], manager_id, clean_date)
    else: 
        artist_id = artist['id']
        
    # Создаем релиз
    rel_id = await db.create_release(data['title'], artist_id, data['type'], clean_date, manager_id)
    
    await generate_release_tasks(rel_id, data['title'], clean_date, manager_id, data['artist'], data['need_cover'])
    
    user = await db.get_user(manager_id)
    await m.answer(f"🚀 <b>Релиз создан!</b>\n🎶 {data['artist']} — {data['title']}", reply_markup=get_main_kb(user['role']), parse_mode="HTML")
    await state.clear()

# --- RELEASES LIST (PAGINATION) ---
@router.message(F.text.in_({"💿 Релизы", "💿 Все релизы", "💿 Мои релизы"}))
async def list_releases_handler(m: types.Message):
    """Показать первую страницу релизов."""
    await show_releases_page(m, 0)

async def show_releases_page(message_or_call, page):
    """Отображение страницы релизов."""
    # Определяем ID пользователя и метод ответа
    if isinstance(message_or_call, types.Message):
        uid = message_or_call.from_user.id
        reply_func = message_or_call.answer
    else:
        uid = message_or_call.from_user.id
        reply_func = message_or_call.message.edit_text

    user = await db.get_user(uid)
    if user['role'] not in ['founder', 'anr']: return

    rels, total_count = await db.get_releases_paginated(user['role'], uid, page=page, limit=5)
    
    header = "💿 <b>Все релизы:</b>" if user['role'] == 'founder' else "💿 <b>Ваши релизы:</b>"
    
    if not rels:
        text = f"{header}\n📭 Список пуст."
        kb = None
    else:
        text = f"{header} (Всего: {total_count})\n\n"
        for r in rels:
            c_info = f"👤 От: {r['creator_name']}\n" if user['role'] == 'founder' and 'creator_name' in r else ""
            text += f"🎶 <b>{r['title']}</b> ({r['type']})\n📅 {r['release_date']}\n{c_info}🆔 ID: <code>{r['id']}</code>\n➖➖➖➖➖➖\n"
        
        # Кнопки пагинации
        kb_build = InlineKeyboardBuilder()
        if page > 0:
            kb_build.button(text="⬅️ Назад", callback_data=f"relpage_{page-1}")
        
        if (page + 1) * 5 < total_count:
            kb_build.button(text="Вперед ➡️", callback_data=f"relpage_{page+1}")
        
        kb = kb_build.as_markup()

    if isinstance(message_or_call, types.CallbackQuery):
        await reply_func(text, reply_markup=kb, parse_mode="HTML")
    else:
        await reply_func(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("relpage_"))
async def releases_page_callback(c: CallbackQuery):
    """Обработчик пагинации релизов."""
    page = int(c.data.split("_")[1])
    await show_releases_page(c, page)

@router.message(F.text == "🗑 Удалить релиз")
async def delete_rel_start(m: types.Message):
    """Начало удаления релиза."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'founder': return
    
    rels = await db.get_last_releases(limit=10)
    kb = InlineKeyboardBuilder()
    for r in rels: kb.button(text=f"❌ {r['title']}", callback_data=f"del_rel_{r['id']}")
    kb.adjust(1)
    await m.answer("Выберите релиз для удаления (показаны последние 10):", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("del_rel_"))
async def delete_rel_confirm(c: CallbackQuery):
    """Подтверждение удаления релиза."""
    rid = int(c.data.split("_")[2])
    await db.delete_release_cascade(rid)
    await c.message.edit_text("🗑 Релиз и задачи удалены.")
