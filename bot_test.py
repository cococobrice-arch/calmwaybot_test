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
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

# =========================================================
# ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не указан BOT_TOKEN в .env")

MODE = os.getenv("MODE", "prod").lower()
FAST_USER_ID = int(os.getenv("FAST_USER_ID", "0") or 0)

DB_PATH = os.getenv("DB_PATH", "users.db")

LINK = os.getenv("LINK", "")
VIDEO_NOTE_FILE_ID = os.getenv("VIDEO_NOTE_FILE_ID", "")

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
CHAT_USERNAME = os.getenv("CHAT_USERNAME", "")

# =========================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# =========================================================
# ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА
# =========================================================

session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, session=session, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =========================================================
# ФУНКЦИИ РАБОТЫ С БД
# =========================================================


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

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

    conn.commit()
    conn.close()


def log_event(user_id: int, action: str, details: str | None = None):
    ts = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO events (user_id, timestamp, action, details) VALUES (?, ?, ?, ?)",
        (user_id, ts, action, details),
    )
    conn.commit()
    conn.close()


def upsert_user(
    user_id: int,
    step: str | None = None,
    username: str | None = None,
    source: str | None = None,
    subscribed: int | None = None,
    consult_interested: int | None = None,
):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    now = datetime.now().isoformat(timespec="seconds")

    if row:
        fields = []
        params: list[object] = []
        if step is not None:
            fields.append("step=?")
            params.append(step)
        if username is not None:
            fields.append("username=?")
            params.append(username)
        if source is not None:
            fields.append("source=?")
            params.append(source)
        if subscribed is not None:
            fields.append("subscribed=?")
            params.append(subscribed)
        if consult_interested is not None:
            fields.append("consult_interested=?")
            params.append(consult_interested)

        fields.append("last_action=?")
        params.append(now)
        params.append(user_id)

        cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", params)
    else:
        cursor.execute(
            """
            INSERT INTO users (user_id, source, step, subscribed, last_action, username, consult_interested)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                source or "unknown",
                step or "старт",
                subscribed if subscribed is not None else 0,
                now,
                username,
                consult_interested if consult_interested is not None else 0,
            ),
        )

    conn.commit()
    conn.close()


def purge_user(user_id: int, keep_events: bool = True):
    """
    Полная очистка состояния пользователя.
    Если keep_events=False — стираем также события (для тестового пользователя).
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    cursor.execute("DELETE FROM answers WHERE user_id=?", (user_id,))
    cursor.execute("DELETE FROM scheduled_messages WHERE user_id=?", (user_id,))

    if not keep_events:
        cursor.execute("DELETE FROM events WHERE user_id=?", (user_id,))

    conn.commit()
    conn.close()


# =========================================================
# УМНЫЙ SLEEP И ПЛАНИРОВАНИЕ
# =========================================================

def is_fast_user(user_id: int) -> bool:
    return user_id == FAST_USER_ID


async def smart_sleep(user_id: int, prod_seconds: int, test_seconds: int):
    """
    Глобальное поведение:
    - Если MODE=test → использовать test_seconds
    - Если пользователь FAST_USER_ID → всегда test_seconds
    - Иначе prod_seconds
    """
    if MODE == "test" or is_fast_user(user_id):
        delay = test_seconds
    else:
        delay = prod_seconds

    if delay <= 0:
        return

    await asyncio.sleep(delay)


def schedule_message(
    user_id: int,
    prod_seconds: int,
    test_seconds: int,
    kind: str,
    payload: str | None = None,
    exact_time: datetime | None = None,
):
    """
    Ставит отложенное сообщение. Если указан exact_time, он имеет приоритет,
    иначе считается как now + delay.
    """
    if MODE == "test" or is_fast_user(user_id):
        delay = test_seconds
    else:
        delay = prod_seconds

    if exact_time is not None:
        send_at = exact_time
    else:
        send_at = datetime.now() + timedelta(seconds=delay)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO scheduled_messages (user_id, send_at, kind, payload, delivered)
        VALUES (?, ?, ?, ?, 0)
        """,
        (user_id, send_at.isoformat(timespec="seconds"), kind, payload),
    )
    conn.commit()
    conn.close()


async def process_scheduled_message(task_id: int, user_id: int, kind: str, payload: str | None):
    try:
        if kind == "channel_invite":
            await send_channel_invite(user_id)
        elif kind == "avoidance_intro":
            await send_avoidance_intro(user_id)
        elif kind == "case_story":
            await send_case_story(user_id, payload)
        elif kind == "case_story_auto":
            await send_case_story(user_id, payload)
        elif kind == "final_block1":
            await send_final_message(user_id)
        elif kind == "final_block2":
            await send_final_block2(user_id)
        elif kind == "final_block3":
            await send_final_block3(user_id)
        elif kind == "chat_invite":
            await send_chat_invite(user_id)
        elif kind == "avoidance_timeout":
            await timeout_finish_test(user_id)
        else:
            logger.warning(f"Неизвестный тип отложенного сообщения: {kind}")
            log_event(user_id, "Неизвестный тип отложенного сообщения", kind)

        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE scheduled_messages SET delivered=1 WHERE id=?",
            (task_id,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception(f"Ошибка обработки отложенного сообщения {task_id}: {e}")
        log_event(user_id, "Ошибка обработки отложенного сообщения", f"{kind}: {e}")


async def scheduler_worker():
    """
    Фоновый воркер, который каждые несколько секунд проверяет таблицу scheduled_messages.
    """
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
                """,
                (now,),
            )
            rows = cursor.fetchall()
            conn.close()

            for task_id, user_id, kind, payload in rows:
                asyncio.create_task(process_scheduled_message(task_id, user_id, kind, payload))

        except Exception as e:
            logger.exception(f"Ошибка в scheduler_worker: {e}")

        await asyncio.sleep(3)


