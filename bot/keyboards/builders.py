from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

def get_cancel_kb(): 
    """Возвращает клавиатуру с кнопкой отмены."""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔙 Отмена")]], resize_keyboard=True)

def get_main_kb(role):
    """
    Возвращает главную клавиатуру в зависимости от роли пользователя.
    """
    kb = []
    if role == 'founder':
        kb = [
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🎤 Артисты")],
            [KeyboardButton(text="➕ Добавить юзера"), KeyboardButton(text="💿 Все релизы")],
            [KeyboardButton(text="💿 Создать релиз"), KeyboardButton(text="➕ Создать задачу")],
            [KeyboardButton(text="📋 Активные задачи"), KeyboardButton(text="📜 История всех задач")]
        ]
    elif role == 'anr':
        kb = [
            [KeyboardButton(text="💿 Создать релиз"), KeyboardButton(text="🎤 Артисты")],
            [KeyboardButton(text="💿 Мои релизы"), KeyboardButton(text="➕ Создать задачу")],
            [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📜 История")]
        ]
    elif role == 'designer':
        kb = [[KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📜 История")], [KeyboardButton(text="🕰 Просроченные")]]
    elif role == 'smm':
        kb = [[KeyboardButton(text="📝 Написать отчет"), KeyboardButton(text="📅 Мои отчеты")],
              [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="📜 История")]]
    
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
