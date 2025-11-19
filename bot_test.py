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
    FSInputFile
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
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "0") or 0)
SCHEDULER_POLL_INTERVAL = int(os.getenv("SCHEDULER_POLL_INTERVAL", "10"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

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


def log_event(user_id: int, action: str, details: str = None):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO events (user_id, timestamp, action, details) VALUES (?, ?, ?, ?)",
        (user_id, datetime.now().isoformat(timespec='seconds'), action, details)
    )
    conn.commit()
    conn.close()


def upsert_user(user_id: int, step: str = None, subscribed: int = None, username: str = None):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    exists = cursor.fetchone()
    now = datetime.now().isoformat(timespec='seconds')

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
            (user_id, "unknown", step or "start", subscribed or 0, now, username)
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
    fast_user_raw = os.getenv("FAST_USER_ID")
    FAST_USER_ID = int(fast_user_raw) if fastisdigit := fast_user_raw and fast_user_raw.isdigit() else None
    return FAST_USER_ID and user_id == FAST_USER_ID


async def smart_sleep(user_id: int, prod_seconds: int, test_seconds: int = 3):
    await asyncio.sleep(test_seconds if is_fast_user(user_id) else prod_seconds)


def schedule_message(user_id: int, prod_seconds: int, test_seconds: int, kind: str, payload: str = None):
    delay = test_seconds if is_fast_user(user_id) else prod_seconds
    send_at = datetime.now() + timedelta(seconds=delay)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM scheduled_messages WHERE user_id=? AND kind=? AND delivered=0",
                   (user_id, kind))

    cursor.execute(
        "INSERT INTO scheduled_messages (user_id, send_at, kind, payload, delivered) VALUES (?, ?, ?, ?, 0)",
        (user_id, send_at.isoformat(timespec='seconds'), kind, payload)
    )
    conn.commit()
    conn.close()

def mark_message_delivered(task_id: int):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_messages SET delivered=1 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


init_db()

# =========================================================
# /start — одна кнопка «📘 Открыть PDF»
# =========================================================
@router.message(F.text == "/start")
async def cmd_start(message: Message):
    user_id = message.from_user.id
    uname = (message.from_user.username or "").strip() or None

    test_ids_raw = os.getenv("TEST_USER_IDS", "")
    TEST_USER_IDS = {int(x) for x in test_ids_raw.split(",") if x.strip().isdigit()} if test_ids_raw else set()
    purge_flag = os.getenv("PURGE_TEST_USERS_ON_START", "false").lower() == "true"

    if purge_flag and user_id in TEST_USER_IDS:
        purge_user(user_id)
        log_event(user_id, "purge_on_start", "Тестовый пользователь очищен")

    upsert_user(user_id, step="start", username=uname)
    log_event(user_id, "user_start", "Пользователь запустил бота")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📘 Открыть PDF", callback_data="open_pdf")]
        ]
    )

    await message.answer(
        """Если Вы зашли в этот бот, значит, Ваши тревоги уже успели сильно вмешаться в жизнь.\n 
• Частое сердцебиение 💓 \n• потемнение в глазах 🌘 \n• головокружение🌀 \n• пот по спине😰 \n• страх потерять рассудок...\n
Вы стараетесь взять себя в руки, но чем сильнее пытаетесь успокоиться — тем страшнее становится. 
Анализы крови, обследования сердца и сосудов показывают, что всё в норме. Но наплывы ужаса продолжают догонять Вас.\n\n
Знакомо? 

Вероятно, Вы уже знаете, что такие наплывы страха называются <b>паническими атаками</b>.
Многие люди месяцами ищут причину этих приступов — и всё равно не могут понять, почему паника возвращается. 
Я покажу, как ослабить её власть и перестать ждать нового приступа каждый день\n \n  
Эти состояния имеют чёткую внутреннюю закономерность — и когда Вы поймёте её, Вы сможете взять происходящее под контроль 🛥

Я приготовил материал, который поможет Вам разобраться, что запускает панические атаки, чем они поддерживаются и как наконец вернуться к расслабленной жизни.  
Скачайте его — и дайте отпор страху!""",
        parse_mode="HTML",
        reply_markup=kb
    )