# =========================================================
# ПРОВЕРКА ПОДПИСКИ НА КАНАЛ
# =========================================================

async def is_user_subscribed_to_channel(user_id: int) -> bool:
    if not CHANNEL_USERNAME:
        return False

    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.info(f"Не удалось проверить подписку пользователя {user_id}: {e}")
        return False


# =========================================================
# 1. ОБРАБОТЧИК /start
# =========================================================

@router.message(F.text.startswith("/start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = (message.from_user.username or "").strip() or None

    # ---- ОПРЕДЕЛЯЕМ ИСТОЧНИК ----
    source = "unknown"
    parts = message.text.split(" ", 1)
    if len(parts) > 1:
        param = parts[1].strip()
        if param == "channel":
            source = "telegram-channel"
    # ------------------------------

    TEST_USER_ID = int(os.getenv("FAST_USER_ID", "0") or 0)

    # ---- ПРОВЕРЯЕМ, НОВЫЙ ЛИ ЭТО ПОЛЬЗОВАТЕЛЬ ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT step FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    # ---- ЕСЛИ ЮЗЕР УЖЕ В БАЗЕ И ЭТО НЕ ПЕРВЫЙ СТАРТ → НЕ ПОКАЗЫВАЕМ ПРИВЕТСТВИЕ ----
    if row is not None and row[0] != "старт":
        log_event(user_id, "Повторный вход через /start – приветствие не показываем")
        await message.answer("Вы уже начали работу со мной — продолжайте в удобном темпе 🙂")
        return

    # ---- ЕСЛИ ЭТО ТЕСТОВЫЙ ПОЛЬЗОВАТЕЛЬ → ПОЛНАЯ ОЧИСТКА ----
    if user_id == TEST_USER_ID:
        purge_user(user_id, keep_events=False)
        log_event(user_id, "Очистка тестового пользователя")
    else:
        # ---- НОВЫЙ ЮЗЕР: ОЧИСТИМ USERS/ANSWERS/MSG, НО ОСТАВИМ events ----
        purge_user(user_id, keep_events=True)

    # ---- ЗАПИСЫВАЕМ ИСТОЧНИК И СОЗДАЁМ ЗАПИСЬ ПОЛЬЗОВАТЕЛЯ ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    exists = cursor.fetchone()

    now = datetime.now().isoformat(timespec="seconds")

    if exists:
        cursor.execute(
            "UPDATE users SET step=?, username=?, source=?, last_action=? WHERE user_id=?",
            ("старт", username, source, now, user_id),
        )
    else:
        cursor.execute(
            "INSERT INTO users (user_id, source, step, subscribed, last_action, username, consult_interested) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, source, "старт", 0, now, username, 0),
        )

    conn.commit()
    conn.close()

    log_event(user_id, "Запуск бота", f"source={source}")

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
# Ручной сброс состояния пользователя (команда /reset)
# =========================================================

@router.message(F.text == "/reset")
async def reset_user(message: Message):
    user_id = message.from_user.id

    # Полностью очистить состояние, но оставить события (логи)
    purge_user(user_id, keep_events=True)

    log_event(user_id, "Пользователь вручную сбросил состояние", None)

    await message.answer(
        "История взаимодействия очищена.\n\n"
        "Чтобы начать заново — введите /start"
    )


# =========================================================
# 2. ГАЙД: ПОЛУЧЕНИЕ МАТЕРИАЛА
# =========================================================

@router.callback_query(F.data == "get_material")
async def send_material(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    username = callback.from_user.username or None

    # ---- ПРОВЕРКА: ПОЛЬЗОВАТЕЛЬ УЖЕ ПОЛУЧАЛ МАТЕРИАЛ ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT step FROM users WHERE user_id=?", (chat_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] != "старт":
        # Убираем клавиатуру, если она вдруг осталась
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer("Материал уже был выдан ранее.")
        return
    # -----------------------------------------------------

    # ---- УБИРАЕМ КЛАВИАТУРУ ПОСЛЕ НАЖАТИЯ ----
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # -----------------------------------------------------

    # ---- ОБНОВЛЯЕМ СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЯ ----
    upsert_user(chat_id, step="получил_гайд", username=username)
    log_event(chat_id, "Нажата кнопка «Получить гайд»", "Начало выдачи материала")

    # ---- ОТПРАВКА ПРИВЕТСТВЕННОГО КРУЖКА ----
    if VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_chat_action(chat_id, "upload_video_note")
            await bot.send_video_note(chat_id, VIDEO_NOTE_FILE_ID)
        except Exception as e:
            logger.warning(f"Ошибка отправки кружка: {e}")
            log_event(chat_id, "Ошибка отправки приветственного видео", str(e))

    # ---- ОТПРАВКА PDF ----
    if LINK and os.path.exists(LINK):
        file = FSInputFile(LINK, filename="Выход из панического круга.pdf")
        await bot.send_document(chat_id, document=file, caption="Вот Ваш первый шаг к спокойствию 🧘🏻‍♀️")
        log_event(chat_id, "Отправлен файл с гайдом", "Гайд отправлен как документ")
    elif LINK and LINK.startswith("http"):
        await bot.send_message(chat_id, f"📘 Ваш материал доступен по ссылке: {LINK}")
        log_event(chat_id, "Отправлена ссылка на гайд", LINK)
    else:
        await bot.send_message(chat_id, "⚠️ Файл не найден.")
        log_event(chat_id, "Не удалось найти файл гайда", LINK or "Путь не задан")

    # ---- ПЛАНИРОВАНИЕ ПРИГЛАШЕНИЯ В КАНАЛ И ВВОДА ТЕСТА ----
    schedule_message(chat_id, prod_seconds=20 * 60, test_seconds=5, kind="channel_invite")
    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=10, kind="avoidance_intro")

    await callback.answer()


async def send_channel_invite(chat_id: int):
    """
    Сообщение-приглашение в канал — только для НЕподписанных пользователей
    """
    try:
        subscribed_now = await is_user_subscribed_to_channel(chat_id)
        if subscribed_now:
            log_event(chat_id, "Пропущено приглашение в канал (уже подписан)", None)
            upsert_user(chat_id, subscribed=1)
            return

        text = (
            "Через несколько дней я подготовлю для Вас ещё несколько материалов, которые покажут, "
            "как ослабить власть паники над Вашей жизнью.\n\n"
            "Чтобы не пропустить их, Вы можете подписаться на мой канал, где я регулярно разбираю "
            "сложные случаи, даю пояснения к методам терапии и отвечаю на вопросы.\n\n"
            "Подписывайтесь — там больше примеров, разборов и живого общения."
        )

        kb = None
        if CHANNEL_USERNAME:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Перейти в канал",
                            url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}",
                        )
                    ]
                ]
            )

        await bot.send_message(chat_id, text, reply_markup=kb)
        log_event(chat_id, "Отправлено приглашение в канал", None)

    except Exception as e:
        logger.error(f"Ошибка отправки приглашения в канал: {e}")
        log_event(chat_id, "Ошибка отправки приглашения в канал", str(e))


