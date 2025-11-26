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
        await m.answer("🔙 <b>Действие отменено.</b>\nВозвращаюсь в главное меню.", reply_markup=get_main_kb(user['role']), parse_mode="HTML")
    else:
        await m.answer("🔙 <b>Отменено.</b>", reply_markup=types.ReplyKeyboardRemove(), parse_mode="HTML")

@router.message(Command("start"))
async def cmd_start(m: types.Message):
    """Обработчик команды /start."""
    user = await db.get_user(m.from_user.id)
    if not user: 
        return await m.answer("⛔️ <b>Доступ запрещен.</b>\nВас нет в системе. Пожалуйста, обратитесь к администратору.", parse_mode="HTML")
    
    # Обновляем username если он изменился или не был задан
    if m.from_user.username:
        await db.add_user(m.from_user.id, user['name'], user['role'], m.from_user.username)

    role_name = ROLES_DISPLAY.get(user['role'], user['role'])
    await m.answer(
        f"👋 <b>Приветствую, {user['name']}!</b>\n\n"
        f"🎯 <b>Твоя роль:</b> <code>{role_name}</code>\n\n"
        f"👇 <b>Используй меню для навигации:</b>", 
        reply_markup=get_main_kb(user['role']), 
        parse_mode="HTML"
    )