# =========================================================
# 2. ЕДИНЫЙ БЛОК «📘 ОТКРЫТЬ PDF» — кружок + PDF + планировщики + fallback
# =========================================================

@router.callback_query(F.data == "open_pdf")
async def unified_open_pdf(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    uname = (callback.from_user.username or "").strip() or None

    await callback.answer()

    upsert_user(chat_id, step="got_material", username=uname)
    log_event(chat_id, "user_clicked_get_material", "Нажал единую кнопку «Открыть PDF»")
    log_event(chat_id, "user_opened_pdf", "Открыл PDF через единую кнопку")

    if VIDEO_NOTE_FILE_ID:
        try:
            await bot.send_chat_action(chat_id, "upload_video_note")
            await bot.send_video_note(chat_id, VIDEO_NOTE_FILE_ID)
            await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Не удалось отправить кружок: {e}")

    if LINK and os.path.exists(LINK):
        await bot.send_document(
            chat_id,
            FSInputFile(LINK, filename="Выход из панического круга.pdf"),
            caption="Вот Ваш первый шаг к спокойствию 🧘🏻‍♀️"
        )
    elif LINK and LINK.startswith("http"):
        await bot.send_message(chat_id, f"📘 Ваш материал доступен по ссылке: {LINK}")
    else:
        await bot.send_message(chat_id, "⚠️ Файл не найден. Попробуйте позже.")

    schedule_message(
        user_id=chat_id,
        prod_seconds=20 * 60,
        test_seconds=5,
        kind="channel_invite"
    )

    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=5,
        kind="avoidance_intro"
    )

    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        test_seconds=30,
        kind="avoidance_fallback"
    )
# =========================================================
# 4. ОПРОС ПО ИЗБЕГАНИЮ
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
        "Давайте проверим, насколько ваши привычки действительно помогают, а где — мешают?\n\n "
        "Пройдите короткий тест — всего 8 вопросов с ответами Да/Нет 🗳"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Начать тест", callback_data="avoidance_start")]]
    )
    await bot.send_message(chat_id, text, reply_markup=kb)
    log_event(chat_id, "bot_avoidance_invite_sent", "Предложен опрос избегания")


@router.callback_query(F.data == "avoidance_start")
async def start_avoidance_test(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()

    # удаляем старые ответы, если тест уже проходился раньше, и отменяем fallback
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM answers WHERE user_id=?", (chat_id,))
    cursor.execute(
        "DELETE FROM scheduled_messages WHERE user_id=? AND kind='avoidance_fallback' AND delivered=0",
        (chat_id,)
    )
    conn.commit()
    conn.close()

    # фиксируем этап и логируем
    upsert_user(chat_id, step="avoidance_test")
    log_event(chat_id, "user_clicked_avoidance_start", "Начал опрос избегания")

    # удаляем кнопку "Начать тест", чтобы она исчезла навсегда
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # начинаем тест
    await bot.send_message(
        chat_id,
        "Сейчас я дам Вам несколько вопросов про то, как Вы ведёте себя в ситуации страха.\n"
        "Пожалуйста, отвечайте честно — здесь нет правильных или неправильных ответов."
    )
    log_event(chat_id, "avoidance_test", "Тест избегания запущен")

    await send_question(chat_id, 0)


async def send_question(chat_id: int, index: int):
    if index >= len(avoidance_questions):
        await finish_test(chat_id)
        return
    q = avoidance_questions[index]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Да", callback_data=f"ans_yes_{index}"),
            InlineKeyboardButton(text="Нет", callback_data=f"ans_no_{index}")
        ]
    ])
    await bot.send_message(chat_id, f"{index + 1}. {q}", reply_markup=kb)


@router.callback_query(F.data.startswith("ans_yes_"))
async def handle_yes(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()
    index_str = callback.data.split("_")[-1]
    try:
        index = int(index_str)
    except ValueError:
        return

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO answers (user_id, question, answer) VALUES (?, ?, ?)",
        (chat_id, index, "yes")
    )
    conn.commit()
    conn.close()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_question(chat_id, index + 1)


