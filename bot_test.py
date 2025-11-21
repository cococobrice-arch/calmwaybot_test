import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
)
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
LINK = os.getenv("LINK_TO_MATERIAL")
VIDEO_NOTE_FILE_ID = os.getenv("VIDEO_NOTE_FILE_ID")
DB_PATH = os.getenv("DATABASE_PATH", "users.db")
CHANNEL_USERNAME = "@OcdAndAnxiety"

MODE = os.getenv("MODE", "prod").lower()
FAST_USER_ID_RAW = os.getenv("FAST_USER_ID", "")
FAST_USER_ID = int(FAST_USER_ID_RAW) if FAST_USER_ID_RAW.isdigit() else None

SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL", "10"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =========================================================
# DB INIT
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            source TEXT,
            step TEXT,
            subscribed INTEGER DEFAULT 0,
            last_action TEXT,
            username TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS answers (
            user_id INTEGER,
            question INTEGER,
            answer TEXT,
            PRIMARY KEY (user_id, question)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            timestamp TEXT,
            action TEXT,
            details TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            send_at TEXT,
            kind TEXT,
            payload TEXT,
            delivered INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


def log_event(user_id: int, action: str, details: str | None = None):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO events (user_id, timestamp, action, details) VALUES (?, ?, ?, ?)",
        (user_id, datetime.now().isoformat(timespec="seconds"), action, details),
    )
    conn.commit()
    conn.close()


def upsert_user(user_id: int, step: str | None = None, subscribed: int | None = None, username: str | None = None):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    exists = cursor.fetchone()
    now = datetime.now().isoformat(timespec="seconds")

    if exists:
        if step is not None and username is not None:
            cursor.execute("UPDATE users SET step=?, username=?, last_action=? WHERE user_id=?",
                           (step, username, now, user_id))
        elif step is not None:
            cursor.execute("UPDATE users SET step=?, last_action=? WHERE user_id=?",
                           (step, now, user_id))
        if subscribed is not None:
            cursor.execute("UPDATE users SET subscribed=?, last_action=? WHERE user_id=?",
                           (subscribed, now, user_id))
        if username is not None and step is None:
            cursor.execute("UPDATE users SET username=?, last_action=? WHERE user_id=?",
                           (username, now, user_id))
    else:
        cursor.execute(
            "INSERT INTO users (user_id, source, step, subscribed, last_action, username) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "unknown", step or "старт", subscribed or 0, now, username),
        )

    conn.commit()
    conn.close()


def purge_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM events WHERE user_id=?", (user_id,))
    cursor.execute("DELETE FROM answers WHERE user_id=?", (user_id,))
    cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    cursor.execute("DELETE FROM scheduled_messages WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def is_fast_user(user_id: int) -> bool:
    if MODE == "test":
        return True
    return FAST_USER_ID is not None and user_id == FAST_USER_ID


async def smart_sleep(user_id: int, prod_seconds: int, test_seconds: int = 3):
    delay = test_seconds if is_fast_user(user_id) else prod_seconds
    await asyncio.sleep(delay)


# =========================================================
# UNIVERSAL SCHEDULER
# =========================================================

def schedule_message(
    user_id: int,
    prod_seconds: int,
    kind: str,
    payload: str | None = None,
    test_seconds: int = 3,
):
    delay = test_seconds if is_fast_user(user_id) else prod_seconds
    send_at = datetime.now() + timedelta(seconds=delay)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
        (user_id, kind),
    )

    cursor.execute(
        "INSERT INTO scheduled_messages (user_id, send_at, kind, payload) VALUES (?, ?, ?, ?)",
        (user_id, send_at.isoformat(timespec="seconds"), kind, payload),
    )

    conn.commit()
    conn.close()


# =========================================================
# НОВЫЕ ФУНКЦИИ ИСТЕЧЕНИЯ КНОПОК
# =========================================================

async def expire_start_test(user_id: int, payload: str | None):
    try:
        if payload:
            msg_id = int(payload)
            await bot.edit_message_reply_markup(chat_id=user_id, message_id=msg_id, reply_markup=None)
    except:
        pass

    log_event(user_id, "Кнопка Начать тест истекла")
    await send_case_story(user_id)


async def expire_after_test(user_id: int, payload: str | None):
    try:
        if payload:
            msg_id = int(payload)
            await bot.edit_message_reply_markup(chat_id=user_id, message_id=msg_id, reply_markup=None)
    except:
        pass

    log_event(user_id, "Кнопки Хорошо/Нет истекли")
    await send_case_story(user_id)