# =========================================================
# 3. ТЕСТ ИЗБЕГАНИЯ: ВОПРОСЫ
# =========================================================

avoidance_questions = [
    "Есть ли места, в которые Вы избегаете ходить из-за страха, что там может случиться паническая атака?",
    "Бывает ли, что Вы планируете маршрут так, чтобы рядом всегда был 'безопасный человек' или возможность быстро вернуться домой?",
    "Держите ли Вы при себе 'спасительные' предметы (таблетки, вода, еда), без которых Вам трудно выйти из дома?",
    "Бывает ли, что Вы отказываетесь от поездок в общественном транспорте из-за страха 'застрять' там во время приступа?",
    "Ограничиваете ли Вы физическую нагрузку (спорт, подъём по лестнице), чтобы не спровоцировать учащённое сердцебиение?",
    "Есть ли ситуации, в которых Вы соглашаетесь участвовать только при условии, что сможете в любой момент выйти или уйти?",
    "Замечали ли Вы, что всё чаще выбираете 'безопасные' места и привычные маршруты, даже если они менее удобны?",
    "Чувствуете ли Вы, что ради ощущения безопасности Ваш мир постепенно сужается?",
]


async def send_avoidance_intro(chat_id: int):
    """
    Вводное сообщение перед тестом избегания.
    """
    upsert_user(chat_id, step="предложен_тест_избегания")
    log_event(chat_id, "Отправлено приглашение пройти тест избегания", None)

    text = (
        "Чтобы ослабить власть паники, важно понять, как сильно она уже вмешалась в Вашу жизнь.\n\n"
        "Я подготовил небольшой тест, который поможет увидеть, насколько избегание управляет Вашими решениями.\n\n"
        "Он займёт всего пару минут. Отвечайте честно — это нужно только Вам.\n\n"
        "Готовы пройти тест?"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Начать тест", callback_data="avoidance_start")]
        ]
    )

    msg = await bot.send_message(chat_id, text, reply_markup=kb)

    # Автопереход к истории пациентки, если человек не нажмёт кнопку (отложенное сообщение)
    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=30,
        kind="case_story_auto",
        payload=str(msg.message_id),
    )


