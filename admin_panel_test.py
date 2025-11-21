import os
import sqlite3
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DB_PATH = "/home/dmitry/calmwaybot_test/users_test.db"

app = FastAPI(title="CalmWayBot TEST Admin Panel")

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
    margin-bottom: 20px;
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
            username TEXT,
            consult_interested INTEGER DEFAULT 0
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
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d – %H:%M")
    except:
        return ts

def get_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, source, step, subscribed, consult_interested, last_action, username
        FROM users
        ORDER BY last_action DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

@app.get("/panel-database-test", response_class=HTMLResponse)
async def panel_main():
    users = get_users()

    rows_html = ""
    for user_id, source, step, subscribed, consult_interested, last_action, username in users:
        subscribed_mark = "✅" if subscribed else "—"
        interest_mark = "✅" if consult_interested else "—"
        display_name = f"@{username}" if username else str(user_id)
        last_action_fmt = fmt_time(last_action)

        rows_html += f"""
        <tr>
            <td>{display_name}</td>
            <td>{source}</td>
            <td>{step}</td>
            <td>{subscribed_mark}</td>
            <td>{interest_mark}</td>
            <td>{last_action_fmt}</td>
            <td><a href="/panel-database-test/user/{user_id}"><button>История</button></a></td>
        </tr>
        """

    html = f"""
    {STYLE}
    <h1>CalmWayBot TEST — Users</h1>

    <table>
        <tr>
            <th>Пользователь</th>
            <th>Источник</th>
            <th>Этап</th>
            <th>Подписан</th>
            <th>Интересовался консультацией</th>
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

@app.get("/panel-database-test/user/{user_id}", response_class=HTMLResponse)
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
    <h1>История действий — {user_id}</h1>
    <a href="/panel-database-test">⬅ Назад</a>

    <table>
        <tr>
            <th>Время</th>
            <th>Действие</th>
            <th>Детали</th>
        </tr>
        {rows}
    </table>
    """

    return html

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("admin_panel_test:app", host="0.0.0.0", port=8081)
