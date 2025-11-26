import datetime
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import db
from bot.states import CreateArtist
from bot.keyboards.builders import get_cancel_kb, get_main_kb
from bot.utils import notify_user

router = Router()

@router.message(F.text == "🎤 Артисты")
async def list_artists(m: types.Message):
    """Список всех артистов."""
    user = await db.get_user(m.from_user.id)
    if user['role'] not in ['founder', 'anr']: return

    artists = await db.get_all_artists()
    
    text = "🎤 <b>Список артистов:</b>\n\n"
    kb = InlineKeyboardBuilder()
    
    for a in artists:
        # Статус онбординга (галочки)
        status = ""
        if a['flag_contract']: status += "📝"
        if a['flag_mm_profile']: status += "🎵"
        if a['flag_mm_verify']: status += "✅"
        if a['flag_yt_link']: status += "📺"
        if a['flag_yt_note']: status += "🎼"
        
        kb.button(text=f"{a['name']} {status}", callback_data=f"view_art_{a['id']}")
    
    kb.button(text="➕ Добавить артиста", callback_data="add_artist")
    kb.adjust(1)
    
    await m.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@router.callback_query(F.data == "add_artist")
async def add_artist_start(c: CallbackQuery, state: FSMContext):
    """Начало добавления артиста."""
    await c.message.answer("👤 <b>Введите имя артиста:</b>", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateArtist.name)
    await c.answer()

@router.message(CreateArtist.name)
async def add_artist_manager(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text)
    
    users = await db.get_all_users()
    kb = InlineKeyboardBuilder()
    for u in users:
        kb.button(text=f"{u['name']}", callback_data=f"set_mgr_{u['telegram_id']}")
    kb.adjust(2)
    
    await m.answer("💼 <b>Выберите менеджера:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.set_state(CreateArtist.manager)

@router.callback_query(CreateArtist.manager)
async def add_artist_date(c: CallbackQuery, state: FSMContext):
    mgr_id = int(c.data.split("_")[2])
    await state.update_data(manager=mgr_id)
    
    await c.message.answer("📅 <b>Дата первого релиза (YYYY-MM-DD):</b>\n(Или напишите 'Нет', если не планируется)", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(CreateArtist.date)
    await c.answer()

@router.message(CreateArtist.date)
async def add_artist_finish(m: types.Message, state: FSMContext):
    date_str = m.text
    if date_str.lower() == "нет":
        date_str = None
    else:
        try:
            date_str = date_str.replace(".", "-").replace("/", "-")
            datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except:
            return await m.answer("⛔️ Неверный формат даты. Используйте YYYY-MM-DD или 'Нет'.")
            
    data = await state.get_data()
    await db.create_artist(data['name'], data['manager'], date_str)
    
    user = await db.get_user(m.from_user.id)
    await m.answer(f"✅ Артист <b>{data['name']}</b> добавлен!", reply_markup=get_main_kb(user['role']), parse_mode="HTML")
    await state.clear()

@router.callback_query(F.data.startswith("view_art_"))
async def view_artist(c: CallbackQuery):
    aid = int(c.data.split("_")[2])
    artist = await db.get_artist_by_id(aid)
    if not artist: return await c.answer("Артист не найден")
    
    mgr = await db.get_user(artist['manager_id'])
    mgr_name = mgr['name'] if mgr else "Не назначен"
    
    text = f"🎤 <b>{artist['name']}</b>\n"
    text += f"💼 Менеджер: {mgr_name}\n"
    text += f"📅 Первый релиз: {artist['first_release_date'] or 'Не задан'}\n\n"
    text += "<b>Статус онбординга:</b>\n"
    
    flags = [
        ('flag_contract', '📝 Контракт'),
        ('flag_mm_profile', '🎵 MM Профиль'),
        ('flag_mm_verify', '✅ MM Верификация'),
        ('flag_yt_link', '📺 YouTube Линк'),
        ('flag_yt_note', '🎼 YouTube Нота')
    ]
    
    kb = InlineKeyboardBuilder()
    for f_col, f_name in flags:
        status = "✅" if artist[f_col] else "❌"
        kb.button(text=f"{status} {f_name}", callback_data=f"tog_{f_col}_{aid}")
        
    kb.button(text="🔙 К списку", callback_data="back_artists")
    kb.adjust(1)
    
    await c.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")

@router.callback_query(F.data.startswith("tog_"))
async def toggle_artist_flag(c: CallbackQuery):
    data = c.data.split("_")
    col = f"{data[1]}_{data[2]}" # flag_contract и т.д. содержат _, поэтому split может разбить на 3+ части
    # Мой формат: tog_{flag_col}_{aid}
    # Пример: tog_flag_contract_1
    # split("_"): ['tog', 'flag', 'contract', '1'] -> ОЙ!
    
    # Исправим парсинг
    parts = c.data.split("_")
    aid = int(parts[-1])
    col = "_".join(parts[1:-1])
    
    artist = await db.get_artist_by_id(aid)
    new_val = 0 if artist[col] else 1
    
    await db.update_artist_flag(aid, col, new_val)
    
    # Обновляем view
    await view_artist(c)

@router.callback_query(F.data == "back_artists")
async def back_to_list(c: CallbackQuery):
    await c.message.delete()
    user = await db.get_user(c.from_user.id)
    artists = await db.get_all_artists()
    text = "🎤 <b>Список артистов:</b>\n\n"
    kb = InlineKeyboardBuilder()
    for a in artists:
        status = ""
        if a['flag_contract']: status += "📝"
        if a['flag_mm_profile']: status += "🎵"
        if a['flag_mm_verify']: status += "✅"
        if a['flag_yt_link']: status += "📺"
        if a['flag_yt_note']: status += "🎼"
        kb.button(text=f"{a['name']} {status}", callback_data=f"view_art_{a['id']}")
    kb.button(text="➕ Добавить артиста", callback_data="add_artist")
    kb.adjust(1)
    await c.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
