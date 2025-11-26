import datetime
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import F, Bot
from aiogram.types import CallbackQuery

from bot.database import db
from bot.utils import notify_user

async def job_check_overdue(bot: Bot):
    """Проверка просроченных задач."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    tasks = await db.get_overdue_tasks(today)
    for t in tasks:
        if t['status'] != 'overdue':
            await db.mark_task_overdue(t['id'])
        await notify_user(bot, t['assigned_to'], f"⚠️ <b>ПРОСРОЧЕНО!</b>\n📌 {t['title']}")

async def job_deadline_alerts(bot: Bot):
    """Уведомления о дедлайнах (менее 24 часов)."""
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = await db.get_deadline_tasks(tomorrow)
    for t in tasks: 
        await notify_user(bot, t['assigned_to'], f"⏰ <b>Дедлайн < 24ч!</b>\n📌 {t['title']}")

async def job_onboarding(bot: Bot):
    """Напоминание о контрактах (онбординг)."""
    artists = await db.get_unsigned_artists()
    for a in artists:
        kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_cont_{a['id']}").button(text="Позже", callback_data="ign")
        await notify_user(bot, a['manager_id'], f"📝 Контракт с <b>{a['name']}</b> подписан?", kb.as_markup())

# Callbacks for onboarding
from aiogram import Router
router = Router()

@router.callback_query(F.data.startswith("onb_"))
async def onb_act(c: CallbackQuery):
    col = {'cont': 'flag_contract'}.get(c.data.split("_")[1])
    if col:
        await db.update_artist_flag(int(c.data.split("_")[2]), col)
        await c.message.edit_text("✅ Обновлено!")

@router.callback_query(F.data == "ign")
async def ign(c: CallbackQuery): await c.message.delete()