@router.callback_query(F.data == "avoidance_start")
async def start_avoidance_test(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    # ---- ПРОВЕРКА: НЕ НАЧИНАЛ ЛИ ПОЛЬЗОВАТЕЛЬ ТЕСТ РАНЬШЕ ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT step FROM users WHERE user_id=?", (chat_id,))
    row = cursor.fetchone()
    conn.close()

    # Если пользователь уже проходил тест, повторно запускать нельзя
    if row and row[0] not in ("предложен_тест_избегания", "тест_избегания_начат"):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer("Вы уже проходили этот тест.")
        return

    # ---- УДАЛЯЕМ КЛАВИАТУРУ "Начать тест" ----
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # ---- УДАЛЯЕМ АВТОЗАДАЧУ перехода к истории пациентки ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
        (chat_id, "case_story_auto"),
    )
    conn.commit()
    conn.close()

    # ---- СБРАСЫВАЕМ ПРЕДЫДУЩИЕ ОТВЕТЫ (ЕСЛИ ЕСТЬ) ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM answers WHERE user_id=?", (chat_id,))
    conn.commit()
    conn.close()

    # ---- УСТАНАВЛИВАЕМ НОВЫЙ ШАГ ----
    upsert_user(chat_id, step="тест_избегания_начат")
    log_event(chat_id, "Начат тест избегания", "Нажата кнопка «Начать тест»")

    # ---- СТАВИМ ТАЙМ-АУТ ДЛЯ ТЕСТА ----
    schedule_message(
        user_id=chat_id,
        prod_seconds=3 * 24 * 60 * 60,
        test_seconds=20,
        kind="avoidance_timeout",
    )

    # ---- ПЕРВОЕ СООБЩЕНИЕ ТЕСТА ----
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

        log_event(
            chat_id,
            "Ответ на вопрос теста избегания",
            f"Вопрос {idx + 1}, ответ: {'Да' if ans == 'yes' else 'Нет'}"
        )

        # ---- СБРАСЫВАЕМ СТАРЫЙ ТАЙМ-АУТ ТЕСТА ----
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
            (chat_id, "avoidance_timeout"),
        )
        conn.commit()
        conn.close()

        # ---- СТАВИМ НОВЫЙ ТАЙМ-АУТ ТЕСТА ----
        schedule_message(
            user_id=chat_id,
            prod_seconds=3 * 24 * 60 * 60,
            test_seconds=20,
            kind="avoidance_timeout",
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
            await bot.send_message(chat_id, "Ошибка обработки ответа. Попробуйте ещё раз.")
        except Exception:
            pass
        log_event(chat_id, "Ошибка обработки ответа теста избегания", str(e))


# =========================================================
# 3.1 — ФИНИШ ТЕСТА
# =========================================================

async def finish_test(chat_id: int):
    # ---- УДАЛЯЕМ ТАЙМ-АУТ ТЕСТА, ЕСЛИ ОН ЕЩЁ В ОЧЕРЕДИ ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
        (chat_id, "avoidance_timeout"),
    )
    conn.commit()
    conn.close()

    # ---- СЧИТЫВАЕМ ОТВЕТЫ ПОЛЬЗОВАТЕЛЯ ----
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT answer FROM answers WHERE user_id=?", (chat_id,))

    answers = [row[0] for row in cursor.fetchall()]
    conn.close()

    yes_count = answers.count("yes")
    upsert_user(chat_id, step="тест_избегания_завершен")
    log_event(chat_id, "Тест избегания завершен", f"Количество ответов «Да»: {yes_count}")

    chain = (
        "Чем больше вынужденных ограничений мы накладываем на свою жизнь\n"
        "️⬇️\nтем большую важность мы придаём панике\n"
        "⬇️\nТем больше концентрируемся на своём теле\n"
        "⬇️\nТем больше чувствуем в нём неожиданные/неприятные ощущения\n"
        "⬇️\nТем больше переживаем по поводу них.\n\n"
        "И так до бесконечности — как по кругу.\n\n"
        "Хорошая новость в том, что этот круг можно разорвать.\n\n"
        "В ближайшие дни я покажу, как постепенно возвращать себе свободу передвижения и ощущение, "
        "что Вы снова управляете своей жизнью, а не паника."
    )

    try:
        await bot.send_message(chat_id, chain)
        log_event(chat_id, "Отправлена цепочка после теста избегания", None)
    except Exception as e:
        log_event(chat_id, "Ошибка отправки цепочки после теста избегания", str(e))

    # через 3 секунды — интерпретация
    await smart_sleep(chat_id, prod_seconds=3, test_seconds=3)

    interpretation = ""
    if yes_count <= 2:
        interpretation = (
            "По Вашим ответам сейчас избегание пока ещё не полностью определяет Вашу жизнь.\n\n"
            "Это хороший момент для того, чтобы не позволить ему укорениться и постепенно расширять "
            "пространство свободы.\n\nВ следующих сообщениях я расскажу, как это сделать. "
        )
    elif 3 <= yes_count <= 5:
        interpretation = (
            "По Вашим ответам избегание уже заметно ограничивает Вашу повседневную жизнь.\n\n"
            "Это может проявляться в отказе от поездок, встреч, определённых мест и ситуаций.\n\n"
            "В ближайших сообщениях я покажу, как шаг за шагом возвращать себе эти области жизни."
        )
    else:
        interpretation = (
            "По Вашим ответам паника сильно влияет на Ваш выбор и образ жизни.\n\n"
            "Это может означать, что многие решения принимаются не Вами, а страхом возможного приступа.\n\n"
            "Но даже в этой ситуации можно постепенно вернуть себе ощущение опоры и контроля — "
            "я буду по шагам показывать, как это сделать."
        )

    try:
        await bot.send_message(chat_id, interpretation)
        log_event(chat_id, "Отправлена интерпретация результатов теста", None)
    except Exception as e:
        log_event(chat_id, "Ошибка отправки интерпретации результатов теста", str(e))

    # через минуту — сообщение «Хорошая новость…»
    schedule_message(chat_id, prod_seconds=60, test_seconds=5, kind="case_story")


