import datetime
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import db
from bot.states import SMMReportState
from bot.keyboards.builders import get_main_kb, get_cancel_kb

router = Router()

# --- СОЗДАНИЕ ОТЧЕТА ---
@router.message(F.text == "📊 Отправить отчет")
async def report_start(m: types.Message, state: FSMContext):
    """Начало создания ежедневного отчета."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'smm':
        return await m.answer("⛔️ Только для SMM специалистов.")
    
    await m.answer("📝 <b>Напишите текст отчета за сегодня:</b>\n(Что было сделано, какие метрики и т.д.)", reply_markup=get_cancel_kb(), parse_mode="HTML")
    await state.set_state(SMMReportState.text)

@router.message(SMMReportState.text)
async def report_submit(m: types.Message, state: FSMContext):
    """Сохранение отчета."""
    if m.text == "🔙 Отмена":
        await state.clear()
        user = await db.get_user(m.from_user.id)
        return await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))

    today = datetime.date.today().strftime("%Y-%m-%d")
    await db.create_report(m.from_user.id, today, m.text)
    
    user = await db.get_user(m.from_user.id)
    await m.answer("✅ <b>Отчет принят!</b>", reply_markup=get_main_kb(user['role']), parse_mode="HTML")
    await state.clear()

# --- ПРОСМОТР ОТЧЕТОВ ---
@router.message(F.text == "🗂 Мои отчеты")
async def report_history(m: types.Message):
    """Просмотр последних отчетов."""
    user = await db.get_user(m.from_user.id)
    if user['role'] != 'smm': return

    reports = await db.get_reports(m.from_user.id)
    if not reports:
        return await m.answer("📭 У вас пока нет отчетов.")

    text = "🗂 <b>Ваши последние отчеты:</b>\n\n"
    for r in reports:
        text += f"📅 <b>{r['report_date']}</b>\n{r['text']}\n━━━━━━━━━━━━━━━━\n"
    
    # Если текст слишком длинный, телеграм его обрежет, но пока оставим так.
    # В идеале нужна пагинация, если отчетов много.
    if len(text) > 4000:
        text = text[:4000] + "\n... (слишком много данных)"
        
    await m.answer(text, parse_mode="HTML")
