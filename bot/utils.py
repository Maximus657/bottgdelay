import logging
from aiogram import Bot

logger = logging.getLogger(__name__)

async def notify_user(bot: Bot, uid, text, reply_markup=None):
    """
    Отправляет уведомление пользователю.
    :param bot: экземпляр бота
    :param uid: Telegram ID пользователя
    :param text: Текст сообщения
    :param reply_markup: Клавиатура (опционально)
    """
    try: 
        await bot.send_message(uid, text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e: 
        logger.warning(f"Failed to notify {uid}: {e}")
