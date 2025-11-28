import time
from hashlib import sha256
from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Router
import asyncio
import logging
import os

# --------------------------------------------------------
# НАСТРОЙКИ
# --------------------------------------------------------

TOKEN = "8376771386:AAF3gv-snD6Yd3xrwKSBwDVo2zBvQzd45S8"

SECRET = "ajd82jhAHD828hd82hds9"     # соль для токена
PDF_SERVER_URL = "https://5.183.95.220:9100/secure-pdf"   # backend FastAPI
TOKEN_TTL = 600  # 10 минут

router = Router()


# --------------------------------------------------------
# Генерация защищённой ссылки
# --------------------------------------------------------

def generate_pdf_link(user_id: int) -> str:
    expires = int(time.time()) + TOKEN_TTL
    raw = f"{user_id}:{expires}:{SECRET}".encode()
    token = sha256(raw).hexdigest()
    token_str = f"{token}:{expires}"
    return f"{PDF_SERVER_URL}?token={token_str}"


# --------------------------------------------------------
# Команда /start
# --------------------------------------------------------

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Читать PDF", callback_data="open_pdf_secure")]
        ]
    )

    await message.answer(
        "Нажми кнопку, чтобы открыть PDF прямо внутри Telegram (без скачивания файла).",
        reply_markup=kb
    )


# --------------------------------------------------------
# Обработчик кнопки
# --------------------------------------------------------

@router.callback_query(F.data == "open_pdf_secure")
async def open_pdf_secure(callback: CallbackQuery):
    user_id = callback.from_user.id
    secure_link = generate_pdf_link(user_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📖 Открыть PDF", url=secure_link)]
        ]
    )

    await callback.message.answer(
        "Открываю PDF в безопасном режиме Telegram Viewer.",
        reply_markup=kb
    )
    await callback.answer()


# --------------------------------------------------------
# Запуск бота
# --------------------------------------------------------

async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=TOKEN, parse_mode="HTML")
    dp = Dispatcher()
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
