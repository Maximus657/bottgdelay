import datetime
from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext

from bot.database import db
from bot.states import SMMReportState
from bot.keyboards.builders import get_cancel_kb, get_main_kb
from bot.config import ADMIN_IDS
from bot.utils import notify_user

router = Router()

@router.message(F.text == "📝 Написать отчет")
async def smm_start(m: types.Message, state: FSMContext):
    """Начало создания SMM отчета."""
    await m.answer("✍️ Текст:", reply_markup=get_cancel_kb())
    await state.set_state(SMMReportState.text)

@router.message(SMMReportState.text)
async def smm_save(m: types.Message, state: FSMContext, bot: Bot):
    """Сохранение и отправка SMM отчета."""
    if m.text == "🔙 Отмена":
        await state.clear()
        user = await db.get_user(m.from_user.id)
        await m.answer("❌ Отменено.", reply_markup=get_main_kb(user['role']))
        return
    
    await db.create_report(m.from_user.id, datetime.date.today(), m.text)
    
    reporter = await db.get_user_link(m.from_user.id)
    report_msg = (
        f"📊 <b>НОВЫЙ SMM ОТЧЕТ</b>\n"
        f"👤 От: {reporter}\n"
        f"📅 Дата: {datetime.date.today()}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{m.text}"
    )
    for admin_id in ADMIN_IDS:
        await notify_user(bot, admin_id, report_msg)

    await m.answer("✅ Отчет сохранен и отправлен руководству.", reply_markup=get_main_kb('smm'))
    await state.clear()

@router.message(F.text == "📅 Мои отчеты")
async def smm_list(m: types.Message):
    """Просмотр последних отчетов пользователя."""
    reps = await db.get_reports(m.from_user.id)
    await m.answer("\n".join([f"📅 <b>{r['report_date']}</b>: {r['text']}" for r in reps]) if reps else "Пусто.", parse_mode="HTML")
