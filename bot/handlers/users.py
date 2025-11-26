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
        text += f"🔹 <b>{u['name']}</b> {un}\n└ Роль: <code>{role_nice}</code> | <a href='tg://user?id={u['telegram_id']}'>Профиль</a>\n\n"
    await m.answer(text, parse_mode="HTML")

@router.message(F.text == "➕ Добавить юзера")
async def add_user_step1(m: types.Message, state: FSMContext):
    """Начало добавления пользователя: ввод ID."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'founder': return
    await m.answer("🆔 <b>Введите Telegram ID сотрудника:</b>\n(Числовой ID, например: 123456789)", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(AddUser.tg_id)

@router.message(AddUser.tg_id)
async def add_user_step2(m: types.Message, state: FSMContext):
    """Ввод имени пользователя."""
    if not m.text.isdigit(): return await m.answer("⚠️ <b>Ошибка:</b> ID должен состоять только из цифр.")
    await state.update_data(uid=m.text)
    await m.answer("👤 <b>Введите Имя сотрудника:</b>\n(Как к нему обращаться в боте)", reply_markup=get_cancel_kb(), parse_mode="HTML")
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
    await m.answer("🎭 <b>Выберите Роль сотрудника:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(AddUser.role)

@router.message(AddUser.role)
async def add_user_finish(m: types.Message, state: FSMContext, bot: Bot):
    """Завершение добавления пользователя."""
    role_code = ROLES_MAP.get(m.text)
    if not role_code: return await m.answer("⚠️ Пожалуйста, выберите роль, используя кнопки.")
    data = await state.get_data()
    
    await db.add_user(int(data['uid']), data['name'], role_code)
    
    await m.answer(f"✅ <b>Сотрудник добавлен!</b>\n\n👤 Имя: {data['name']}\n🎭 Роль: {m.text}", reply_markup=get_main_kb('founder'), parse_mode="HTML")
    try:
        await notify_user(bot, int(data['uid']), f"🎉 <b>Добро пожаловать в команду!</b>\n\nВаша роль: <b>{m.text}</b>\nНажмите /start для начала работы.")
    except:
        await m.answer("⚠️ Не удалось отправить уведомление пользователю (возможно, бот не запущен у него).")
    await state.clear()

@router.message(F.text == "🗑 Удалить юзера")
async def delete_user_start(m: types.Message):
    """Начало удаления пользователя."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'founder': return
    
    users = await db.get_all_users()
    users = [u for u in users if u['role'] != 'founder']
    
    if not users: return await m.answer("📭 <b>Список пуст.</b>\nУдалять некого, кроме вас.")
    
    kb = InlineKeyboardBuilder()
    for u in users: kb.button(text=f"❌ {u['name']}", callback_data=f"rm_usr_{u['telegram_id']}")
    kb.adjust(1)
    await m.answer("🗑 <b>Выберите сотрудника для удаления:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")

@router.callback_query(F.data.startswith("rm_usr_"))
async def delete_user_confirm(c: CallbackQuery):
    """Подтверждение удаления пользователя."""
    uid = int(c.data.split("_")[2])
    await db.delete_user(uid)
    await c.message.edit_text("🗑 <b>Пользователь удален.</b>", parse_mode="HTML")
