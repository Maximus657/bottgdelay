from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import db
from bot.states import AddUser
from bot.keyboards.builders import get_cancel_kb, get_main_kb
from bot.config import ROLES_MAP, ROLES_DISPLAY
from bot.utils import notify_user

router = Router()

@router.message(F.text == "👥 Пользователи")
async def list_users(m: types.Message):
    """Выводит список всех пользователей."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'founder': return
    
    users = await db.get_all_users()
    text = "👥 <b>Команда лейбла:</b>\n\n"
    for u in users:
        role_nice = ROLES_DISPLAY.get(u['role'], u['role'])
        un = f"(@{u['username']})" if u.get('username') else ""
        text += f"🔹 <a href='tg://user?id={u['telegram_id']}'>{u['name']}</a> {un} — <code>{role_nice}</code>\n"
    await m.answer(text, parse_mode="HTML")

@router.message(F.text == "➕ Добавить юзера")
async def add_user_step1(m: types.Message, state: FSMContext):
    """Начало добавления пользователя: ввод ID."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'founder': return
    await m.answer("🆔 Введите <b>Telegram ID</b>:", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(AddUser.tg_id)

@router.message(AddUser.tg_id)
async def add_user_step2(m: types.Message, state: FSMContext):
    """Ввод имени пользователя."""
    if not m.text.isdigit(): return await m.answer("⚠️ ID должен быть числом.")
    await state.update_data(uid=m.text)
    await m.answer("👤 Введите <b>Имя сотрудника</b>:", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(AddUser.name)

@router.message(AddUser.name)
async def add_user_step3(m: types.Message, state: FSMContext):
    """Выбор роли пользователя."""
    await state.update_data(name=m.text)
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👑 Основатель"), KeyboardButton(text="🎧 A&R Менеджер")],
        [KeyboardButton(text="🎨 Дизайнер"), KeyboardButton(text="📱 SMM Специалист")],
        [KeyboardButton(text="🔙 Отмена")]
    ], resize_keyboard=True)
    await m.answer("🎭 Выберите <b>Роль</b>:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(AddUser.role)

@router.message(AddUser.role)
async def add_user_finish(m: types.Message, state: FSMContext, bot: Bot):
    """Завершение добавления пользователя."""
    role_code = ROLES_MAP.get(m.text)
    if not role_code: return await m.answer("⚠️ Выберите роль кнопкой.")
    data = await state.get_data()
    
    await db.add_user(int(data['uid']), data['name'], role_code)
    
    await m.answer(f"✅ <b>{data['name']}</b> добавлен!", reply_markup=get_main_kb('founder'), parse_mode="HTML")
    await notify_user(bot, int(data['uid']), f"🎉 <b>Добро пожаловать!</b>\nРоль: {m.text}\nНажмите /start для начала работы.")
    await state.clear()

@router.message(F.text == "🗑 Удалить юзера")
async def delete_user_start(m: types.Message):
    """Начало удаления пользователя."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'founder': return
    
    # Получаем всех кроме фаундера
    # Т.к. get_all_users возвращает всех, фильтруем в коде или нужен новый метод в БД
    # Для простоты отфильтруем в Python, но лучше в SQL
    # Используем прямой запрос через pool в db методе, но тут нет метода "get_all_except_founder"
    # Добавим логику фильтрации
    users = await db.get_all_users()
    users = [u for u in users if u['role'] != 'founder']
    
    if not users: return await m.answer("Удалять некого.")
    
    kb = InlineKeyboardBuilder()
    for u in users: kb.button(text=f"❌ {u['name']}", callback_data=f"rm_usr_{u['telegram_id']}")
    kb.adjust(1)
    await m.answer("Кого удалить?", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("rm_usr_"))
async def delete_user_confirm(c: CallbackQuery):
    """Подтверждение удаления пользователя."""
    uid = int(c.data.split("_")[2])
    await db.delete_user(uid)
    await c.message.edit_text("🗑 Пользователь удален.")
