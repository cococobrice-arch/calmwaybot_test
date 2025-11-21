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


def upsert_user(
    user_id: int,
    step: str | None = None,
    subscribed: int | None = None,
    username: str | None = None
):
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
            "INSERT INTO users (user_id, source, step, subscribed, last_action, username) "
            "VALUES (?, ?, ?, ?, ?, ?)",
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
        "DELETE FROM scheduled_messages "
        "WHERE user_id=? AND kind=? AND delivered=0",
        (user_id, kind),
    )

    cursor.execute(
        "INSERT INTO scheduled_messages (user_id, send_at, kind, payload) "
        "VALUES (?, ?, ?, ?)",
        (user_id, send_at.isoformat(timespec="seconds"), kind, payload),
    )

    conn.commit()
    conn.close()


def mark_message_delivered(task_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_messages SET delivered=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


async def process_scheduled_message(
    task_id: int,
    user_id: int,
    kind: str,
    payload: str | None
):
    try:
        if kind == "channel_invite":
            await send_channel_invite(user_id)
        elif kind == "avoidance_intro":
            await send_avoidance_intro(user_id)
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
            log_event(
                user_id,
                "Неизвестный тип отложенного сообщения",
                kind
            )
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


init_db()


# =========================================================
# 1. START
# =========================================================

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = (message.from_user.username or "").strip() or None

    TEST_USER_ID = int(os.getenv("FAST_USER_ID", "0") or 0)

    if user_id == TEST_USER_ID:
        purge_user(user_id)
        log_event(
            user_id,
            "Очистка данных тестового пользователя",
            "Данные тестового пользователя очищены при старте",
        )

    upsert_user(user_id, step="старт", username=username)
    log_event(user_id, "Запуск бота", "Команда /start")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📘 Получить гайд",
                    callback_data="get_material",
                )
            ]
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
    log_event(
        chat_id,
        "Нажата кнопка «Получить гайд»",
        "Начало выдачи материала",
    )

    if VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_chat_action(chat_id, "upload_video_note")
            await bot.send_video_note(chat_id, VIDEO_NOTE_FILE_ID)
        except Exception as e:
            logger.warning(f"Ошибка отправки кружка: {e}")
            log_event(
                chat_id,
                "Ошибка отправки приветственного видео",
                str(e),
            )

    if LINK and os.path.exists(LINK):
        file = FSInputFile(LINK, filename="Выход из панического круга.pdf")
        await bot.send_document(
            chat_id,
            document=file,
            caption="Вот Ваш первый шаг к спокойствию 🧘🏻‍♀️",
        )
        log_event(
            chat_id,
            "Отправлен файл с гайдом",
            "Гайд отправлен как документ",
        )
    elif LINK and LINK.startswith("http"):
        await bot.send_message(
            chat_id,
            f"📘 Ваш материал доступен по ссылке: {LINK}",
        )
        log_event(
            chat_id,
            "Отправлена ссылка на гайд",
            LINK,
        )
    else:
        await bot.send_message(chat_id, "⚠️ Файл не найден.")
        log_event(
            chat_id,
            "Не удалось найти файл гайда",
            LINK or "Путь не задан",
        )

    # Тестовая задержка увеличена до 10 секунд
    schedule_message(
        chat_id,
        prod_seconds=20 * 60,
        test_seconds=10,
        kind="channel_invite",
    )
    schedule_message(
        chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=10,
        kind="avoidance_intro",
    )

    await callback.answer()


async def send_channel_invite(chat_id: int):
    upsert_user(chat_id, step="приглашение_в_канал")

    text = (
        "У меня есть телеграм-канал, где я делюсь нюансами об эффективных способах преодоления тревоги "
        "и развеиваю мифы о <i>не</i>работающих методах. "
        "Никакой воды — только проверенные решения 💧🙅🏻‍♂️\n\n"
        "Например, я писал там посты:\n\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/16\">Как неправильное дыхание усиливает паническую атаку</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/17\">Алкоголь и первый приступ ПА</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/28\">Каковы опасные цифры давления?</a>\n"
        "🔸 <a href=\"https://t.me/OcdAndAnxiety/34\">Волшебный газ для успокоения?</a>\n\n"
        "Подписывайтесь и получайте практические рекомендации 👇🏽"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подписаться",
                    url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}",
                )
            ]
        ]
    )

    try:
        await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        log_event(
            chat_id,
            "Отправлено приглашение в канал",
            None,
        )
    except Exception as e:
        log_event(
            chat_id,
            "Ошибка отправки приглашения в канал",
            str(e),
        )