@router.callback_query(F.data.startswith("ans_no_"))
async def handle_no(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    await callback.answer()
    index_str = callback.data.split("_")[-1]
    try:
        index = int(index_str)
    except ValueError:
        return

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO answers (user_id, question, answer) VALUES (?, ?, ?)",
        (chat_id, index, "no")
    )
    conn.commit()
    conn.close()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await send_question(chat_id, index + 1)


async def finish_test(chat_id: int):
    # собираем ответы
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT answer FROM answers WHERE user_id=?", (chat_id,))
    rows = cursor.fetchall()
    conn.close()

    yes_count = sum(1 for (ans,) in rows if ans == "yes")

    upsert_user(chat_id, step="avoidance_done")
    log_event(chat_id, "user_finished_test", f"Ответов 'ДА': {yes_count}")

    chain = (
        "Чем больше вынужденных ограничений мы накладываем на свою жизнь\n"
        "️⬇️\nтем большую важность мы придаём панике\n"
        "⬇️\nТем больше концентрируемся на своём теле\n"
        "⬇️\nТем больше чувствуем в нём неожиданные/неприятные ощущения\n"
        "⬇️\nТем больше переживаем по поводу них.\n\nИ так до бесконечности 🔄"
    )

    # 6. "Тест завершён" — сразу
    await bot.send_message(chat_id, "Тест завершён. Подождите секунду, обрабатываем результаты ⏳")
    await smart_sleep(chat_id, prod_seconds=3, test_seconds=1)  # 7. "Судя по Вашим ответам" — через 3 секунды

    if yes_count >= 4:
        part1 = (
            "Судя по Вашим ответам, Вам приходится довольн"
            "о сильно подстраивать свою жизнь под "
            "<b><i>избегание</i></b> возможных повторных приступ...о ловушка, в которую попадаются очень многие люди 🪤\n\n" + chain
        )
        part2 = (
            "☀️ Хорошая новость в том, что мы в силах менять стр...ию своих действий — и тем самым разрывать этот порочный круг.\n"
            "Если тревога долгое время диктовала правила, естест...траху будут ощущаться как последнее, чем захочется заниматься. "
            "Кажется, будто без этих «страхующих» привычек станет невыносимо дискомфортно. "
            "Но каждый раз, когда мы не убегаем, а остаёмся в пу...лучает новый опыт — что <i>опасность была преувеличена</i>.\n\n"
            "Вы уже почитали в моём гайде о том, как правильно отвечать себе на пугающие <u>мысли</u>. "
            "Поэтому теперь, держа под рукой эту памятку, Вы можете и в своих <u>действиях</u>"
            "выходить из замкнутого круга тревоги.\n\n"
            "Именно так и работают современные методы терапии панических приступов — "
            "через постепенное, выверенное столкновение со страхами и пересмотр привычных реакций.\n\n"
            "Я рядом, и готов помочь Вам пройти этот путь."
        )
    else:
        part1 = (
            "Судя по Вашим ответам, Вы почти не подстраиваете жизнь под избегание паники — "
            "и это отличный знак! Это означает, что многие способы, которыми Вы реагируете на страх, "
            "уже помогают Вам не усиливать его.\n\n" + chain
        )
        part2 = (
            "Тем не менее, даже если избегание выражено слабо, тревога может сохраняться — "
            "особенно если пугающие ощущения появляются внезапно.\n\n"
            "То, что помогает окончательно закрыть эту тему — это научиться правильно отвечать себе "
            "на внезапные пугающие мысли, которые мелькают во время приступа.\n\n"
            "В гайде Вы уже видели примеры таких ответов. "
            "Если Вы будете регулярно применять их в нужный момент, приступы постепенно утратят силу."
        )

    # отправляем интерпретацию
    await bot.send_message(chat_id, part1, parse_mode="HTML")
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)
    await bot.send_message(chat_id, part2, parse_mode="HTML")

    # призыв посмотреть условия консультации
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Узнать про консультации",
                    callback_data="open_consult"
                )
            ]
        ]
    )
    await bot.send_message(chat_id, "Если хотите, расскажу, как проходят консультации.", reply_markup=kb)