# =========================================================
# 4. ИСТОРИЯ ПАЦИЕНТКИ И ДАЛЬНЕЙШАЯ ЦЕПОЧКА
# =========================================================

async def send_case_story(chat_id: int, payload: str | None):
    """
    История пациентки — как переход от теста к терапевтическому содержанию.
    """
    upsert_user(chat_id, step="история_пациентки")
    log_event(chat_id, "Отправлена история пациентки", f"payload={payload}")

    text = (
        "Хорошая новость состоит в том, что даже при выраженных панических атаках можно постепенно вернуть себе "
        "ощущение опоры и свободы.\n\n"
        "Например, одна из моих пациенток несколько лет избегала поездок в метро, торговых центров и любых "
        "мест, где «нельзя быстро выйти».\n\n"
        "Каждая попытка выйти из дома сопровождалась мыслями о том, что «если станет плохо, никто не поможет», "
        "и она снова оставалась дома.\n\n"
        "Мы начали с очень небольших шагов — сначала короткие выходы рядом с домом, затем поездки на одну-две "
        "остановки, отработку техник выдерживания волн тревоги.\n\n"
        "Постепенно её мир снова начал расширяться.\n\n"
        "Сейчас она спокойно ездит по городу, встречается с друзьями и не строит свою жизнь вокруг страха "
        "следующего приступа."
    )

    await bot.send_message(chat_id, text)

    # через сутки — следующий блок прогрева
    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=10, kind="final_block1")


