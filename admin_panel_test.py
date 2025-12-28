import os
import sqlite3
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

if load_dotenv is not None:
    load_dotenv(dotenv_path=ENV_PATH, override=False)

DB_PATH = os.getenv("DATABASE_PATH") or os.getenv("DB_PATH") or os.path.join(BASE_DIR, "users.db")

app = FastAPI(title="CalmWayBot Test - Admin Panel")

STYLE = """
<style>
:root {
    --bg: #ffffff;
    --fg: #000000;
    --table-bg: #f4f4f4;
    --table-border: #d0d0d0;
    --table-header-bg: #e9e9e9;
    --accent: #0077cc;
    --accent-hover: #005fa3;
}

@media (prefers-color-scheme: dark) {
    :root {
        --bg: #0d1117;
        --fg: #e6edf3;
        --table-bg: #161b22;
        --table-border: #30363d;
        --table-header-bg: #21262d;
        --accent: #238636;
        --accent-hover: #2ea043;
    }
}

body {
    background-color: var(--bg);
    color: var(--fg);
    font-family: Arial, sans-serif;
    margin: 0;
    padding: 20px;
}

h1 {
    color: var(--accent);
    margin-bottom: 10px;
}

.small-note {
    opacity: 0.75;
    font-size: 12px;
    margin-bottom: 18px;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 15px;
    background-color: var(--table-bg);
    border-radius: 6px;
    overflow: hidden;
}

th, td {
    border: 1px solid var(--table-border);
    padding: 10px;
    text-align: left;
}

th {
    background-color: var(--table-header-bg);
    color: var(--accent);
    font-weight: bold;
}

tr:hover {
    background-color: var(--accent-hover);
    color: white;
}

button {
    background-color: var(--accent);
    color: white;
    border: none;
    padding: 8px 14px;
    border-radius: 6px;
    cursor: pointer;
}

button:hover {
    background-color: var(--accent-hover);
}

a {
    color: var(--accent);
}

.summary-block {
    margin-top: 16px;
    padding: 16px;
    background-color: var(--table-bg);
    border: 1px solid var(--table-border);
    border-radius: 8px;
}

.summary-block h2 {
    margin: 0 0 12px 0;
    color: var(--accent);
}

.summary-list {
    margin: 0;
    padding-left: 18px;
}

.summary-list li {
    margin-bottom: 8px;
}

.custom-period {
    margin-top: 16px;
}

.custom-period form {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: flex-end;
    margin-bottom: 10px;
}

.custom-period label {
    display: flex;
    flex-direction: column;
    font-size: 12px;
    gap: 4px;
}
</style>
"""

