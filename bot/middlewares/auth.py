from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from typing import Callable, Dict, Any, Awaitable
from bot.database import db

class AuthMiddleware(BaseMiddleware):
    """
    Middleware для проверки регистрации пользователя в БД.
    """
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if event.text == "/start": 
            return await handler(event, data)
        
        if event.from_user:
            user = await db.get_user(event.from_user.id)
            if not user:
                await event.answer("⛔️ <b>Доступ запрещен.</b>\nОбратитесь к администратору.", parse_mode="HTML")
                return
        return await handler(event, data)

class AuthCallbackMiddleware(BaseMiddleware):
    """
    Middleware для проверки регистрации пользователя в БД (для колбэков).
    """
    async def __call__(
        self,
        handler: Callable[[CallbackQuery, Dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        if event.from_user:
            user = await db.get_user(event.from_user.id)
            if not user:
                await event.answer("⛔️ Доступ запрещен.", show_alert=True)
                return
        return await handler(event, data)
