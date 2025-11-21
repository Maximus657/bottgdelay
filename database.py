import os
from enum import Enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Integer, String, DateTime, ForeignKey, Boolean, BigInteger, Text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Создаем движок и сессию
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

# --- ENUMS (ПЕРЕЧИСЛЕНИЯ) ---
class UserRole(str, Enum):
    FOUNDER = "Основатель"
    AR_MANAGER = "A&R-менеджер"
    DESIGNER = "Дизайнер"
    SMM = "SMM-специалист"

class TaskStatus(str, Enum):
    PENDING = "Ожидает выполнения"
    IN_PROGRESS = "В работе"
    DONE = "Выполнена"
    OVERDUE = "Просрочена"

class ReleaseType(str, Enum):
    SINGLE_80_20 = "Сингл 80/20"
    SINGLE_50_50 = "Сингл 50/50"
    ALBUM = "Альбом"

# --- МОДЕЛИ (ТАБЛИЦЫ) ---

class User(Base):
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram ID
    username: Mapped[str] = mapped_column(String, nullable=True)
    full_name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)  # Храним строкой из Enum
    
    # Вот эта колонка, которой не хватало
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class Artist(Base):
    __tablename__ = "artists"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    ar_manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    
    # Флаги онбординга
    contract_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    musixmatch_profile: Mapped[bool] = mapped_column(Boolean, default=False)
    musixmatch_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_note: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_binding: Mapped[bool] = mapped_column(Boolean, default=False)
    
    first_release_date: Mapped[datetime] = mapped_column(DateTime, nullable=True)

class Release(Base):
    __tablename__ = "releases"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String)
    release_type: Mapped[str] = mapped_column(String)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"))
    release_date: Mapped[datetime] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))

class Task(Base):
    __tablename__ = "tasks"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default=TaskStatus.PENDING)
    deadline: Mapped[datetime] = mapped_column(DateTime)
    
    assignee_id: Mapped[int] = mapped_column(ForeignKey("users.id")) # Исполнитель
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))  # Создатель
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    
    needs_file: Mapped[bool] = mapped_column(Boolean, default=False)
    file_url: Mapped[str] = mapped_column(String, nullable=True)
    
    is_regular: Mapped[bool] = mapped_column(Boolean, default=False) # Для SMM

class Report(Base):
    __tablename__ = "reports"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())