import datetime
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import F, Bot, Router
from aiogram.types import CallbackQuery

from bot.database import db
from bot.utils import notify_user
from bot.config import ADMIN_IDS

router = Router()

async def job_check_overdue(bot: Bot):
    """Проверка просроченных задач (Ежечасно)."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    tasks = await db.get_overdue_tasks(today)
    for t in tasks:
        if t['status'] != 'overdue':
            await db.mark_task_overdue(t['id'])
        await notify_user(bot, t['assigned_to'], f"⚠️ <b>ПРОСРОЧЕНО!</b>\n📌 {t['title']}")

async def job_deadline_alerts(bot: Bot):
    """Уведомления о дедлайнах (Утро/Вечер)."""
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = await db.get_deadline_tasks(tomorrow)
    for t in tasks: 
        await notify_user(bot, t['assigned_to'], f"⏰ <b>Дедлайн < 24ч!</b>\n📌 {t['title']}")

async def job_pitching_alert(bot: Bot):
    """Срочный алерт по питчингу (За 3 дня до релиза)."""
    # Ищем релизы, которые выходят через 3 дня
    releases = await db.get_upcoming_releases(days_ahead=3)
    for r in releases:
        task = await db.get_release_pitching_task(r['id'])
        if task and task['status'] != 'done':
            # Уведомляем всех основателей
            msg = f"🚨 <b>СРОЧНО! ПИТЧИНГ!</b>\nРелиз: {r['title']}\nДо релиза 3 дня, задача не закрыта!"
            for admin_id in ADMIN_IDS:
                await notify_user(bot, admin_id, msg)

async def job_onboarding(bot: Bot):
    """Автоматизированный онбординг (Ежедневно)."""
    
    # 1. Контракт (Ежедневно)
    artists_contract = await db.get_artists_by_flag('flag_contract', 0)
    for a in artists_contract:
        kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_cont_{a['id']}").button(text="Позже", callback_data="ign")
        await notify_user(bot, a['manager_id'], f"📝 Контракт с <b>{a['name']}</b> подписан?", kb.as_markup())

    # 2. Musixmatch Profile (Еженедельно - тут упростим до ежедневной проверки, но можно добавить логику дня недели)
    # Проверяем только если контракт подписан (flag_contract=1) и профиля нет (flag_mm_profile=0)
    # Сложный запрос, сделаем фильтрацию в python или расширим get_artists_by_flag
    # Лучше сделать SQL запрос в DB, но пока используем то что есть, фильтруем в коде
    
    # Получаем всех, у кого контракт подписан
    signed_artists = await db.get_artists_by_flag('flag_contract', 1)
    
    for a in signed_artists:
        kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_mmp_{a['id']}").button(text="Позже", callback_data="ign")
        
        if a['flag_mm_profile'] == 0:
             await notify_user(bot, a['manager_id'], f"🎵 Профиль <b>Musixmatch</b> для {a['name']} создан?", kb.as_markup())
        
        elif a['flag_mm_verify'] == 0:
            kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_mmv_{a['id']}").button(text="Позже", callback_data="ign")
            await notify_user(bot, a['manager_id'], f"✅ Профиль <b>Musixmatch</b> для {a['name']} верифицирован?", kb.as_markup())
            
        elif a['flag_yt_link'] == 0:
            kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_ytl_{a['id']}").button(text="Позже", callback_data="ign")
            await notify_user(bot, a['manager_id'], f"📺 Заявка на привязку канала <b>YouTube</b> для {a['name']} подана?", kb.as_markup())
            
        elif a['flag_yt_note'] == 0:
             # Проверяем дату первого релиза
             if a['first_release_date']:
                 try:
                     r_date = datetime.datetime.strptime(a['first_release_date'], "%Y-%m-%d").date()
                     if datetime.date.today() >= r_date:
                        kb = InlineKeyboardBuilder().button(text="✅ Да", callback_data=f"onb_ytn_{a['id']}").button(text="Позже", callback_data="ign")
                        await notify_user(bot, a['manager_id'], f"🎼 Заявка на <b>YouTube Нотку</b> для {a['name']} подана?", kb.as_markup())
                 except: pass

# --- CALLBACKS ---
@router.callback_query(F.data.startswith("onb_"))
async def onb_act(c: CallbackQuery):
    action = c.data.split("_")[1]
    artist_id = int(c.data.split("_")[2])
    
    col_map = {
        'cont': 'flag_contract',
        'mmp': 'flag_mm_profile',
        'mmv': 'flag_mm_verify',
        'ytl': 'flag_yt_link',
        'ytn': 'flag_yt_note'
    }
    
    col = col_map.get(action)
    if col:
        await db.update_artist_flag(artist_id, col)
        await c.message.edit_text("✅ Статус обновлен! Двигаемся дальше.")
    else:
        await c.answer("Ошибка")

@router.callback_query(F.data == "ign")
async def ign(c: CallbackQuery): await c.message.delete()