def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

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
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            timestamp TEXT,
            action TEXT,
            details TEXT
        )
    """)

    conn.commit()
    conn.close()

ensure_schema()

def fmt_time(ts: str) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts

def get_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, source, step, subscribed, last_action, username
        FROM users
        ORDER BY last_action DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

def has_consult_interest(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT action, details
        FROM events
        WHERE user_id = ?
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    for action, details in rows:
        text = f"{action} {details}".lower()
        if "консультац" in text:
            return True

    return False

def get_time_column() -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(users)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    if "created_at" in columns:
        return "created_at"
    return "last_action"

def build_source_filter(source: str) -> tuple[str, list]:
    if source == "unknown":
        return "(source IS NULL OR source = '' OR source = 'unknown')", []
    if source in {"telegram", "telegram-channel"}:
        return "source IN (?, ?)", ["telegram", "telegram-channel"]
    if source:
        return "source = ?", [source]
    return "1=1", []

def get_new_users_stats(start_at: datetime, end_at: datetime, source: str) -> tuple[int, int]:
    time_column = get_time_column()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    source_clause, params = build_source_filter(source)
    params = params + [start_at.isoformat(), end_at.isoformat()]

    cursor.execute(
        f"""
        SELECT COUNT(*), COALESCE(SUM(subscribed), 0)
        FROM users
        WHERE {source_clause}
          AND {time_column} >= ?
          AND {time_column} < ?
        """,
        params,
    )
    total, subscribed = cursor.fetchone()
    conn.close()
    return int(total or 0), int(subscribed or 0)

@app.get("/panel-database", response_class=HTMLResponse)
@app.get("/panel-database-test", response_class=HTMLResponse)
async def panel_main(date_from: str = "", date_to: str = "", source: str = ""):
    users = get_users()

    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=6)
    month_start = today_start - timedelta(days=29)

    yandex_source = "yandex_direct"
    today_total, today_subscribed = get_new_users_stats(today_start, now, yandex_source)
    week_total, week_subscribed = get_new_users_stats(week_start, now, yandex_source)
    month_total, month_subscribed = get_new_users_stats(month_start, now, yandex_source)

    custom_message = ""
    custom_error = ""
    if date_from and date_to and source:
        try:
            start_date = datetime.fromisoformat(date_from)
            end_date = datetime.fromisoformat(date_to) + timedelta(days=1)
            if end_date <= start_date:
                raise ValueError("Некорректный диапазон")
            custom_total, custom_subscribed = get_new_users_stats(start_date, end_date, source)
            source_label_map = {
                "unknown": "unknown",
                "telegram": "telegram",
                "telegram-channel": "telegram-channel",
                "yandex_direct": "yandex_direct",
            }
            source_label = source_label_map.get(source, source)
            custom_message = (
                "Новые пользователи за период "
                f"{date_from}-{date_to} из источника {source_label}: "
                f"{custom_total}, из них подписались: {custom_subscribed}"
            )
        except Exception:
            custom_error = "Не удалось обработать выбранный период. Проверьте даты."

    rows_html = ""
    for user_id, source_value, step, subscribed, last_action, username in users:
        subscribed_mark = "✅" if subscribed else "-"
        consult_mark = "✅" if has_consult_interest(user_id) else "-"
        display_name = f"@{username}" if username else str(user_id)
        last_action_fmt = fmt_time(last_action)

        rows_html += f"""
        <tr>
            <td>{display_name}</td>
            <td>{source_value}</td>
            <td>{step}</td>
            <td>{subscribed_mark}</td>
            <td>{consult_mark}</td>
            <td>{last_action_fmt}</td>
            <td><a href="/panel-database/user/{user_id}"><button>История</button></a></td>
        </tr>
        """

    html = f"""
    {STYLE}
    <h1>CalmWayBot Test - Users</h1>
    <div class="small-note">DB: {DB_PATH}</div>

    <div class="summary-block">
        <h2>Новые пользователи по источнику</h2>
        <ul class="summary-list">
            <li>Новые пользователи из Яндекс Директ за сегодня: {today_total}, из них подписались: {today_subscribed}</li>
            <li>Новые пользователи из Яндекс Директ за неделю: {week_total}, из них подписались: {week_subscribed}</li>
            <li>Новые пользователи из Яндекс Директ за месяц: {month_total}, из них подписались: {month_subscribed}</li>
        </ul>

        <div class="custom-period">
            <strong>Произвольный период</strong>
            <form method="get">
                <label>
                    Начало
                    <input type="date" name="date_from" value="{date_from}" required>
                </label>
                <label>
                    Конец
                    <input type="date" name="date_to" value="{date_to}" required>
                </label>
                <label>
                    Источник
                    <select name="source" required>
                        <option value="" {'selected' if not source else ''}>Выберите источник</option>
                        <option value="unknown" {'selected' if source == 'unknown' else ''}>unknown</option>
                        <option value="telegram" {'selected' if source == 'telegram' else ''}>telegram</option>
                        <option value="telegram-channel" {'selected' if source == 'telegram-channel' else ''}>telegram-channel</option>
                        <option value="yandex_direct" {'selected' if source == 'yandex_direct' else ''}>yandex_direct</option>
                    </select>
                </label>
                <button type="submit">Показать</button>
            </form>
            {f"<div class='small-note'>{custom_error}</div>" if custom_error else ""}
            {f"<div>{custom_message}</div>" if custom_message else ""}
        </div>
    </div>

    <table>
        <tr>
            <th>Пользователь</th>
            <th>Источник</th>
            <th>Этап</th>
            <th>Подписан</th>
            <th>Интерес к консультации</th>
            <th>Последнее действие</th>
            <th></th>
        </tr>
        {rows_html}
    </table>

    <script>
        setTimeout(() => location.reload(), 10000);
    </script>
    """

    return html

@app.get("/panel-database/user/{user_id}", response_class=HTMLResponse)
async def user_history(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT timestamp, action, details
        FROM events
        WHERE user_id=?
        ORDER BY id ASC
    """, (user_id,))
    events = cursor.fetchall()
    conn.close()

    if not events:
        rows = "<tr><td colspan='3'>Нет записей</td></tr>"
    else:
        rows = "".join(
            f"<tr><td>{fmt_time(ts)}</td><td>{action}</td><td>{details or '-'}</td></tr>"
            for ts, action, details in events
        )

    html = f"""
    {STYLE}
    <h1>История действий - {user_id}</h1>
    <div class="small-note">DB: {DB_PATH}</div>
    <a href="/panel-database">Назад</a>

    <table>
        <tr><th>Время</th><th>Действие</th><th>Детали</th></tr>
        {rows}
    </table>
    """

    return html

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("admin_panel_test:app", host="0.0.0.0", port=8081)
