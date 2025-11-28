import asyncio
import os

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ВСТАВЬ сюда токен тестового бота
BOT_TOKEN = os.getenv("PDF_VIEWER_TEST_BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_БОТА")

PDF_URL = "https://xn----8sbnaardarcyey0i.xn--p1ai/wp-content/uploads/2025/11/Childhood-trauma-in.pdf"  # ← сюда вставь реальный URL на PDF


router = Router()


@router.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    text = (
        "Это тестовая читалка PDF внутри Telegram.\n\n"
        "Нажми кнопку ниже, Telegram откроет PDF во встроенном просмотрщике."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть PDF",
                    url=PDF_URL
                )
            ]
        ]
    )

    await message.answer(text, reply_markup=keyboard)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
