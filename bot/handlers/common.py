from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from bot.database import db
from bot.keyboards.builders import get_main_kb
from bot.config import ROLES_DISPLAY

router = Router()

@router.message(F.text == "🔙 Отмена")
async def cancel_handler(m: types.Message, state: FSMContext):
    """Обработчик отмены действия."""
    await state.clear()
    user = await db.get_user(m.from_user.id)
    if user:
        await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))
    else:
        await m.answer("❌ Отменено.", reply_markup=types.ReplyKeyboardRemove())

@router.message(Command("start"))
async def cmd_start(m: types.Message):
    """Обработчик команды /start."""
    user = await db.get_user(m.from_user.id)
    if not user: 
        return await m.answer("⛔️ Вас нет в системе. Попросите администратора добавить ваш ID.")
    
    # Обновляем username если он изменился или не был задан
    if m.from_user.username:
        await db.add_user(m.from_user.id, user['name'], user['role'], m.from_user.username)

    role_name = ROLES_DISPLAY.get(user['role'], user['role'])
    await m.answer(f"👋 Привет, <b>{user['name']}</b>!\nРоль: <code>{role_name}</code>", reply_markup=get_main_kb(user['role']), parse_mode="HTML")