# =========================================================
# Fallback: если юзер НЕ начал тест → продолжаем цепочку
# =========================================================
async def avoidance_fallback(chat_id: int):
    """
    Если пользователь НЕ нажал «Начать тест» в течение суток (или 30 сек в тестовом режиме),
    продолжаем воронку как будто тест завершён.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT step FROM users WHERE user_id=?", (chat_id,))
    row = cursor.fetchone()
    conn.close()

    user_step = row[0] if row else None

    # если тест начат или завершён — ничего не делаем
    if user_step in ("avoidance_test", "avoidance_done"):
        return

    await send_case_story(chat_id)


# =========================================================
# 5. БЛОК ПОСЛЕ ТЕСТА — “Чтобы ослабить власть тревоги…”
# =========================================================
async def send_case_story(chat_id: int):
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    text = (
        "Чтобы ослабить власть тревоги и уменьшить силу приступов, важно не только знать, "
        "что происходит с телом во время паники, но и начать менять своё поведение.\n\n"
        "Давайте расскажу историю одной моей пациентки."
    )
    await bot.send_message(chat_id, text)

    # Следующий блок через сутки
    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        kind="final_block1",
        test_seconds=5
    )


# =========================================================
# 6. Финальные блоки сообщений
# =========================================================
async def send_final_message(chat_id: int):
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    text = (
        "С людьми, переживающими панические атаки, я работаю каждый день, "
        "и я хорошо знаю, как важно не откладывать обращение за помощью. "
        "Потому что со временем тревога перестаёт быть лишь реакцией на стресс и начинает определять Ваш образ мыслей и восприятия.\n\n"
        "<b>Как я могу помочь Вам?</b>\n\n"
        "На индивидуальных консультациях мы можем вместе разобраться, из чего складывается <i>именно Ваш цикл тревоги</i>: "
        "какие мысли, телесные реакции и привычные способы поведения поддерживают его. Мы составим для Вас подробный план действий: "
        "от списка необходимых обследований - до распорядка упраженений по преодолению страха.\n\n"
        "По итогам прохождения психотерапии Вы сможете не только сократить количество приступов, "
        "но и полностью вернуть себе качество жизни."
    )

    photo = FSInputFile("media/DSC03503.jpg")
    await bot.send_photo(chat_id, photo=photo, caption=text, parse_mode="HTML")

    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        kind="final_block2",
        test_seconds=5
    )


async def send_final_block2(chat_id: int):
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    extra_text = (
        "<b>Одно из самых частых сомнений у тех, кто задумывается о психотерапии, — «А мне это точно поможет?»</b>\n\n"
        "Это абсолютно понятный вопрос, особенно если панические атаки длятся уже долго, а прошлые попытки справиться не дали ощутимого эффекта. "
        "Но психотерапия — это не абстрактные разговоры, а детально просчитанная точечная работа по изменению Вашего способа реагирования на страх "
        "и восприятия своих телесных ощущений.\n\n"
        "Иногда люди могут смотреть на эффект от противодействия проблеме как на черно-белые варианты: либо выздоровею, либо нет. "
        "На самом деле процесс освобождения от тревоги в чем-то похож на занятие физкультурой: можно стать мастером спорта, если задаться такой целью, "
        "но даже просто обретение хорошей физической формы - это отличный результат.\n\n"
        "Могу Вам гарантировать, что любой человек, который получает на занятиях со специалистом новые знания и начинает действовать в соответствии с ними — "
        "чувствует результат уже с первых недель.\n\n"
        "Вот что часто говорят мои клиенты после нескольких занятий:"
    )
    await bot.send_message(chat_id, extra_text, parse_mode="HTML")

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)
    extra_photo1 = FSInputFile("media/Scrc2798760b2b95377.jpg")
    await bot.send_photo(chat_id, photo=extra_photo1)

    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)
    extra_photo2 = FSInputFile("media/Scb2b95377.jpg")
    await bot.send_photo(chat_id, photo=extra_photo2)

    schedule_message(
        user_id=chat_id,
        prod_seconds=24 * 60 * 60,
        kind="final_block3",
        test_seconds=5
    )


async def send_final_block3(chat_id: int):
    await smart_sleep(chat_id, prod_seconds=1, test_seconds=1)

    thoughts_text = (
        "<b>Вам может казаться, что у Вас нет никаких мыслей во время панической атаки.</b>\n\n"
        "Может складываться впечатление, что страх просто наваливается сам по себе: "
        "«Я ничего не успеваю подумать — и сразу соскальзываю в поток из ужасных ощущений». "
        "Дальше приходится думать лишь про то, как \"спастись\" "
        "(беру это слово в кавычки - потому что никак спасаться от панической атаки конечно же не надо).\n\n"
        "Но если прислушаться внимательнее, оказывается, что даже на пике страха, "
        "сквозь затуманенный рассудок внутри постоянно мелькают короткие разорванные фразы:\n\n"
        "<i>«Это опасно»</i>\n"
        "<i>«Я сейчас упаду»</i>\n"
        "<i>«Что-то не так с сердцем»</i>\n\n"
        "Эти обрывочные мысли, проносясь сквозь сознание на реактивной скорости, "
        "могут оставаться не замеченными Вами, но они оставляют за собой испепеляющий эмоциональный хвост ☄️\n\n"
        "И вот одна из основных причин, почему у Вас может не получаться справиться с паникой: "
        "Вы можете знать, что паническая атака не опасна, но не даёте <b>ответа на конкретную мысль</b>. "
        "Вместо этого начинаете искать спасение — измерять давление, глубоко дышать, открывать окно — "
        "вместо того, чтобы понять, какая именно идея вызвала тревогу.\n\n"
        "Вам требуется распознать их и давать себе на них чёткие адресные ответы 🎯"
        "Недостаточно «в целом знать», что паника не причиняет вреда — "
        "важно распознать конкретный страх, лежащий в основе приступа.\n\n"
        "На психотерапевтических сеансах мы проводим буквально археологические раскопки "
        "в отношении внутреннего опыта: слой за слоем убираем общие формулировки "
        "(«когда это кончится?», «что со мной?», «я не справлюсь»), "
        "пока не обнаружим само ядро страха. Например, "
        "<i>«я боюсь упасть в обморок», «я задохнусь, если перестану следить за дыханием»</i>.\n\n"
        "И только тогда можно дать точный ответ, который нейтрализует страх:\n"
        "<i>«Я не могу потерять сознание, потому что при панике давление повышено, а не понижено»</i>\n"
        "<i>«Дыхание не нужно контролировать, потому что я не смогу перестать дышать — даже если бы захотел»</i>\n\n"
        "Вот в этот момент контроль над происходящим вновь возвращается Вам. "
        "Адреналин еще сохраняется в теле, но уже перестаёт затмевать разум."
    )

    await bot.send_message(chat_id, thoughts_text, parse_mode="HTML")

    upsert_user(chat_id, step="final_message_sent")
    log_event(chat_id, "bot_final_message_sent", "Отправлена завершающая серия сообщений")


# =========================================================
# 7. CTA — Узнать про консультации → Перейти на сайт
# =========================================================
@router.callback_query(F.data == "open_consult")
async def open_consult(callback: CallbackQuery):
    chat_id = callback.message.chat.id

    log_event(chat_id, "user_clicked_consult_cta", "Переход к консультациям")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перейти на страницу консультаций",
                    url="https://лечение-паники.рф/консультации"
                )
            ]
        ]
    )

    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass

    await callback.answer()


# =========================================================
# 8. Запуск
# =========================================================
async def main():
    logger.info(f"Бот запущен. MODE={MODE}, TEST_USER_ID={TEST_USER_ID or '—'}")
    await asyncio.gather(
        dp.start_polling(bot),
        scheduler_worker(),
    )

if __name__ == "__main__":
    asyncio.run(main())
