import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

load_dotenv("/home/dmitry/calmwaybot_test/.env")

DB_PATH = os.getenv("DATABASE_PATH")
if not DB_PATH:
    raise RuntimeError("DATABASE_PATH is not set in .env")

app = FastAPI(title="CalmWayBot — Admin Panel")

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
h1 { color: var(--accent); margin-bottom: 20px; }
table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 15px;
    background-color: var(--table-bg);
}
th, td {
    border: 1px solid var(--table-border);
    padding: 10px;
}
th {
    background-color: var(--table-header-bg);
    color: var(--accent);
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
}
button:hover { background-color: var(--accent-hover); }
</style>
"""

def connect():
    return sqlite3.connect(DB_PATH)

def ensure_schema():
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                source TEXT,
                step TEXT,
                subscribed INTEGER DEFAULT 0,
                last_action TEXT,
                username TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                timestamp TEXT,
                action TEXT,
                details TEXT
            )
        """)

ensure_schema()

def fmt(ts):
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except:
        return ts

def has_consult_interest(user_id: int) -> bool:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT action, details FROM events WHERE user_id=?", (user_id,))
        for action, details in cur.fetchall():
            if "консультац" in f"{action} {details}".lower():
                return True
    return False

@app.get("/panel-database", response_class=HTMLResponse)
async def panel():
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, source, step, subscribed, last_action, username
            FROM users ORDER BY last_action DESC
        """)
        users = cur.fetchall()

    rows = ""
    for u in users:
        uid, source, step, sub, last, username = u
        rows += f"""
        <tr>
            <td>@{username or uid}</td>
            <td>{source}</td>
            <td>{step}</td>
            <td>{"✅" if sub else "—"}</td>
            <td>{"✅" if has_consult_interest(uid) else "—"}</td>
            <td>{fmt(last)}</td>
            <td><a href="/panel-database/user/{uid}"><button>История</button></a></td>
        </tr>
        """

    return f"""
    {STYLE}
    <h1>CalmWayBot — Users</h1>
    <table>
        <tr>
            <th>Пользователь</th><th>Источник</th><th>Этап</th>
            <th>Подписан</th><th>Интерес</th><th>Последнее</th><th></th>
        </tr>
        {rows}
    </table>
    <script>setTimeout(()=>location.reload(),10000)</script>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("admin_panel:app", host="0.0.0.0", port=8080)