# =========================================================
# 3. ОПРОС ИЗБЕГАНИЯ
# =========================================================

avoidance_questions = [
    "Вы часто измеряете давление или пульс? 💓",
    "Когда выходите из дома, берёте с собой бутылку воды? 💧",
    "Вам пришлось отказаться от спорта или физических нагрузок из-за опасений? 🧎🏻‍♀️‍➡️",
    "Стараетесь не оставаться в одиночестве? 👥",
    "Стали частро открывать окно, чтобы не было душно? 💨",
    "В общественных местах предпочитаете садиться поближе к выходу? 🚪",
    "Отвлекаетесь в телефон, чтобы не замечать неприятные телесные ощущения? 📲",
    "Избегаете поездок за город, чтобы не оставаться без мобильной связи и интернета? 📶",
]


async def send_avoidance_intro(chat_id: int):
    upsert_user(chat_id, step="предложен_тест_избегания")
    text = (
        "Вам может казаться, что панические атаки продолжают возникать, несмотя на то что Вы стараетесь их не провоцировать.\n"
        "Давайте проверим, насколько ваши привычки действительно помогают, а где — мешают?\n\n"
        "Пройдите короткий тест — всего 8 вопросов с ответами Да/Нет 🗳"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Начать тест",
                    callback_data="avoidance_start",
                )
            ]
        ]
    )
    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    log_event(
        chat_id,
        "Показан блок с предложением теста",
        "Предложен опрос избегания",
    )

    # Если пользователь не нажал кнопку "Начать тест",
    # через сутки (или 10 секунд для тестового пользователя)
    # кнопка будет убрана, и сразу придёт история пациентки.
    schedule_message(
        chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=10,
        kind="case_story",
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
    log_event(
        chat_id,
        "Начат тест избегания",
        "Нажата кнопка «Начать тест»",
    )

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

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
                InlineKeyboardButton(
                    text="Да",
                    callback_data=f"ans_yes_{index}",
                ),
                InlineKeyboardButton(
                    text="Нет",
                    callback_data=f"ans_no_{index}",
                ),
            ]
        ]
    )

    await bot.send_message(
        chat_id,
        f"{index + 1}. {q}",
        reply_markup=kb,
    )


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
            "INSERT OR REPLACE INTO answers (user_id, question, answer) "
            "VALUES (?, ?, ?)",
            (chat_id, idx, "yes" if ans == "yes" else "no"),
        )
        conn.commit()
        conn.close()

        log_event(
            chat_id,
            "Ответ на вопрос теста избегания",
            f"Вопрос {idx + 1}, ответ: {'Да' if ans == 'yes' else 'Нет'}",
        )

        if idx + 1 < len(avoidance_questions):
            await send_question(chat_id, idx + 1)
        else:
            await finish_test(chat_id)

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Ошибка ответа: {e}")
        try:
            await bot.send_message(
                chat_id,
                "Ошибка обработки ответа. Попробуйте ещё раз.",
            )
        except Exception:
            pass
        log_event(
            chat_id,
            "Ошибка обработки ответа теста избегания",
            str(e),
        )


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
    log_event(
        chat_id,
        "Тест избегания завершен",
        f"Количество ответов «Да»: {yes_count}",
    )

    chain = (
        "Чем больше вынужденных ограничений мы накладываем на свою жизнь\n"
        "️⬇️\nтем большую важность мы придаём панике\n"
        "⬇️\nТем больше концентрируемся на своём теле\n"
        "⬇️\nТем больше чувствуем в нём неожиданные/неприятные ощущения\n"
        "⬇️\nТем больше переживаем по поводу них.\n\nИ так до бесконечности 🔄"
    )

    await bot.send_message(
        chat_id,
        "Тест завершён. Подождите секунду, обрабатываем результаты ⏳",
    )
    await smart_sleep(chat_id, prod_seconds=3, test_seconds=1)

    final_msg_id: int | None = None

    if yes_count >= 4:
        part1 = (
            "Судя по Вашим ответам, Вам приходится довольно сильно подстраивать свою жизнь под "
            "<b><i>избегание</i></b> возможных повторных приступов паники. Это ловушка, в которую попадаются очень многие люди 🪤\n\n"
            + chain
        )
        part2 = (
            "☀️ Хорошая новость в том, что мы в силах менять стратегию своих действий — и тем самым разрывать этот порочный круг.\n"
            "Если тревога долгое время диктовала правила, естественно, что шаги навстречу страху будут ощущаться как последнее, чем захочется заниматься. "
            "Кажется, будто без этих «страхующих» привычек станет невыносимо дискомфортно. "
            "Но каждый раз, когда мы не убегаем, а остаёмся в пугающей ситуации, мозг получает новый опыт — что <i>опасность была преувеличена</i>.\n\n"
            "Вы уже почитали в моём гайде о том, как правильно отвечать себе на пугающие <u>мысли</u>. "
            "Поэтому теперь, держа под рукой эту памятку, Вы можете и в своих <u>действиях</u>"
            "попробовать немного зайти за грань того, в чём ограничивает Вас тревога 🪂\n\n"
            "Я предлагаю следующее.\n\nВозьмите один из пунктов, на который Вы ответили «Да», и начните делать его наоборот.\n\n"
            "🔹 Привыкли всегда носить с собой бутылку воды? 👉🏼 Оставьте её дома!\n"
            "🔹 Держите окно приоткрытым? 👉🏼 Побудьте подольше в небольшом дефиците кислорода.\n"
            "И т.п.\n\n"
            "Но не всё сразу! Возьмите сначала только одно правило и поработайте над отказом от него пару недель.\n\n"
            "Это будет дискомфортно, но я обещаю: это даст Вам больше уверенности в своей способности справляться со страхом 🦁\n\n"
            "Попробуете?"
        )
        await bot.send_message(chat_id, part1, parse_mode="HTML")
        await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)
        msg = await bot.send_message(
            chat_id,
            part2,
            parse_mode="HTML",
            reply_markup=_cta_keyboard(),
        )
        final_msg_id = msg.message_id

    elif 2 <= yes_count <= 3:
        part1 = (
            "Судя по Вашим ответам, Вам в некоторой степени приходится подстраивать свою жизнь под "
            "<b><i>избегание</i></b> возможных повторных приступов паники. Это ловушка, в которую попадаются очень многие люди 🪤\n\n"
            + chain
        )
        part2 = (
            "☀️ Хорошая новость в том, что мы в силах менять стратегию своих действий — и тем самым разрывать этот порочный круг.\n"
            "Если тревога долгое время диктовала правила, естественно, что шаги навстречу страху будут ощущаться как последнее, чем захочется заниматься. "
            "Кажется, будто без этих «страхующих» привычек станет невыносимо дискомфортно. "
            "Но каждый раз, когда мы не убегаем, а остаёмся в пугающей ситуации, мозг получает новый опыт — что <i>опасность была преувеличена</i>.\n\n"
            "Вы уже почитали в моём гайде о том, как правильно отвечать себе на пугающие <u>мысли</u>. "
            "Поэтому теперь, держа под рукой эту памятку, Вы можете и в своих <u>действиях</u>"
            "попробовать немного зайти за грань того, в чём ограничивает Вас тревога 🪂\n\n"
            "Я предлагаю следующее.\n\nВозьмите один из пунктов, на который Вы ответили «Да», и начните делать его наоборот.\n\n"
            "🔹 Привыкли всегда носить с собой бутылку воды? 👉🏼 Оставьте её дома!\n"
            "🔹 Держите окно приоткрытым? 👉🏼 Постарайтесь подольше побыть в небольшом дефиците кислорода.\n"
            "И т.п.\n\n"
            "Но не всё сразу! Возьмите для изменения сначала только одно правило и поработайте пару недель над отказом от него.\n\n"
            "Это будет дискомфортно, но я обещаю: это даст Вам больше уверенности в Вашей способности справляться со страхом 🦁\n\n"
            "Попробуете?"
        )
        await bot.send_message(chat_id, part1, parse_mode="HTML")
        await smart_sleep(chat_id, prod_seconds=60, test_seconds=3)
        msg = await bot.send_message(
            chat_id,
            part2,
            parse_mode="HTML",
            reply_markup=_cta_keyboard(),
        )
        final_msg_id = msg.message_id

    elif yes_count == 1:
        text = (
            "Судя по Вашим ответам, Вы практически не позволяете страху менять Ваш образ жизни. Это отлично!\n\n"
            "Потому что <b><i>избегание</i></b> часто загоняет в ловушку:\n"
            + chain
            + "\n\n"
            "Вы уже почитали в моём гайде о том, как правильно отвечать себе на пугающие <u>мысли</u>. "
            "Теперь можно и в <u>действиях</u> вернуть себе полностью нормальную жизнь 🪂\n\n"
            "Возьмите тот единственный пункт, который Вы ответили «Да», и делайте его наоборот.\n\n"
            "🔹 Привыкли всегда носить с собой бутылку воды? 👉🏼 Оставьте её дома!\n"
            "🔹 Держите окно приоткрытым? 👉🏼 Постарайтесь подольше побыть в небольшом дефиците кислорода.\n"
            "И т.п.\n\n"
            "Но не всё сразу! Возьмите для изменения сначала только одно правило и поработайте пару недель над отказом от него.\n\n"
            "Это будет дискомфортно, но я обещаю: это даст Вам больше уверенности в своей способности справляться со страхом 🦁\n\n"
            "Попробуете?"
        )
        msg = await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=_cta_keyboard(),
        )
        final_msg_id = msg.message_id

    else:
        text = (
            "Судя по Вашим ответам, Вы не позволяете страху менять Ваш образ жизни. Это отлично!\n\n"
            "Если у Вас есть какие-то <b><i>избегания</i></b>, которые не попали в опросник, то теперь — держа под рукой памятку — "
            "можно и в <u>действиях</u> вернуть себе полностью нормальную жизнь.\n\n"
            "Примеры:\n"
            "🔹 Стараетесь не вспоминать про паническую атаку? 👉🏼 Повспоминайте про неё специально.\n\n"
            "🔹 Избегаете места первого приступа? 👉🏼 Посетите его ещё раз.\n\n\n"
            "Это будет дискомфортно, но я обещаю: это даст Вам больше уверенности в своей способности справляться со страхом 🦁\n\n"
            "Попробуете?"
        )
        msg = await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=_cta_keyboard(),
        )
        final_msg_id = msg.message_id

    if final_msg_id is not None:
        schedule_message(
            user_id=chat_id,
            prod_seconds=24 * 60 * 60,
            test_seconds=10,
            kind="case_story",
            payload=str(final_msg_id),
        )