async def send_final_message(chat_id: int):
    upsert_user(chat_id, step="финальный_блок_1")
    log_event(chat_id, "Отправлен финальный блок 1", None)

    text = (
        "Чтобы ослабить власть паники, мало только понимать, что «это всего лишь тревога».\n\n"
        "Важно менять те механизмы, которые поддерживают тревогу: избегание, постоянные проверки состояния, "
        "поиск «идеальных условий» и попытки полностью контролировать своё самочувствие.\n\n"
        "В терапии мы не просто обсуждаем, «почему так получилось», а шаг за шагом выстраиваем новую систему "
        "реакций на тревогу и панические симптомы.\n\n"
        "Если Вы чувствуете, что хотели бы пройти этот путь в сопровождении специалиста, я могу рассказать, "
        "как обычно строится работа со мной, из каких этапов она состоит и какие результаты мы ожидаем."
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

    await bot.send_message(chat_id, text, reply_markup=kb)

    # через сутки — второй блок прогрева
    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=10, kind="final_block2")


@router.callback_query(F.data == "consult_show")
async def consult_show(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    # Фиксируем реальный интерес к консультации только по нажатию кнопки
    upsert_user(chat_id, consult_interested=1)
    log_event(chat_id, "Пользователь нажал кнопку 'Узнать про консультации'", None)

    text = (
        "Когда мы начинаем работу, первое, что мы делаем — аккуратно и подробно разбираем, "
        "как именно проявляются Ваши панические атаки, что их запускает и какие мысли "
        "возникают в момент приступа.\n\n"
        "Далее я предлагаю план терапии, в который обычно входят:\n"
        "• обучение тому, как работает паника и тревога;\n"
        "• специальные упражнения для снижения чувствительности к телесным ощущениям;\n"
        "• постепенное расширение пространства жизни — с выходом из избегания.\n\n"
        "Мы будем идти в таком темпе, который будет для Вас достаточно посильным, "
        "но при этом дающим реальные изменения."
    )

    await bot.send_message(chat_id, text)


async def send_final_block2(chat_id: int):
    upsert_user(chat_id, step="финальный_блок_2")
    log_event(chat_id, "Отправлен финальный блок 2", None)

    text = (
        "С людьми, переживающими панические атаки, я работаю в формате регулярных встреч.\n\n"
        "В начале терапии мы встречаемся чаще, затем — по мере продвижения и stabilизации состояния — "
        "интервалы могут увеличиваться.\n\n"
        "Главная цель — не просто «снять симптомы», а вернуть Вам ощущение, что жизнь снова принадлежит Вам, "
        "а не страху следующего приступа."
    )

    try:
        await bot.send_photo(chat_id, FSInputFile("media/panic_story_photo.jpg"), caption=text)
    except Exception:
        await bot.send_message(chat_id, text)

    # через минуту — сообщение «По итогам прохождения…»
    await smart_sleep(chat_id, prod_seconds=60, test_seconds=10)
    await send_chat_invite(chat_id)


async def send_chat_invite(chat_id: int):
    upsert_user(chat_id, step="приглашение_в_чат")
    log_event(chat_id, "Отправлено приглашение в чат", None)

    if CHAT_USERNAME:
        text = (
            "По итогам прохождения этого пути у многих возникает желание задать уточняющие вопросы или "
            "поделиться опытом.\n\n"
            "У меня есть закрытый чат, где я отвечаю на вопросы и разбираю сложные ситуации.\n\n"
            "Если Вам откликается такой формат — Вы можете присоединиться."
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Перейти в чат",
                        url=f"https://t.me/{CHAT_USERNAME.lstrip('@')}",
                    )
                ]
            ]
        )

        await bot.send_message(chat_id, text, reply_markup=kb)
    else:
        await bot.send_message(
            chat_id,
            "Если у Вас останутся вопросы, Вы можете написать мне в личные сообщения.",
        )


