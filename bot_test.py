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
            username TEXT,
            consult_interested INTEGER DEFAULT 0
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

    # Миграция для уже существующей таблицы users: добавляем consult_interested, если колонки ещё нет
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN consult_interested INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        # Колонка уже существует – тихо продолжаем
        pass

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
            cursor.execute(
                "UPDATE users SET step=?, username=?, last_action=? WHERE user_id=?",
                (step, username, now, user_id),
            )
        elif step is not None:
            cursor.execute(
                "UPDATE users SET step=?, last_action=? WHERE user_id=?",
                (step, now, user_id),
            )
        if subscribed is not None:
            cursor.execute(
                "UPDATE users SET subscribed=?, last_action=? WHERE user_id=?",
                (subscribed, now, user_id),
            )
        if username is not None and step is None:
            cursor.execute(
                "UPDATE users SET username=?, last_action=? WHERE user_id=?",
                (username, now, user_id),
            )
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

    log_event(
        user_id,
        "Запланировано отложенное сообщение",
        f"Тип: {kind}, отправка: {send_at.isoformat(timespec='seconds')}"
    )


def mark_message_delivered(task_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_messages SET delivered=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


# =========================================================
# НОВЫЕ ФУНКЦИИ — для истечения кнопок
# =========================================================

async def expire_start_test(user_id: int, payload: str | None):
    try:
        msg_id = int(payload)
        await bot.edit_message_reply_markup(chat_id=user_id, message_id=msg_id, reply_markup=None)
    except Exception:
        pass

    log_event(user_id, "Кнопка «Начать тест» истекла", None)

    await send_case_story(user_id)


async def expire_after_test(user_id: int, payload: str | None):
    try:
        msg_id = int(payload)
        await bot.edit_message_reply_markup(chat_id=user_id, message_id=msg_id, reply_markup=None)
    except Exception:
        pass

    log_event(user_id, "Кнопки Хорошо/Нет истекли", None)

    await send_case_story(user_id)


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

        else:
            log_event(user_id, "Неизвестный тип scheduled", kind)

    finally:
        mark_message_delivered(task_id)


async def scheduler_worker():
    logger.info("Scheduler запущен")

    while True:
        try:
            now = datetime.now().isoformat(timespec="seconds")

            conn = sqlite3.connect(DB_PATH, timeout=10)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_id, kind, payload
                FROM scheduled_messages
                WHERE delivered=0 AND send_at <= ?
                ORDER BY send_at ASC
                LIMIT 50
                """,
                (now,),
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                await asyncio.sleep(SCHEDULER_POLL_INTERVAL)
                continue

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
        log_event(user_id, "Очистка данных тестового пользователя", "Данные пользователя очищены (тест)")

    upsert_user(user_id, step="старт", username=username)
    log_event(user_id, "Запуск бота", "/start")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 Получить гайд", callback_data="get_material")]
        ]
    )

    await message.answer(
        "Если Вы зашли в этот бот, значит, Ваши тревоги уже успели сильно вмешаться в жизнь.\n"
        "• Частое сердцебиение 💓 \n"
        "• потемнение в глазах 🌘 \n"
        "• головокружение🌀 \n"
        "• пот по спине😰 \n"
        "• страх потерять рассудок...\n"
        "Вы стараетесь взять себя в руки, но чем сильнее пытаетесь успокоиться — тем страшнее становится. \n"
        "Анализы крови, обследования сердца и сосудов показывают, что всё в норме. Но наплывы ужаса продолжают догонять Вас.\n\n"
        "Знакомо? \n\n"
        "Вероятно, Вы уже знаете, что такие наплывы страха называются <b>паническими атаками</b>.\n"
        "Многие люди месяцами ищут причину этих приступов — и всё равно не могут понять, почему паника возвращается.\n"
        "Я покажу, как ослабить её власть и перестать ждать нового приступа каждый день.\n\n"
        "Эти состояния имеют чёткую внутреннюю закономерность — и когда Вы поймёте её, Вы сможете взять происходящее под контроль 🛥\n\n"
        "Я приготовил материал, который поможет Вам разобраться, что запускает панические атаки, чем они поддерживаются и как наконец вернуться к расслабленной жизни.\n"
        "Скачайте его — и дайте отпор страху!",
        parse_mode="HTML",
        reply_markup=kb,
    )


# =========================================================
# 2. МАТЕРИАЛ
# =========================================================

@router.callback_query(F.data == "get_material")
async def send_material(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    username = callback.from_user.username or None

    upsert_user(chat_id, step="получил_гайд", username=username)
    log_event(chat_id, "Получение гайда", "Нажата кнопка 'Получить гайд'")

    if VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_chat_action(chat_id, "upload_video_note")
            await bot.send_video_note(chat_id, VIDEO_NOTE_FILE_ID)
        except Exception as e:
            logger.warning(f"Ошибка отправки кружка: {e}")
            log_event(chat_id, "Ошибка отправки приветственного кружка", str(e))

    if LINK and os.path.exists(LINK):
        file = FSInputFile(LINK, filename="Выход из панического круга.pdf")
        await bot.send_document(chat_id, file, caption="Вот Ваш первый шаг к спокойствию 🧘🏻‍♀️")
        log_event(chat_id, "Отправлен PDF", "Гайд отправлен")
    elif LINK and LINK.startswith("http"):
        await bot.send_message(chat_id, f"📘 Ваш материал доступен по ссылке: {LINK}")
        log_event(chat_id, "Отправлена ссылка на материал", LINK)
    else:
        await bot.send_message(chat_id, "⚠️ Файл не найден.")
        log_event(chat_id, "Файл гайда не найден", LINK or "нет пути")

    schedule_message(chat_id, prod_seconds=20 * 60, test_seconds=5, kind="channel_invite")
    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="avoidance_intro")

    await callback.answer()


async def send_channel_invite(chat_id: int):
    upsert_user(chat_id, step="приглашение_в_канал")

    text = (
        "У меня есть телеграм-канал, где я делюсь нюансами об эффективных способах преодоления тревоги "
        "и развеиваю мифы о <i>не</i>работающих методах 💧🙅🏻‍♂️\n\n"
        "Несколько примеров:\n\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/16\">Как неправильное дыхание усиливает паническую атаку</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/17\">Алкоголь и первый приступ ПА</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/28\">Опасные цифры давления?</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/34\">Волшебный газ для успокоения?</a>\n\n"
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
# 3. ОПРОС ИЗБЕГАНИЯ
# =========================================================

avoidance_questions = [
    "Вы часто измеряете давление или пульс? 💓",
    "Когда выходите из дома, берёте с собой бутылку воды? 💧",
    "Вам пришлось отказаться от спорта или физических нагрузок из-за опасений? 🧎🏻‍♀️‍➡️",
    "Стараетесь не оставаться в одиночестве? 👥",
    "Стали часто открывать окно, чтобы не было душно? 💨",
    "В общественных местах предпочитаете садиться поближе к выходу? 🚪",
    "Отвлекаетесь в телефон, чтобы не замечать неприятные ощущения? 📲",
    "Избегаете поездок за город, чтобы не оставаться без мобильной связи? 📶"
]


async def send_avoidance_intro(chat_id: int):
    upsert_user(chat_id, step="предложен_тест_избегания")
    text = (
        "Вам может казаться, что панические атаки продолжают возникать, несмотря на то, что вы стараетесь их не провоцировать.\n\n"
        "Давайте проверим, насколько ваши привычки помогают, а где — мешают.\n\n"
        "Пройдите короткий тест — всего 8 вопросов 🗳"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать тест", callback_data="avoidance_start")]
        ]
    )

    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    log_event(chat_id, "Показан блок теста избегания")

    # ⬇️ НОВЫЙ ТАЙМЕР: отдельный kind, не case_story
    schedule_message(
        chat_id,
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

        log_event(chat_id, "Ответ на тест", f"Вопрос {idx + 1}, ответ: {ans}")

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        if idx + 1 < len(avoidance_questions):
            await send_question(chat_id, idx + 1)
        else:
            await finish_test(chat_id)

    except Exception as e:
        logger.error(f"Ошибка обработки ответа: {e}")
        await bot.send_message(chat_id, "Ошибка обработки ответа. Попробуйте ещё раз.")
        log_event(chat_id, "Ошибка обработки ответа теста", str(e))


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
    log_event(chat_id, "Тест избегания завершен", f"ДА: {yes_count}")

    chain = (
        "Чем больше вынужденных ограничений мы накладываем на свою жизнь\n"
        "⬇️\nтем большую важность мы придаём панике\n"
        "⬇️\nТем больше концентрируемся на своём теле\n"
        "⬇️\nТем больше чувствуем в нём неожиданные/неприятные ощущения\n"
        "⬇️\nТем больше переживаем из-за них.\n\nИ так до бесконечности 🔄"
    )

    await bot.send_message(chat_id, "Тест завершён. Обрабатываем результаты ⏳")
    await smart_sleep(chat_id, prod_seconds=3, test_seconds=1)

    final_msg_id = None

    if yes_count >= 4:
        part1 = (
            "Судя по Вашим ответам, Вам приходится сильно подстраивать свою жизнь под "
            "<b><i>избегание</i></b> повторных приступов паники 🪤\n\n" + chain
        )
        part2 = (
            "☀️ Хорошая новость в том, что мы можем менять стратегию действий — и разрывать этот круг.\n\n"
            "Я предлагаю выбрать один пункт, где Вы ответили «Да», и начать делать противоположное.\n\n"
            "🔹 Всегда носите с собой воду? 👉🏼 Оставьте дома.\n"
            "🔹 Всегда открыто окно? 👉🏼 Побудьте немного в духоте.\n\n"
            "Но только одно изменение на пару недель.\n\n"
            "Попробуете?"
        )
        await bot.send_message(chat_id, part1, parse_mode="HTML")
        await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)
        msg = await bot.send_message(chat_id, part2, parse_mode="HTML", reply_markup=_cta_keyboard())
        final_msg_id = msg.message_id

    elif 2 <= yes_count <= 3:
        part1 = (
            "Судя по Вашим ответам, некоторые элементы избегания всё же присутствуют 🪤\n\n" + chain
        )
        part2 = (
            "Давайте попробуем зайти за границу привычных ограничений.\n"
            "Выберите один пункт «Да» — и начните делать наоборот. Только один пункт на пару недель.\n\n"
            "Попробуете?"
        )
        await bot.send_message(chat_id, part1, parse_mode="HTML")
        await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)
        msg = await bot.send_message(chat_id, part2, parse_mode="HTML", reply_markup=_cta_keyboard())
        final_msg_id = msg.message_id

    elif yes_count == 1:
        text = (
            "У Вас практически нет избеганий — это отлично!\n\n"
            "Но даже одно избегание стоит проработать.\n\n"
            "Выберите тот единственный пункт, где ответили «Да», и попробуйте делать наоборот.\n\n"
            "Попробуете?"
        )
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_cta_keyboard())
        final_msg_id = msg.message_id

    else:
        text = (
            "У Вас нет избеганий — это замечательно!\n\n"
            "Если какие-то избегания есть, но не попали в тест — работайте над ними.\n\n"
            "Попробуете?"
        )
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_cta_keyboard())
        final_msg_id = msg.message_id

    if final_msg_id is not None:
        schedule_message(
            user_id=chat_id,
            prod_seconds=24 * 60 * 60,
            test_seconds=30,
            kind="expired_after_test",
            payload=str(final_msg_id),
        )
# =========================================================
# 4. КНОПКИ "ХОРОШО / НЕТ"
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
    except Exception:
        pass

    log_event(chat_id, "Выбрал Хорошо 😌")
    await bot.send_message(chat_id, "Супер! У Вас всё получится! 💪🏼")

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
    except Exception:
        pass

    log_event(chat_id, "Выбрал боюсь")
    await bot.send_message(chat_id, "Это нормально. Иногда нужно чуть больше времени 🫶🏼")

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
        except Exception:
            pass

    text = (
        "<b>Чтобы ослабить власть тревоги, нужно делать то, что страшно.</b>\n\n"
        "Помните историю из гайда про девушку, у которой приступ случился после разговора с начальником?\n"
        "Полгода она жила в страхе, пока не пришла на терапию.\n\n"
        "<b>Экспозиция.</b>\n\n"
        "Метро стало для неё угрозой. Мы шаг за шагом возвращались туда: сначала просто на платформу, потом — одна станция, две.\n"
        "На каждом этапе тело кричало «опасность», но мы заранее были готовы.\n\n"
        "Через несколько недель она снова спокойно ездила по маршруту.\n\n"
        "<b>Изменение убеждений.</b>\n\n"
        "В основе её паники лежали не только телесные ощущения, но установка — «быть идеальной».\n"
        "Когда она начала делегировать, позволять себе «4» вместо «5» — напряжение ушло.\n\n"
        "Сейчас она свободно перемещается по городу и не ждёт нового приступа ⛱"
    )

    await bot.send_message(chat_id, text, parse_mode="HTML")
    log_event(chat_id, "Отправлена история пациентки")

    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="final_block1")


# =========================================================
# 6. ПРИГЛАШЕНИЕ НА КОНСУЛЬТАЦИЮ
# =========================================================

async def send_final_message(chat_id: int):
    upsert_user(chat_id, step="приглашение_на_консультацию")
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    photo = FSInputFile("media/DSC03503.jpg")

    caption = (
        "С людьми, переживающими панические атаки, я работаю каждый день.\n"
        "Мы разбираем индивидуальный цикл тревоги и составляем план действий.\n\n"
        "<b>Как я могу помочь?</b>\n\n"
        "Мы определим Ваши мысли, реакции и привычки, которые поддерживают страх, "
        "и шаг за шагом будем заменять их на здоровые паттерны."
    )

    try:
        await bot.send_photo(chat_id, photo, caption=caption, parse_mode="HTML")
        log_event(chat_id, "Фото консультации отправлено")
    except Exception as e:
        log_event(chat_id, "Ошибка отправки фото консультации", str(e))

    await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)

    text2 = (
        "По итогам терапии вы получите:\n\n"
        "✨ снижение гиперконтроля\n"
        "✨ способность свободно передвигаться\n"
        "✨ умение оставаться в контакте с тревогой\n"
        "✨ жизнь без избеганий\n"
        "✨ уверенность, что с Вами всё в порядке\n\n"
        "Подробнее о консультациях 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Узнать про консультации", callback_data="consult_show")]
        ]
    )

    try:
        await bot.send_message(chat_id, text2, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log_event(chat_id, "Ошибка отправки приглашения на консультацию", str(e))

    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=5, kind="final_block2")


@router.callback_query(F.data == "consult_show")
async def consult_show(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    # помечаем интерес пользователя
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET consult_interested = 1 WHERE user_id=?", (chat_id,))
    conn.commit()
    conn.close()

    upsert_user(chat_id, step="перешел_к_описанию_консультаций")
    log_event(chat_id, "Открыт раздел консультаций")

    text = "Подробнее о консультациях: https://лечение-паники.рф/консультации"

    await bot.send_message(chat_id, text, disable_web_page_preview=True)


# =========================================================
# 7. ФИНАЛЬНЫЕ БЛОКИ
# =========================================================

async def send_final_block2(chat_id: int):
    upsert_user(chat_id, step="сомнение_в_психотерапии")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    extra = (
        "<b>Частый вопрос:</b> «А вдруг терапия не поможет?»\n\n"
        "Психотерапия — это не разговоры, а точечная работа по изменению реакции на страх.\n\n"
        "Клиенты чувствуют облегчение уже через несколько недель."
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
    upsert_user(chat_id, step="ошибки_пациента_с_паническими_атаками")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    text = (
        "<b>Почему паника не уходит?</b>\n\n"
        "Потому что Вы не отвечаете на конкретную мысль, вызывающую страх.\n\n"
        "На сеансах мы ищем ядро страха — и возвращаем контроль Вам."
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
        "Хотите задать вопросы про симптомы, лечение или диагностику?\n\n"
        "Присоединяйтесь к моему чату 👇"
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