# =========================================================
# 4. ПОСЛЕ ТЕСТА
# =========================================================

def _cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Хорошо 😌",
                    callback_data="avoidance_ok",
                ),
                InlineKeyboardButton(
                    text="Нет, пока боюсь 🙈",
                    callback_data="avoidance_scared",
                ),
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

    await bot.send_message(
        chat_id,
        "Супер! У Вас всё получится! 💪🏼",
    )
    log_event(
        chat_id,
        "Ответ на блок с предложением экспозиции",
        "Ответ: «Хорошо 😌»",
    )

    schedule_message(
        user_id=chat_id,
        prod_seconds=60 * 60,
        test_seconds=10,
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

    await bot.send_message(
        chat_id,
        "Ничего, иногда нужно собраться с силами, чтобы решиться на то, что тревожно 🫶🏼",
    )
    log_event(
        chat_id,
        "Ответ на блок с предложением экспозиции",
        "Ответ: «Нет, пока боюсь 🙈»",
    )

    schedule_message(
        user_id=chat_id,
        prod_seconds=60 * 60,
        test_seconds=10,
        kind="case_story",
        payload=str(callback.message.message_id),
    )


# =========================================================
# 5. ИСТОРИЯ ПАЦИЕНТКИ
# =========================================================

async def send_case_story(chat_id: int, payload: str | None):
    upsert_user(chat_id, step="история_пациентки")

    # Если пришли с автоперехода — убираем старую кнопку "Начать тест"
    if payload:
        try:
            msg_id = int(payload)
            try:
                await bot.edit_message_reply_markup(
                    chat_id,
                    msg_id,
                    reply_markup=None,
                )
            except TelegramBadRequest:
                # Кнопка уже могла быть снята — просто игнорируем
                pass
        except Exception:
            pass

    text1 = (
        "Я расскажу Вам историю одной пациентки, с которой мы работали над паническими атаками.\n\n"
        "Она начала замечать, что больше не может ездить в метро: сердце начинало биться все чаще, "
        "возникало ощущение нехватки воздуха, и каждый раз ей казалось, что она не выдержит и потеряет сознание.\n\n"
        "Со временем она стала избегать любых ситуаций, где чувствовала, что не сможет быстро выйти: "
        "кинотеатры, торговые центры, даже длинные очереди в магазине.\n\n"
        "С каждым месяцем её мир становился всё меньше 🌍⬇️"
    )

    text2 = (
        "Когда мы начали работать, оказалось, что она постоянно проверяла своё состояние:\n"
        "прислушивалась к сердцу, контролировала дыхание, оцениваала, не закружится ли голова.\n\n"
        "Каждый выход из дома превращался для неё в экзамен, который она боялась провалить.\n\n"
        "На наших встречах мы постепенно начали менять её отношение к этим ощущениям.\n"
        "Мы учились <i>оставаться</i> в ситуациях, которые раньше казались невыносимыми, "
        "и позволять телу реагировать так, как оно реагирует — без попыток всё время себя спасать.\n\n"
        "По мере того как она переставала избегать «опасные» места, панические атаки начали происходить всё реже, "
        "а затем практически сошли на нет."
    )

    await bot.send_message(chat_id, text1)
    await smart_sleep(chat_id, prod_seconds=60 * 60, test_seconds=3)
    await bot.send_message(chat_id, text2)

    schedule_message(
        chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=10,
        kind="final_block1",
    )


# =========================================================
# 6. ЗАКЛЮЧИТЕЛЬНЫЕ БЛОКИ
# =========================================================

async def send_final_message(chat_id: int):
    upsert_user(chat_id, step="приглашение_на_консультацию")
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    photo = FSInputFile("media/DSC03503.jpg")

    caption = (
        "С людьми, переживающими панические атаки, я работаю каждый день, "
        "и я хорошо знаю, как важно не откладывать обращение за помощью. "
        "Потому что со временем тревога перестаёт быть лишь реакцией на стресс и начинает определять Ваш образ мыслей и восприятия.\n\n"
        "<b>Как я могу помочь Вам?</b>\n\n"
        "На индивидуальных консультациях мы можем вместе разобрать, из чего складывается <i>именно Ваш цикл тревоги</i>: "
        "какие мысли, телесные реакции и привычные способы поведения поддерживают его. Мы составим для Вас подробный план действий, "
        "который позволит шаг за шагом развернуть этот цикл вспять.\n\n"
        "Я работаю в современном научно обоснованном подходе — когнитивно-поведенческой терапии (КПТ), "
        "которая считается золотым стандартом в лечении тревожных расстройств.\n\n"
        "По итогам прохождения психотерапии Вы получите:\n\n"
        "✨ снижение гиперконтроля и проверок собственного состояния\n\n"
        "✨ способность снова свободно выходить из дома, ездить в метро, летать в самолётах, водить машину\n\n"
        "✨ умение оставаться в контакте с тревогой, не убегая от неё\n\n"
        "✨ способность жить спонтанно и легко, не подстраиваясь под ограничения\n\n"
        "✨ внутреннюю уверенность, что с Вами всё в порядке\n\n"
        "Почитать подробнее о том, как проходит психотерапия со мной 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Узнать про консультации",
                    callback_data="consult_show",
                )
            ]
        ]
    )

    try:
        await bot.send_photo(chat_id, photo=photo, caption=caption, reply_markup=kb)
        log_event(
            chat_id,
            "Отправлен блок с приглашением на консультацию",
            None,
        )
    except Exception as e:
        log_event(
            chat_id,
            "Ошибка отправки блока с консультацией",
            str(e),
        )

    schedule_message(
        chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=10,
        kind="final_block2",
    )


