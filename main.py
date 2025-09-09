import os
import asyncio
import uuid
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (KeyboardButton, ReplyKeyboardMarkup, 
                          InlineKeyboardMarkup, InlineKeyboardButton, 
                          BotCommand, CallbackQuery, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from sqlalchemy import Column, Integer, BigInteger, String, Text as SQLText, DateTime, Boolean, select, func, ForeignKey
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker

# ========== CONFIG ==========
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./anonbot.db")

if not BOT_TOKEN or BOT_TOKEN == "YOUR_TOKEN_HERE":
    raise RuntimeError("Укажи BOT_TOKEN в .env или в окружении")

# ========== DB ==========
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, index=True, nullable=True)
    username = Column(String(128), unique=True, index=True, nullable=True)
    unique_code = Column(String(32), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    messages = relationship("Message", back_populates="receiver", cascade="all,delete-orphan")

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    sender_tg_id = Column(BigInteger, nullable=True)
    receiver_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(SQLText, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    delivered = Column(Boolean, default=False, nullable=False)
    
    receiver = relationship("User", back_populates="messages")

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ========== Keyboards ==========
def reply_button(message_id: int):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"reply:{message_id}")]
        ]
    )
    return kb

# ========== FSM ==========
class Flow(StatesGroup):
    awaiting_username = State()
    awaiting_message_for_username = State()
    awaiting_message_by_link = State()
    awaiting_reply = State()

# ========== Texts & helpers ==========
MAX_TG_MESSAGE_LEN = 4096

INVITE_TEXT = "✍️ Напиши анонимное сообщение — оно будет отправлено или сохранено для будущей доставки."
SENT_TEXT = "✅ Готово! Жди ответ в этом чате."
INBOX_PREFIX = "📥 Вам прилетело анонимное послание:\n\n"
REPLY_BUTTON_TEXT = "✏️ Ответить"
REPLY_PROMPT = "💬 Напиши свой ответ — мы отправим его автору."
REPLY_SENT = "🚀 Ответ уже у адресата."
REPLY_INBOX_PREFIX = "📨 Вам прислали ответ:\n\n"
BAD_LINK = "❌ Ссылка несуществующая или больше не активна."
SELF_LINK_NOTE = "ℹ️ Это твоя личная ссылка. Отправь её другим, чтобы получать анонимки."
NOT_REGISTERED_NOTICE = (
    "💾 Сообщение сохранено. Получатель пока не зарегистрирован в боте.\n"
    "Когда он зарегистрируется — мы доставим письмо и уведомим тебя."
)

def clamp(text: str, max_len: int = MAX_TG_MESSAGE_LEN - 100) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_len else text[:max_len-1] + "…"

async def ensure_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ========== Bot init ==========
bot = Bot(BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Основное меню
menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✉️ Написать по username"), KeyboardButton(text="🔗 Моя ссылка")]
    ],
    resize_keyboard=True
)

# Клавиатура с кнопкой отмены
cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

# ========== DB helpers ==========
async def generate_unique_code(session: AsyncSession, length: int = 8) -> str:
    while True:
        code = uuid.uuid4().hex[:length]
        r = await session.execute(select(func.count()).select_from(User).where(User.unique_code == code))
        if r.scalar_one() == 0:
            return code

async def get_user_by_tg(session: AsyncSession, tg_id: int):
    r = await session.execute(select(User).where(User.tg_id == tg_id))
    return r.scalars().first()

async def get_user_by_username(session: AsyncSession, username: str):
    if not username:
        return None
    r = await session.execute(select(User).where(User.username == username))
    return r.scalars().first()

async def get_user_by_code(session: AsyncSession, code: str):
    r = await session.execute(select(User).where(User.unique_code == code))
    return r.scalars().first()

async def create_placeholder_by_username(session: AsyncSession, username: str):
    user = await get_user_by_username(session, username)
    if user:
        return user
    code = await generate_unique_code(session)
    user = User(tg_id=None, username=username, unique_code=code)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user