async def send_final_block3(chat_id: int):
    upsert_user(chat_id, step="финальный_блок_3")
    log_event(chat_id, "Отправлен финальный блок 3", None)

    text = (
        "Вам может казаться, что у Вас нет никаких «мыслей» во время приступа — только чувство ужаса.\n\n"
        "На самом деле за этим чувством почти всегда стоят очень конкретные предположения о том, "
        "что может случиться: потеря контроля, «сойти с ума», умереть от остановки сердца или удушья.\n\n"
        "В терапии мы не убеждаем себя, что «всё будет хорошо», а учимся по-новому относиться к этим "
        "мыслям и ощущениям.\n\n"
        "Это требует времени и определённой смелости, но в итоге позволяет перестать жить в ожидании "
        "очередного приступа и вернуть себе право на нормальную, живую жизнь."
    )

    extra_text = (
        "Если Вы почувствуете, что готовы к изменениям и хотели бы пройти этот путь вместе со специалистом — "
        "Вы можете написать мне, и мы обсудим формат работы.\n\n"
        "Даже если сейчас кажется, что паника полностью управляет Вашей жизнью, это состояние поддаётся "
        "коррекции. Важен первый шаг."
    )

    try:
        await bot.send_message(chat_id, text)
        log_event(chat_id, "Отправлен заключительный блок", None)
    except Exception as e:
        log_event(chat_id, "Ошибка отправки заключительного блока", str(e))

    await smart_sleep(chat_id, prod_seconds=60, test_seconds=10)

    try:
        await bot.send_message(chat_id, extra_text, parse_mode="HTML")
        log_event(chat_id, "Отправлен блок про сомнения в психотерапии", None)
    except Exception as e:
        log_event(chat_id, "Ошибка отправки блока про сомнения в психотерапии", str(e))

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)
    try:
        await bot.send_photo(chat_id, FSInputFile("media/Scrc2798760b2b95377.jpg"))
        await bot.send_photo(chat_id, FSInputFile("media/Scb2b95377.jpg"))
        log_event(chat_id, "Отправлены отзывы в блоке про сомнения", None)
    except Exception as e:
        log_event(chat_id, "Ошибка отправки отзывов в блоке про сомнения", str(e))

    schedule_message(chat_id, prod_seconds=24 * 60 * 60, test_seconds=10, kind="final_block3")


# =========================================================
# 3.2 — ЗАВЕРШЕНИЕ ТЕСТА ПО ТАЙМ-АУТУ
# =========================================================

async def timeout_finish_test(chat_id: int):
    """Принудительное завершение теста, если пользователь завис на вопросе."""
    log_event(chat_id, "Тест избегания завершён по таймауту", None)

    # Подстрахуемся и удалим незавершённый тайм-аут, если он всё ещё есть
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
        (chat_id, "avoidance_timeout"),
    )
    conn.commit()
    conn.close()

    # Переход к истории пациентки как к следующему логическому шагу
    await send_case_story(chat_id, payload=None)


# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    init_db()
    asyncio.create_task(scheduler_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