@router.callback_query(F.data == "consult_show")
async def consult_show(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    upsert_user(chat_id, step="перешел_к_описанию_консультаций")
    log_event(
        chat_id,
        "Открыт раздел консультаций",
        "Нажата кнопка «Узнать про консультации»",
    )

    text = (
        "Прочитать про консультации можно здесь:\n"
        "https://лечение-паники.рф/консультации"
    )

    try:
        await bot.send_message(
            chat_id,
            text,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log_event(
            chat_id,
            "Ошибка отправки ссылки на консультации",
            str(e),
        )


async def send_final_block2(chat_id: int):
    upsert_user(chat_id, step="поддерживающее_сообщение")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    text = (
        "Я знаю, что сделать первый шаг к работе с тревогой бывает непросто.\n\n"
        "Многие люди откладывают обращение за помощью, надеясь, что «само пройдёт», "
        "или стараясь держаться из последних сил, пока тревога не становится слишком тяжёлой.\n\n"
        "Но чем раньше Вы начнёте разбираться с тем, что происходит, тем быстрее сможете вернуть себе ощущение опоры и спокойствия.\n\n"
        "Если Вы чувствуете, что готовы хотя бы <i>рассмотреть</i> возможность психотерапии, — я рядом и буду рад помочь Вам в этом пути 🌿"
    )

    await bot.send_message(chat_id, text)

    schedule_message(
        chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=10,
        kind="final_block3",
    )


async def send_final_block3(chat_id: int):
    upsert_user(chat_id, step="финальное_приглашение")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    text = (
        "Если Вы чувствуете, что устали жить в ожидании очередного приступа паники "
        "и хотите снова свободно дышать, выходить из дома без оглядки и ощущать, что Ваша жизнь принадлежит Вам — "
        "я буду рад поддержать Вас на этом пути.\n\n"
        "Записаться на консультацию, задать вопросы о формате работы и подобрать удобное время можно, написав мне в чат 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Написать Дмитрию в Telegram",
                    url="https://t.me/OcdAndAnxiety",
                )
            ]
        ]
    )

    await bot.send_message(
        chat_id,
        text,
        reply_markup=kb,
    )

    schedule_message(
        user_id=chat_id,
        prod_seconds=7 * 24 * 60 * 60,
        test_seconds=10,
        kind="chat_invite",
        payload=None,
    )


async def send_chat_invite(chat_id: int):
    upsert_user(chat_id, step="напоминание_о_чате")

    text = (
        "Напоминаю, что Вы можете написать мне в Telegram, "
        "если захотите обсудить возможность психотерапии или задать вопросы по поводу работы с паническими атаками.\n\n"
        "Я открыт к диалогу и буду рад помочь Вам разобраться в происходящем 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Написать Дмитрию в Telegram",
                    url="https://t.me/OcdAndAnxiety",
                )
            ]
        ]
    )

    await bot.send_message(chat_id, text, reply_markup=kb)


# =========================================================
# MAIN
# =========================================================

async def main():
    asyncio.create_task(scheduler_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