async def create_or_update_user_on_start(session: AsyncSession, tg_id: int, username: str | None):
    user = await get_user_by_tg(session, tg_id)
    if user:
        if username and user.username != username:
            user.username = username
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user
    
    if username:
        placeholder = await get_user_by_username(session, username)
        if placeholder and placeholder.tg_id is None:
            placeholder.tg_id = tg_id
            session.add(placeholder)
            await session.commit()
            await session.refresh(placeholder)
            return placeholder
    
    code = await generate_unique_code(session)
    user = User(tg_id=tg_id, username=username, unique_code=code)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user

# ========== Handlers ==========
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    username = message.from_user.username
    tg_id = message.from_user.id
    async with AsyncSessionLocal() as s:
        user = await create_or_update_user_on_start(s, tg_id, username)
        res = await s.execute(select(Message).where(Message.receiver_user_id == user.id, Message.delivered == False).order_by(Message.created_at))
        pending = res.scalars().all()

    me = await bot.me()
    link = f"https://t.me/{me.username}?start={user.unique_code}"

    await message.answer(
        f"Привет, {message.from_user.first_name or 'друг'}!\nВот твоя персональная ссылка:\n{link}",
        reply_markup=menu_kb
    )

    if pending:
        await message.answer(f"📬 У тебя есть {len(pending)} сообщений, полученных до регистрации — доставляю их:")
        async with AsyncSessionLocal() as s:
            for msg in pending:
                try:
                    kb = InlineKeyboardBuilder()
                    kb.button(text=REPLY_BUTTON_TEXT, callback_data=f"reply_msg:{msg.id}")
                    await bot.send_message(user.tg_id, INBOX_PREFIX + f"[{msg.created_at.strftime('%Y-%m-%d %H:%M')}] \n{msg.text}", reply_markup=kb.as_markup())
                    msg.delivered = True
                    s.add(msg)
                    await s.commit()
                except Exception:
                    pass
                if msg.sender_tg_id:
                    try:
                        await bot.send_message(msg.sender_tg_id, f"✅ Твоё сообщение, отправленное {msg.created_at.strftime('%Y-%m-%d %H:%M')}, доставлено получателю после его регистрации.")
                    except Exception:
                        pass

@dp.message(Command(commands=["my", "link"]))
async def cmd_my(message: types.Message):
    async with AsyncSessionLocal() as s:
        user = await get_user_by_tg(s, message.from_user.id)
        if not user:
            user = await create_or_update_user_on_start(s, message.from_user.id, message.from_user.username)
    me = await bot.me()
    await message.answer(f"Твоя ссылка:\nhttps://t.me/{me.username}?start={user.unique_code}", reply_markup=menu_kb)

@dp.message(F.text == "✉️ Написать по username")
async def menu_send_username(message: types.Message, state: FSMContext):
    await state.set_state(Flow.awaiting_username)
    await message.answer("Введи username получателя без @ (например: ivan_ivanov).", reply_markup=cancel_kb)

@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
    
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=menu_kb)

@dp.message(Flow.awaiting_username)
async def got_username(message: types.Message, state: FSMContext):
    # Обработка отмены
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=menu_kb)
        return
        
    # Обработка других команд меню
    if message.text in ["✉️ Написать по username", "🔗 Моя ссылка"]:
        await state.clear()
        if message.text == "✉️ Написать по username":
            await menu_send_username(message, state)
        else:
            await cmd_my(message)
        return
        
    username = (message.text or "").strip().lstrip("@")
    if not username:
        await message.answer("Неверный username. Попробуй ещё раз.", reply_markup=cancel_kb)
        return
        
    # Дополнительная проверка на валидность username
    if not re.match(r'^[a-zA-Z0-9_]{5,32}$', username):
        await message.answer("Неверный формат username. Используйте только латинские буквы, цифры и подчеркивания, от 5 до 32 символов.", reply_markup=cancel_kb)
        return
        
    # create placeholder if needed and save receiver id in state
    async with AsyncSessionLocal() as s:
        user = await create_placeholder_by_username(s, username)
    await state.update_data(receiver_user_id=user.id, receiver_tg_id=user.tg_id, receiver_username=username)
    await state.set_state(Flow.awaiting_message_for_username)
    await message.answer(f"Отлично. Теперь напиши анонимное сообщение для @{username}.", reply_markup=ReplyKeyboardRemove())

