import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import API_TOKEN, setup_logging
from bot.database import db
from bot.handlers import router as main_router
from bot.middlewares.auth import AuthMiddleware, AuthCallbackMiddleware
from bot.jobs import job_check_overdue, job_deadline_alerts, job_onboarding, job_pitching_alert, router as jobs_router

async def main():
    # Настройка логгирования
    setup_logging()
    logger = logging.getLogger(__name__)

    # Инициализация бота и диспетчера
    bot = Bot(token=API_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Подключение базы данных
    await db.connect()

    # Регистрация middleware
    dp.message.outer_middleware(AuthMiddleware())
    dp.callback_query.outer_middleware(AuthCallbackMiddleware())

    # Регистрация роутеров
    dp.include_router(main_router)
    dp.include_router(jobs_router)

    # Настройка планировщика задач
    scheduler = AsyncIOScheduler()
    scheduler.add_job(job_check_overdue, CronTrigger(minute=0), args=[bot]) # Раз в час
    scheduler.add_job(job_deadline_alerts, CronTrigger(hour='10,18'), args=[bot]) # Утро и вечер
    scheduler.add_job(job_onboarding, CronTrigger(hour=15), args=[bot])
    scheduler.add_job(job_pitching_alert, CronTrigger(hour=9), args=[bot]) # Утром, раз в день
    scheduler.start()

    # Запуск
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("BOT STARTED (ASYNC V3 - MODULAR)")
    
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