async def expired_get_material(user_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT subscribed FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    is_subscribed = row and row[0] == 1

    if is_subscribed:
        log_event(user_id, "Истёк таймер Получить гайд — подписан")
        await send_avoidance_intro(user_id)
    else:
        log_event(user_id, "Истёк таймер Получить гайд — НЕ подписан")
        await send_channel_invite(user_id)


# =========================================================
# SCHEDULER WORKER
# =========================================================

async def process_scheduled_message(task_id: int, user_id: int, kind: str, payload: str | None):

    log_event(user_id, "Запуск отложенного сообщения", kind)

    try:
        if kind == "channel_invite":
            await send_channel_invite(user_id)

        elif kind == "avoidance_intro":
            await send_avoidance_intro(user_id)

        elif kind == "expired_start_test":
            await expire_start_test(user_id, payload)

        elif kind == "expired_after_test":
            await expire_after_test(user_id, payload)

        elif kind == "get_material_expired":
            await expired_get_material(user_id)

        elif kind == "case_story":
            await send_case_story(user_id, payload)

        elif kind == "final_block1":
            await send_final_message(user_id)

        elif kind == "final_block2":
            await send_final_block2(user_id)

        elif kind == "final_block3":
            await send_final_block3(user_id)

        elif kind == "chat_invite":
            await send_chat_invite(user_id)

    finally:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("UPDATE scheduled_messages SET delivered=1 WHERE id=?", (task_id,))
        conn.commit()
        conn.close()


async def scheduler_worker():
    logger.info("Scheduler запущен")

    while True:
        try:
            now = datetime.now().isoformat(timespec="seconds")

            conn = sqlite3.connect(DB_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, kind, payload
                FROM scheduled_messages
                WHERE delivered=0 AND send_at <= ?
                ORDER BY send_at ASC
                LIMIT 50
            """, (now,))
            rows = cursor.fetchall()
            conn.close()

            for task_id, user_id, kind, payload in rows:
                await process_scheduled_message(task_id, user_id, kind, payload)

        except Exception as e:
            logger.exception(f"Scheduler error: {e}")

        await asyncio.sleep(SCHEDULER_POLL_INTERVAL)
# =========================================================
# 1. START
# =========================================================

init_db()

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = (message.from_user.username or "").strip() or None

    TEST_USER_ID = int(os.getenv("FAST_USER_ID", "0") or 0)
    if user_id == TEST_USER_ID:
        purge_user(user_id)
        log_event(user_id, "Очистка данных тестового пользователя")

    upsert_user(user_id, step="старт", username=username)
    log_event(user_id, "Запуск бота /start")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 Получить гайд", callback_data="get_material")]
        ]
    )

    msg = await message.answer(
        "Если Вы зашли в этот бот, значит, Ваши тревоги уже успели сильно вмешаться в жизнь.\n"
        "• Частое сердцебиение 💓\n"
        "• потемнение в глазах 🌘\n"
        "• головокружение🌀\n"
        "• пот по спине😰\n"
        "• страх потерять рассудок...\n"
        "Вы стараетесь взять себя в руки, но чем сильнее пытаетесь успокоиться — тем страшнее становится.\n"
        "Анализы в норме, а наплывы ужаса продолжают догонять.\n\n"
        "Чтобы разобраться в механизме паники и вернуть контроль — скачайте материал.",
        parse_mode="HTML",
        reply_markup=kb,
    )

    # Новый таймер — если НЕ нажал "Получить гайд"
    schedule_message(
        user_id=user_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=30,
        kind="get_material_expired",
        payload=None,
    )


# =========================================================
# 2. МАТЕРИАЛ
# =========================================================

@router.callback_query(F.data == "get_material")
async def send_material(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    username = callback.from_user.username or None
    await callback.answer()

    upsert_user(chat_id, step="получил_гайд", username=username)
    log_event(chat_id, "Получил гайд")

    # Приветственный кружок
    if VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_chat_action(chat_id, "upload_video_note")
            await bot.send_video_note(chat_id, VIDEO_NOTE_FILE_ID)
        except Exception as e:
            log_event(chat_id, "Ошибка отправки кружка", str(e))

    # PDF
    if LINK and os.path.exists(LINK):
        file = FSInputFile(LINK, filename="Выход из панического круга.pdf")
        await bot.send_document(chat_id, file, caption="Вот Ваш первый шаг 🧘🏻‍♀️")
        log_event(chat_id, "Отправлен PDF-файл")
    elif LINK and LINK.startswith("http"):
        await bot.send_message(chat_id, f"📘 Материал по ссылке: {LINK}")
        log_event(chat_id, "Отправлена ссылка вместо файла")
    else:
        await bot.send_message(chat_id, "⚠️ Файл не найден.")
        log_event(chat_id, "Файл PDF не найден")

    # Следующие этапы
    schedule_message(chat_id, prod_seconds=20 * 60, test_seconds=5, kind="channel_invite")
    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="avoidance_intro")


async def send_channel_invite(chat_id: int):
    upsert_user(chat_id, step="приглашение_в_канал")

    text = (
        "У меня есть телеграм-канал, где я делюсь рабочими техниками против тревоги.\n\n"
        "Несколько примеров:\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/16\">Как неправильное дыхание усиливает ПА</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/17\">Алкоголь и первый приступ</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/28\">Опасные цифры давления?</a>\n\n"
        "Подписывайтесь 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")]
        ]
    )

    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        log_event(chat_id, "Отправлено приглашение в канал")
    except Exception as e:
        log_event(chat_id, "Ошибка отправки приглашения в канал", str(e))


# =========================================================
# 3. ТЕСТ ИЗБЕГАНИЯ
# =========================================================

avoidance_questions = [
    "Вы часто измеряете давление или пульс? 💓",
    "Когда выходите из дома, берёте с собой воду? 💧",
    "Отказываетесь от спорта из-за опасений? 🧎🏻‍♀️",
    "Стараетесь не оставаться в одиночестве? 👥",
    "Часто открываете окно, чтобы «не было душно»? 💨",
    "В общественных местах садитесь у выхода? 🚪",
    "Отвлекаетесь в телефон, чтобы не чувствовать тело? 📲",
    "Избегаете поездок за город без связи? 📶"
]


async def send_avoidance_intro(chat_id: int):
    upsert_user(chat_id, step="предложен_тест_избегания")
    log_event(chat_id, "Предложен тест избегания")

    text = (
        "Давайте проверим, какие привычки помогают, а какие наоборот усиливают тревогу.\n\n"
        "Короткий тест — всего 8 вопросов 🗳"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать тест", callback_data="avoidance_start")]
        ]
    )

    msg = await bot.send_message(chat_id, text, reply_markup=kb)

    # Новый таймер — если НЕ нажал кнопку "Начать тест"
    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=30,
        kind="expired_start_test",
        payload=str(msg.message_id),
    )


@router.callback_query(F.data == "avoidance_start")
async def start_avoidance_test(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM answers WHERE user_id=?", (chat_id,))
    conn.commit()
    conn.close()

    upsert_user(chat_id, step="тест_избегания_начат")
    log_event(chat_id, "Начат тест избегания")

    await bot.send_message(chat_id, "Итак, начнём:")
    await send_question(chat_id, 0)


async def send_question(chat_id: int, index: int):
    if index >= len(avoidance_questions):
        await finish_test(chat_id)
        return

    q = avoidance_questions[index]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=f"ans_yes_{index}"),
                InlineKeyboardButton(text="Нет", callback_data=f"ans_no_{index}")
            ]
        ]
    )

    await bot.send_message(chat_id, f"{index + 1}. {q}", reply_markup=kb)
@router.callback_query(F.data.startswith("ans_"))
async def handle_answer(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    try:
        _, ans, idx_raw = callback.data.split("_")
        idx = int(idx_raw)

        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO answers (user_id, question, answer) VALUES (?, ?, ?)",
            (chat_id, idx, "yes" if ans == "yes" else "no"),
        )
        conn.commit()
        conn.close()

        log_event(chat_id, "Ответ на тест", f"Вопрос {idx + 1}: {ans}")

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass

        if idx + 1 < len(avoidance_questions):
            await send_question(chat_id, idx + 1)
        else:
            await finish_test(chat_id)

    except Exception as e:
        logger.error(f"Ошибка обработки ответа: {e}")
        await bot.send_message(chat_id, "Ошибка обработки ответа, попробуйте ещё раз.")
        log_event(chat_id, "Ошибка обработки ответа", str(e))


# =========================================================
# 3.1 — ФИНИШ ТЕСТА
# =========================================================

async def finish_test(chat_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT answer FROM answers WHERE user_id=?", (chat_id,))
    answers = [row[0] for row in cursor.fetchall()]
    conn.close()

    yes_count = answers.count("yes")
    upsert_user(chat_id, step="тест_избегания_завершен")
    log_event(chat_id, "Тест избегания завершён", f"ДА: {yes_count}")

    chain = (
        "Чем больше ограничений мы накладываем,\n"
        "⬇️\nтем сильнее становится тревога,\n"
        "⬇️\nтем больше ощущений мы замечаем,\n"
        "⬇️\nтем сильнее пугаемся.\n\n"
        "Получается замкнутый круг 🔄"
    )

    await bot.send_message(chat_id, "Тест завершён. Обрабатываем результаты ⏳")
    await smart_sleep(chat_id, prod_seconds=3, test_seconds=1)

    final_msg = None

    if yes_count >= 4:
        part1 = (
            "Похоже, что избегание серьёзно вмешивается в Вашу жизнь 🪤\n\n" + chain
        )
        part2 = (
            "Хорошая новость — круг можно разорвать.\n"
            "Выберите один пункт, где ответили «Да», и сделайте наоборот.\n\n"
            "Только один шаг на пару недель.\n\n"
            "Попробуете?"
        )
        await bot.send_message(chat_id, part1)
        await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)
        msg = await bot.send_message(chat_id, part2, reply_markup=_cta_keyboard())
        final_msg = msg.message_id

    elif 2 <= yes_count <= 3:
        part1 = (
            "Некоторые элементы избегания всё же есть 🪤\n\n" + chain
        )
        part2 = (
            "Выберите один пункт «Да» — и попробуйте делать противоположное.\n"
            "Всего один шаг.\n\n"
            "Попробуете?"
        )
        await bot.send_message(chat_id, part1)
        await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)
        msg = await bot.send_message(chat_id, part2, reply_markup=_cta_keyboard())
        final_msg = msg.message_id

    elif yes_count == 1:
        text = (
            "У Вас практически нет избеганий — отлично.\n\n"
            "Но даже одно избегание стоит проработать.\n\n"
            "Попробуете?"
        )
        msg = await bot.send_message(chat_id, text, reply_markup=_cta_keyboard())
        final_msg = msg.message_id

    else:
        text = (
            "Избеганий нет. Это замечательно!\n\n"
            "Если какие-то есть вне теста — начните работать с ними.\n\n"
            "Попробуете?"
        )
        msg = await bot.send_message(chat_id, text, reply_markup=_cta_keyboard())
        final_msg = msg.message_id

    # Таймер истечения кнопок "Хорошо/Нет"
    if final_msg:
        schedule_message(
            user_id=chat_id,
            prod_seconds=24 * 60 * 60,
            test_seconds=30,
            kind="expired_after_test",
            payload=str(final_msg),
        )


# =========================================================
# 4. КНОПКИ "Хорошо / Нет"
# =========================================================

def _cta_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Хорошо 😌", callback_data="avoidance_ok"),
                InlineKeyboardButton(text="Нет, пока боюсь 🙈", callback_data="avoidance_scared"),
            ]
        ]
    )


@router.callback_query(F.data == "avoidance_ok")
async def handle_avoidance_ok(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    log_event(chat_id, "Нажал Хорошо 😌")
    await bot.send_message(chat_id, "Супер! У Вас всё получится 💪")

    schedule_message(
        chat_id,
        prod_seconds=60 * 60,
        test_seconds=5,
        kind="case_story",
        payload=str(callback.message.message_id),
    )


@router.callback_query(F.data == "avoidance_scared")
async def handle_avoidance_scared(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    log_event(chat_id, "Нажал Нет, боюсь 🙈")
    await bot.send_message(chat_id, "Это нормально. Иногда нужно чуть больше времени 🫶🏻")

    schedule_message(
        chat_id,
        prod_seconds=60 * 60,
        test_seconds=5,
        kind="case_story",
        payload=str(callback.message.message_id),
    )


# =========================================================
# 5. ИСТОРИЯ ПАЦИЕНТКИ
# =========================================================

async def send_case_story(chat_id: int, payload: str | None = None):
    upsert_user(chat_id, step="история_пациентки")

    if payload:
        try:
            await bot.edit_message_reply_markup(chat_id, int(payload), reply_markup=None)
        except:
            pass

    text = (
        "<b>Чтобы ослабить власть тревоги, нужно делать то, что страшно.</b>\n\n"
        "Помните девушку из гайда? У неё приступ случился после разговора с начальником.\n"
        "Мы постепенно возвращали её в метро: платформа → одна станция → две.\n\n"
        "Тело кричало «опасность», но мы заранее готовились к этим ощущениям.\n"
        "Через несколько недель она снова спокойно ездила по маршруту.\n\n"
        "Параллельно мы разбирали убеждение «я должна быть идеальной».\n"
        "Когда она начала говорить о своих потребностях и позволять себе «не быть идеальной», напряжение ушло.\n\n"
        "Сейчас она свободно перемещается по городу и живёт без ожидания приступов ⛱"
    )

    await bot.send_message(chat_id, text, parse_mode="HTML")
    log_event(chat_id, "Отправлена история пациентки")

    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="final_block1")


# =========================================================
# 6. ФИНАЛЬНАЯ ВОРОНКА
# =========================================================

async def send_final_message(chat_id: int):
    upsert_user(chat_id, step="приглашение_на_консультацию")
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    photo = FSInputFile("media/DSC03503.jpg")
    caption = (
        "С людьми с паническими атаками я работаю ежедневно.\n"
        "Мы разбираем индивидуальный цикл тревоги и составляем план действий.\n\n"
        "<b>Как я могу помочь?</b>\n"
        "Меняем реакции, мысли и привычки, которые поддерживают страх."
    )

    try:
        await bot.send_photo(chat_id, photo, caption=caption, parse_mode="HTML")
    except Exception as e:
        log_event(chat_id, "Ошибка отправки фото консультации", str(e))

    await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)

    text2 = (
        "По итогам терапии Вы получите:\n\n"
        "✨ меньше проверок состояния\n"
        "✨ свободу передвижения\n"
        "✨ контакт с тревогой без избеганий\n"
        "✨ уверенность, что с Вами всё в порядке\n\n"
        "Подробнее о консультациях 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Узнать про консультации", callback_data="consult_show")]]
    )

    await bot.send_message(chat_id, text2, parse_mode="HTML", reply_markup=kb)

    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="final_block2")


@router.callback_query(F.data == "consult_show")
async def consult_show(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    upsert_user(chat_id, step="перешел_к_описанию_консультаций")
    log_event(chat_id, "Интересовался консультацией")

    await bot.send_message(
        chat_id,
        "Подробнее о консультациях: https://лечение-паники.рф/консультации",
        disable_web_page_preview=True
    )


async def send_final_block2(chat_id: int):
    upsert_user(chat_id, step="сомнение_в_терапии")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    extra = (
        "<b>Частый вопрос:</b> «А вдруг терапия не поможет?»\n\n"
        "Психотерапия — это не разговоры, а обучение тому, как правильно реагировать на страх.\n"
        "Большинство клиентов чувствует облегчение уже через несколько недель."
    )

    await bot.send_message(chat_id, extra, parse_mode="HTML")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    try:
        await bot.send_photo(chat_id, FSInputFile("media/Scrc2798760b2b95377.jpg"))
        await bot.send_photo(chat_id, FSInputFile("media/Scb2b95377.jpg"))
    except Exception as e:
        log_event(chat_id, "Ошибка отправки отзывов", str(e))

    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="final_block3")


async def send_final_block3(chat_id: int):
    upsert_user(chat_id, step="ошибки_пациента_с_паникой")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    text = (
        "<b>Почему паника не уходит?</b>\n\n"
        "Потому что Вы боретесь с ощущениями, вместо того чтобы отвечать на конкретную мысль."
    )

    await bot.send_message(chat_id, text, parse_mode="HTML")

    schedule_message(
        chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=5,
        kind="chat_invite",
    )


async def send_chat_invite(chat_id: int):
    upsert_user(chat_id, step="приглашение_в_чат")

    text = (
        "Хотите задать вопросы про симптомы или лечение?\n\n"
        "Присоединяйтесь к чату 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вступить в чат 🩷", url="https://t.me/Ocd_and_Anxiety_Chat")]
        ]
    )

    await bot.send_message(chat_id, text, reply_markup=kb)


# =========================================================
# RUN
# =========================================================

async def main():
    logger.info(f"MODE={MODE}, FAST_USER_ID={FAST_USER_ID}")
    await asyncio.gather(
        dp.start_polling(bot),
        scheduler_worker(),
    )


if __name__ == "__main__":
    asyncio.run(main())
