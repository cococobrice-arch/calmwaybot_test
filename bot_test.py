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

# -------------------- Логи --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------- Переменные окружения --------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
LINK = os.getenv("LINK_TO_MATERIAL")  # ссылка или локальный путь
VIDEO_NOTE_FILE_ID = os.getenv("VIDEO_NOTE_FILE_ID")
DB_PATH = os.getenv("DATABASE_PATH", "users.db")
CHANNEL_USERNAME = "@OcdAndAnxiety"

MODE = os.getenv("MODE", "prod").lower()  # "prod" или "test"
FAST_USER_ID_RAW = os.getenv("FAST_USER_ID", "")
FAST_USER_ID = int(FAST_USER_ID_RAW) if FAST_USER_ID_RAW.isdigit() else None

SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL", "10"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# =========================================================
# 0. БАЗА ДАННЫХ
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            source TEXT,
            step TEXT,
            subscribed INTEGER DEFAULT 0,
            last_action TEXT,
            username TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS answers (
            user_id INTEGER,
            question INTEGER,
            answer TEXT,
            PRIMARY KEY (user_id, question)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            timestamp TEXT,
            action TEXT,
            details TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            send_at TEXT,
            kind TEXT,
            payload TEXT,
            delivered INTEGER DEFAULT 0
        )
        """
    )

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
    user_id: int, step: str | None = None, subscribed: int | None = None, username: str | None = None
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
            "INSERT INTO users (user_id, source, step, subscribed, last_action, username) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, "unknown", step or "start", subscribed or 0, now, username),
        )

    conn.commit()
    conn.close()


def purge_user(user_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
    send_at_str = send_at.isoformat(timespec="seconds")

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
        (user_id, kind),
    )

    cursor.execute(
        "INSERT INTO scheduled_messages (user_id, send_at, kind, payload) VALUES (?, ?, ?, ?)",
        (user_id, send_at_str, kind, payload),
    )

    conn.commit()
    conn.close()

    log_event(user_id, "scheduled_message_created", f"{kind} @ {send_at_str}")


def mark_message_delivered(task_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_messages SET delivered=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


# =========================================================
# 0.1. ОБРАБОТКА ОТЛОЖЕННЫХ ЗАДАЧ
# =========================================================
async def process_scheduled_message(task_id: int, user_id: int, kind: str, payload: str | None):
    try:
        if kind == "channel_invite":
            await send_channel_invite(user_id)
        elif kind == "avoidance_intro":
            await send_avoidance_intro(user_id)
        elif kind == "case_story":
            await send_case_story(user_id)
        elif kind == "final_block1":
            await send_final_message(user_id)
        elif kind == "final_block2":
            await send_final_block2(user_id)
        elif kind == "final_block3":
            await send_final_block3(user_id)
        else:
            log_event(user_id, "scheduled_message_unknown", kind)
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
# 1. ПРИВЕТСТВИЕ
# =========================================================
@router.message(F.text == "/start")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    uname = (message.from_user.username or "").strip() or None

    # В тестовом режиме всегда очищаем историю пользователя при новом /start
    if MODE == "test":
        purge_user(user_id)

    upsert_user(user_id, step="start", username=uname)
    log_event(user_id, "user_start", "Пользователь запустил бота")

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
# 2. ОТПРАВКА ГАЙДА
# =========================================================
@router.callback_query(F.data == "get_material")
async def send_material(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    uname = (callback.from_user.username or "").strip() or None

    upsert_user(chat_id, step="got_material", username=uname)
    log_event(chat_id, "user_clicked_get_material", "Нажал «Получить гайд»")

    # Кружок
    if VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_chat_action(chat_id, "upload_video_note")
            await bot.send_video_note(chat_id, VIDEO_NOTE_FILE_ID)
        except Exception as e:
            logger.warning(f"Не удалось отправить кружок: {e}")

    # Материал
    if LINK and os.path.exists(LINK):
        file = FSInputFile(LINK, filename="Выход из панического круга.pdf")
        await bot.send_document(
            chat_id,
            document=file,
            caption="Вот Ваш первый шаг к спокойствию 🧘🏻‍♀️",
        )
    elif LINK and LINK.startswith("http"):
        await bot.send_message(chat_id, f"📘 Ваш материал доступен по ссылке: {LINK}")
    else:
        await bot.send_message(chat_id, "⚠️ Файл не найден. Попробуйте позже.")

    # 2) приглашение в канал — через 20 минут
    schedule_message(
        user_id=chat_id,
        prod_seconds=20 * 60,
        test_seconds=5,
        kind="channel_invite",
    )

    # 3) приглашение к тесту избегания — через сутки
    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=5,
        kind="avoidance_intro",
    )

    # 4) история пациента по умолчанию через сутки
    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=30,
        kind="case_story",
    )

    await callback.answer()


async def send_channel_invite(chat_id: int):
    text = (
        "Если Вы хотите глубже разобраться в механизмах паники и тревоги, "
        "подписывайтесь на мой канал — там я подробно разбираю реальные случаи из практики, "
        "делюсь техниками и объясняю, как шаг за шагом выходить из панического круга.\n\n"
        f"Подписаться можно здесь: {CHANNEL_USERNAME}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перейти в канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
                )
            ]
        ]
    )
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        log_event(chat_id, "channel_invite_sent", "Отправлено приглашение в канал")
    except Exception:
        log_event(
            chat_id,
            "channel_invite_failed",
            "Ошибка при отправке приглашения в канал",
        )
# =========================================================
# 3. ОПРОС ПО ИЗБЕГАНИЮ
# =========================================================

avoidance_questions = [
    "Вы часто измеряете давление или пульс? 💓",
    "Когда выходите из дома, берёте с собой бутылку воды? 💧",
    "Вам пришлось отказаться от спорта или физических нагрузок из-за опасений? 🧎🏻‍♀️‍➡️",
    "Стараетесь не оставаться в одиночестве? 👥",
    "Стали частро открывать окно, чтобы не было душно? 💨",
    "В общественных местах предпочитаете садиться поближе к выходу? 🚪",
    "Отвлекаетесь в телефон, чтобы не замечать неприятные телесные ощущения? 📲",
    "Избегаете поездок за город, чтобы не оставаться без мобильной связи и интернета? 📶"
]


async def send_avoidance_intro(chat_id: int):
    text = (
        "Вам может казаться, что панические атаки продолжают возникать, несмотя на то что Вы стараетесь их не провоцировать.\n"
        "Давайте проверим, насколько ваши привычки действительно помогают, а где — мешают?\n\n"
        "Пройдите короткий тест — всего 8 вопросов с ответами Да/Нет 🗳"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Начать тест", callback_data="avoidance_start")]]
    )
    await bot.send_message(chat_id, text, reply_markup=kb)
    log_event(chat_id, "user_clicked_avoidance_intro", "Предложен опрос избегания")


@router.callback_query(F.data == "avoidance_start")
async def start_avoidance_test(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    # очистка старых ответов
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM answers WHERE user_id=?", (chat_id,))
    conn.commit()
    conn.close()

    upsert_user(chat_id, step="avoidance_test")
    log_event(chat_id, "user_clicked_avoidance_start", "Начал опрос избегания")

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

        # ответы НЕ логируем
        # user_answer — удалён

        if idx + 1 < len(avoidance_questions):
            await send_question(chat_id, idx + 1)
        else:
            await finish_test(chat_id)

        # убираем кнопки у предыдущего сообщения
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass

    except Exception as e:
        logger.error(f"Ошибка ответа: {e}")
        try:
            await bot.send_message(chat_id, "Ошибка обработки ответа. Попробуйте ещё раз.")
        except:
            pass


# =========================================================
# 3.1. ИТОГ ТЕСТА
# =========================================================

def _cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Хорошо 😌", callback_data="avoidance_ok"),
                InlineKeyboardButton(text="Нет, пока боюсь 🙈", callback_data="avoidance_scared")
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

    await bot.send_message(chat_id, "Супер! У Вас всё получится! 💪🏼")
    log_event(chat_id, "user_avoidance_response", "Ответил: Хорошо 😌")


@router.callback_query(F.data == "avoidance_scared")
async def handle_avoidance_scared(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    await bot.send_message(chat_id, "Ничего, иногда нужно собраться с силами, чтобы решиться на то, что тревожно 🫶🏼")
    log_event(chat_id, "user_avoidance_response", "Ответил: Нет, пока боюсь 🙈")


async def finish_test(chat_id: int):
    yes_count = 0  # ответы не сохраняем

    upsert_user(chat_id, step="avoidance_done")
    log_event(chat_id, "user_finished_test", "Тест завершён")

    chain = (
        "Чем больше вынужденных ограничений мы накладываем на свою жизнь\n"
        "️⬇️\nтем большую важность мы придаём панике\n"
        "⬇️\nТем больше концентрируемся на своём теле\n"
        "⬇️\nТем больше чувствуем в нём неожиданные/неприятные ощущения\n"
        "⬇️\nТем больше переживаем по поводу них.\n\nИ так до бесконечности 🔄"
    )

    await bot.send_message(chat_id, "Тест завершён. Подождите секунду, обрабатываем результаты ⏳")
    await smart_sleep(chat_id, prod_seconds=3, test_seconds=1)

    text = (
        "Судя по Вашим ответам, Вы практически не позволяете страху менять Ваш образ жизни. Это отлично!\n\n"
        "Потому что <b><i>избегание</i></b> часто загоняет в ловушку:\n" + chain + "\n\n"
        "Вы уже почитали в моём гайде о том, как правильно отвечать себе на пугающие <u>мысли</u>. "
        "Теперь можно и в <u>действиях</u> вернуть себе полностью нормальную жизнь 🪂\n\n"
        "Возьмите тот единственный пункт, который Вы ответили «Да», и делайте его наоборот.\n\n"
        "🔹 Привыкли всегда носить с собой бутылку воды? 👉🏼 Оставьте её дома!\n"
        "🔹 Держите окно приоткрытым? 👉🏼 Постарайтесь подольше побыть в небольшом дефиците кислорода.\n\n"
        "Но не всё сразу! Возьмите для изменения сначала только одно правило и поработайте пару недель над отказом от него.\n\n"
        "Это будет дискомфортно, но я обещаю: это даст Вам больше уверенности в Вашей способности справляться со страхом 🦁\n\n"
        "Попробуете?"
    )

    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=_cta_keyboard())

    # -----------------------------------------------
    # НОВАЯ ЛОГИКА: если человек ответил на «Попробуете?»
    # → история через 1 час / 5 сек
    # Если не ответил → через сутки / 30 сек
    # -----------------------------------------------

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT action FROM events WHERE user_id=? AND action='user_avoidance_response' ORDER BY id DESC LIMIT 1",
        (chat_id,)
    )
    answered = cursor.fetchone()
    conn.close()

    if answered:
        schedule_message(
            user_id=chat_id,
            prod_seconds=60 * 60,
            test_seconds=5,
            kind="case_story"
        )
    else:
        schedule_message(
            user_id=chat_id,
            prod_seconds=24 * 60 * 60,
            test_seconds=30,
            kind="case_story"
        )