@dp.message(Command(commands=["send"]))
async def cmd_send_inline(message: types.Message, state: FSMContext):
    # Очищаем предыдущее состояние, если было
    await state.clear()
    
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Используй: /send @username")
        return
    username = parts[1].lstrip("@").strip()
    if not username:
        await message.answer("Неверный username.")
        return
    async with AsyncSessionLocal() as s:
        user = await create_placeholder_by_username(s, username)
    await state.update_data(receiver_user_id=user.id, receiver_tg_id=user.tg_id, receiver_username=username)
    await state.set_state(Flow.awaiting_message_for_username)
    await message.answer(f"Отправляешь анонимку @{username}. Напиши текст сообщения.")

@dp.message(Flow.awaiting_message_for_username)
async def got_message_for_username(message: types.Message, state: FSMContext):
    # Обработка отмены
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=menu_kb)
        return
        
    # Обработка других команд меню
    if message.text in ["✉️ Написать по username", "🔗 Моя ссылка"]:
        await state.clear()
        if message.text == "✉️ Написать по username":
            await menu_send_username(message, state)
        else:
            await cmd_my(message)
        return
        
    data = await state.get_data()
    receiver_user_id = data.get("receiver_user_id")
    receiver_tg_id = data.get("receiver_tg_id")
    receiver_username = data.get("receiver_username")
    if not receiver_user_id:
        await message.answer("Ошибка — не найден получатель. Попробуй ещё раз.")
        await state.clear()
        return
    text = clamp(message.text or message.caption or "")
    if not text:
        await message.answer("Сообщение пустое. Напечатай что-нибудь.")
        return

    async with AsyncSessionLocal() as s:
        msg = Message(sender_tg_id=message.from_user.id, receiver_user_id=receiver_user_id, text=text, delivered=False)
        s.add(msg)
        await s.commit()
        await s.refresh(msg)

        if receiver_tg_id:
            try:
                kb = InlineKeyboardBuilder()
                kb.button(text=REPLY_BUTTON_TEXT, callback_data=f"reply_msg:{msg.id}")
                await bot.send_message(receiver_tg_id, INBOX_PREFIX + text, reply_markup=kb.as_markup())
                msg.delivered = True
                s.add(msg)
                await s.commit()
                try:
                    await bot.send_message(message.from_user.id, "✅ Сообщение доставлено получателю.")
                except Exception:
                    pass
            except Exception:
                pass
        else:
            await message.answer(NOT_REGISTERED_NOTICE)

    await state.clear()
    await message.answer("Сообщение отправлено!", reply_markup=menu_kb)

@dp.message(F.text.startswith("/start "))
async def deeplink_start(message: types.Message, state: FSMContext):
    try:
        code = message.text.split(maxsplit=1)[1].strip()
    except Exception:
        await message.answer(BAD_LINK)
        return
    async with AsyncSessionLocal() as s:
        owner = await get_user_by_code(s, code)
    if not owner:
        await message.answer(BAD_LINK)
        return
    if owner.tg_id == message.from_user.id:
        await message.answer(SELF_LINK_NOTE)
        return
    await state.update_data(receiver_user_id=owner.id, receiver_tg_id=owner.tg_id, receiver_username=owner.username)
    await state.set_state(Flow.awaiting_message_by_link)
    await message.answer(INVITE_TEXT)

@dp.message(Flow.awaiting_message_by_link)
async def got_message_by_link(message: types.Message, state: FSMContext):
    # Обработка отмены
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=menu_kb)
        return
        
    # Обработка других команд меню
    if message.text in ["✉️ Написать по username", "🔗 Моя ссылка"]:
        await state.clear()
        if message.text == "✉️ Написать по username":
            await menu_send_username(message, state)
        else:
            await cmd_my(message)
        return
        
    data = await state.get_data()
    receiver_user_id = data.get("receiver_user_id")
    receiver_tg_id = data.get("receiver_tg_id")
    if not receiver_user_id:
        await message.answer(BAD_LINK)
        await state.clear()
        return
    text = clamp(message.text or message.caption or "")
    if not text:
        await message.answer("Сообщение пустое. Напечатай что-нибудь.")
        return

    async with AsyncSessionLocal() as s:
        msg = Message(sender_tg_id=message.from_user.id, receiver_user_id=receiver_user_id, text=text, delivered=False)
        s.add(msg)
        await s.commit()
        await s.refresh(msg)

        if receiver_tg_id:
            try:
                kb = InlineKeyboardBuilder()
                kb.button(text=REPLY_BUTTON_TEXT, callback_data=f"reply_msg:{msg.id}")
                await bot.send_message(receiver_tg_id, INBOX_PREFIX + text, reply_markup=kb.as_markup())
                msg.delivered = True
                s.add(msg)
                await s.commit()
                try:
                    await bot.send_message(message.from_user.id, "✅ Сообщение доставлено получателю.")
                except Exception:
                    pass
            except Exception:
                pass
        else:
            await message.answer(NOT_REGISTERED_NOTICE)

    await state.clear()
    await message.answer("Сообщение отправлено!", reply_markup=menu_kb)

@dp.callback_query(F.data.startswith("reply_msg:"))
async def on_reply_click(callback: types.CallbackQuery, state: FSMContext):
    payload = callback.data.split(":", 1)[1]
    try:
        msg_id = int(payload)
    except Exception:
        await callback.answer("Неверный идентификатор сообщения.", show_alert=True)
        return
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(Message).where(Message.id == msg_id))
        orig = res.scalars().first()
    if not orig:
        await callback.answer("Сообщение не найдено.", show_alert=True)
        return
    if not orig.sender_tg_id:
        await callback.answer("Нельзя ответить этому сообщению — автор полностью анонимен.", show_alert=True)
        return
    await state.update_data(target_sender_tg_id=orig.sender_tg_id)
    await state.set_state(Flow.awaiting_reply)
    await callback.message.answer(REPLY_PROMPT, reply_markup=cancel_kb)
    await callback.answer()

@dp.message(Flow.awaiting_reply)
async def got_reply_text(message: types.Message, state: FSMContext):
    # Обработка отмены
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=menu_kb)
        return
        
    # Обработка других команд меню
    if message.text in ["✉️ Написать по username", "🔗 Моя ссылка"]:
        await state.clear()
        if message.text == "✉️ Написать по username":
            await menu_send_username(message, state)
        else:
            await cmd_my(message)
        return
        
    data = await state.get_data()
    target_tg = data.get("target_sender_tg_id")
    if not target_tg:
        await message.answer("Не удалось найти адресата. Попробуй снова через кнопку.")
        await state.clear()
        return
    text = clamp(message.text or message.caption or "")
    if not text:
        await message.answer("Ответ пустой. Напиши текст и отправь.")
        return
    try:
        await bot.send_message(target_tg, REPLY_INBOX_PREFIX + text)
        await message.answer(REPLY_SENT, reply_markup=menu_kb)
    except Exception:
        await message.answer("Не удалось отправить ответ — возможно, пользователь тебя заблокировал.", reply_markup=menu_kb)
    await state.clear()

@dp.message(F.text == "🔗 Моя ссылка")
async def menu_my_link(message: types.Message, state: FSMContext):
    # Очищаем состояние, если было
    await state.clear()
    await cmd_my(message)

async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="Старт / регистрация"),
        BotCommand(command="my", description="Показать мою ссылку"),
        BotCommand(command="send", description="/send @username — отправить по username"),
    ]
    await bot.set_my_commands(commands)

# ========== Main ==========
async def main():
    await ensure_schema()
    await set_bot_commands()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass