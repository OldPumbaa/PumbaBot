from fastapi import FastAPI, Request, Form, HTTPException, Depends, File, UploadFile, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import socketio
import sqlite3
from datetime import datetime, timedelta
import asyncio
import logging
from dotenv import load_dotenv
import os
import pytz
import time
import re
import secrets
import hashlib
import hmac
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, User

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)

astana_tz = pytz.timezone('Asia/Almaty')

load_dotenv()
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

app = FastAPI()

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
app.mount("/socket.io", socketio.ASGIApp(sio))

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/Uploads", StaticFiles(directory="Uploads"), name="uploads")

message_queue = asyncio.Queue()
loop = None

BOT_TOKEN = os.getenv("BOT_TOKEN")
NOTIFICATION_CHAT_ID = os.getenv("NOTIFICATION_CHAT_ID")
NOTIFICATION_TOPIC_ID = os.getenv("NOTIFICATION_TOPIC_ID")

bot = Bot(token=BOT_TOKEN)

def set_event_loop(event_loop):
    global loop
    loop = event_loop
    logging.debug(f"Установлен цикл событий: {loop}")

db_lock = asyncio.Lock()

def generate_session_token():
    return secrets.token_urlsafe(32)

def datetimeformat(value):
    if not value:
        return ""
    astana_tz = pytz.timezone('Asia/Almaty')
    dt = datetime.fromisoformat(value)
    return dt.astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S')

templates.env.filters['datetimeformat'] = datetimeformat

def shorten_filename(filename):
    if not filename:
        return 'unknown'
    if len(filename) > 20:
        return filename[:17] + '...'
    return filename

templates.env.filters['shortenFilename'] = shorten_filename

def verify_telegram_auth(data: dict, bot_token: str) -> bool:
    received_hash = data.pop("hash", None)
    if not received_hash:
        logging.error("Отсутствует hash в Telegram данных")
        return False
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if v)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    logging.debug(f"Проверка подписи: data={data}, data_check_string={data_check_string}, computed_hash={computed_hash}, received_hash={received_hash}")
    return computed_hash == received_hash

async def get_current_user(request: Request):
    session_token = request.cookies.get("session_token")
    if not session_token:
        logging.error("Отсутствует session_token в куки")
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT telegram_id, expires_at FROM sessions WHERE session_token = ?",
        (session_token,)
    )
    session = cursor.fetchone()
    if not session:
        conn.close()
        logging.error("Недействительный session_token")
        raise HTTPException(status_code=401, detail="Invalid session")
    
    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.utcnow() > expires_at:
        cursor.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
        conn.commit()
        conn.close()
        logging.error("Session_token истёк")
        raise HTTPException(status_code=401, detail="Session expired")
    
    telegram_id = session["telegram_id"]
    cursor.execute("SELECT login, is_admin FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    if not employee:
        conn.close()
        logging.error(f"Сотрудник с telegram_id={telegram_id} не найден")
        raise HTTPException(status_code=403, detail="Not authorized")
    if not employee["is_admin"]:
        conn.close()
        logging.error(f"Сотрудник с telegram_id={telegram_id} не является администратором")
        raise HTTPException(status_code=403, detail="Not authorized")
    
    new_expires_at = (datetime.utcnow() + timedelta(days=30)).isoformat()
    cursor.execute(
        "UPDATE sessions SET expires_at = ? WHERE session_token = ?",
        (new_expires_at, session_token)
    )
    conn.commit()
    conn.close()
    
    logging.debug(f"Авторизован пользователь: telegram_id={telegram_id}, login={employee['login']}, is_admin={employee['is_admin']}")
    return {"telegram_id": telegram_id, "login": employee['login'], "is_admin": employee['is_admin']}

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, employee: dict = Depends(get_current_user)):
    logging.debug("Запрос к странице настроек /settings")
    settings = {
        "registration_greeting": get_setting("registration_greeting"),
        "new_ticket_response": get_setting("new_ticket_response"),
        "non_working_hours_message": get_setting("non_working_hours_message"),
        "holiday_message": get_setting("holiday_message"),
        "working_hours_start": get_setting("working_hours_start"),
        "working_hours_end": get_setting("working_hours_end"),
        "weekend_days": [int(day) for day in get_setting("weekend_days", "0,6").split(",")],
        "is_holiday": get_setting("is_holiday", "0")
    }
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "employee": employee,
        "settings": settings,
        "BASE_URL": BASE_URL
    })

@app.get("/telegram-auth")
async def telegram_auth(
    id: str = Query(...),
    first_name: str = Query(None),
    last_name: str = Query(None),
    username: str = Query(None),
    photo_url: str = Query(None),
    auth_date: str = Query(...),
    hash: str = Query(...),
):
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logging.error("BOT_TOKEN не установлен в .env")
        raise HTTPException(status_code=500, detail="Server configuration error")
    
    data = {
        "id": id,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "photo_url": photo_url,
        "auth_date": auth_date,
        "hash": hash
    }
    logging.debug(f"Получены Telegram данные: {data}")
    
    if not verify_telegram_auth(data, bot_token):
        logging.error("Неверная подпись Telegram")
        raise HTTPException(status_code=403, detail="Неверная подпись Telegram")
    
    telegram_id = int(id)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT login, is_admin FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    if not employee:
        conn.close()
        logging.error(f"Сотрудник с telegram_id={telegram_id} не найден в базе")
        raise HTTPException(status_code=403, detail="Вы не авторизованы. Попросите администратора добавить ваш Telegram ID.")
    if not employee["is_admin"]:
        conn.close()
        logging.error(f"Сотрудник с telegram_id={telegram_id} не является администратором")
        raise HTTPException(status_code=403, detail="Вы не авторизованы. Попросите администратора добавить ваш Telegram ID.")
    
    session_token = generate_session_token()
    expires_at = (datetime.utcnow() + timedelta(days=30)).isoformat()
    cursor.execute(
        "INSERT INTO sessions (session_token, telegram_id, expires_at) VALUES (?, ?, ?)",
        (session_token, telegram_id, expires_at)
    )
    full_name = " ".join(filter(None, [first_name, last_name])).strip() or f"User {telegram_id}"
    cursor.execute(
        "UPDATE employees SET full_name = ? WHERE telegram_id = ?",
        (full_name, telegram_id)
    )
    conn.commit()
    conn.close()
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=30 * 24 * 60 * 60
    )
    logging.debug(f"Установлен session_token в куки, редирект на /")
    return response

def get_setting(key: str, default: str = None) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = cursor.fetchone()
    conn.close()
    return result["value"] if result else default

def update_setting(key: str, value: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()
    logging.debug(f"Настройка {key} обновлена: {value}")

def get_db_connection():
    conn = sqlite3.connect("support.db", timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def get_unique_filename(filename: str, directory: str = "Uploads"):
    cleaned_filename = re.sub(r'[^\w\-\.]', '_', filename)
    cleaned_filename = re.sub(r'_+', '_', cleaned_filename).strip('_')
    base, ext = os.path.splitext(cleaned_filename)
    counter = 0
    new_filename = cleaned_filename
    while os.path.exists(os.path.join(directory, new_filename)):
        counter += 1
        new_filename = f"{base}_{counter}{ext}"
    logging.debug(f"Сгенерировано уникальное имя файла: {filename} -> {new_filename}")
    return new_filename

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        logging.debug(f"Проверка авторизации для пути: {request.url.path}")
        if request.url.path in ["/login", "/telegram-auth", "/static", "/Uploads"] or request.url.path.startswith(("/static/", "/Uploads/", "/socket.io")):
            logging.debug(f"Путь {request.url.path} не требует авторизации")
            return await call_next(request)
        try:
            employee = await get_current_user(request)
            request.state.employee = employee
            logging.debug(f"Авторизация успешна для telegram_id={employee['telegram_id']}")
            return await call_next(request)
        except HTTPException as e:
            logging.error(f"Ошибка авторизации: {e.detail}")
            return RedirectResponse(url="/login", status_code=303)

app.add_middleware(AuthMiddleware)

async def send_notification_to_topic(ticket_id: int, login: str, message: str, is_reopened: bool = False):
    if not NOTIFICATION_CHAT_ID or not NOTIFICATION_TOPIC_ID:
        logging.warning("NOTIFICATION_CHAT_ID или NOTIFICATION_TOPIC_ID не заданы, уведомление не отправлено")
        return
    try:
        history_text = f"Тикет #{ticket_id} ({login}): {message if not is_reopened else f'Тикет #{ticket_id} открыт повторно'}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Открыть тикет", url=f"{BASE_URL}/ticket/{ticket_id}")
            ]
        ])
        await bot.send_message(
            chat_id=NOTIFICATION_CHAT_ID,
            message_thread_id=int(NOTIFICATION_TOPIC_ID),
            text=history_text,
            reply_markup=keyboard
        )
        logging.info(f"Уведомление отправлено в чат {NOTIFICATION_CHAT_ID}, топик {NOTIFICATION_TOPIC_ID}: Тикет #{ticket_id}")
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления: {str(e)}")
        raise

@app.get("/quickview/{ticket_id}", response_class=HTMLResponse)
async def quickview(
    request: Request,
    ticket_id: int,
    employee: dict = Depends(get_current_user)
):
    logging.debug(f"Запрос к QuickView тикета #{ticket_id}")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
    ticket_data = cursor.fetchone()
    if not ticket_data:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    telegram_id = ticket_data["telegram_id"]

    cursor.execute(
        """
        SELECT m.message_id, m.ticket_id, m.telegram_id, m.text, m.is_from_bot, m.timestamp,
            CASE WHEN m.is_from_bot THEN COALESCE(e2.login, 'Техподдержка') ELSE e.login END AS login
        FROM messages m
        JOIN employees e ON m.telegram_id = e.telegram_id
        LEFT JOIN employees e2 ON m.employee_telegram_id = e2.telegram_id
        WHERE m.ticket_id = ?
        ORDER BY m.timestamp
        """,
        (ticket_id,)
    )
    messages = cursor.fetchall()
    messages_list = []
    astana_tz = pytz.timezone('Asia/Almaty')
    for row in messages:
        cursor.execute(
            """
            SELECT file_path, file_name, file_type
            FROM attachments
            WHERE message_id = ?
            """,
            (row["message_id"],)
        )
        attachments = [
            {"file_path": a["file_path"], "file_name": a["file_name"], "file_type": a["file_type"]}
            for a in cursor.fetchall()
        ]
        messages_list.append({
            "message_id": row["message_id"],
            "ticket_id": row["ticket_id"],
            "telegram_id": row["telegram_id"],
            "text": row["text"],
            "is_from_bot": bool(row["is_from_bot"]),
            "timestamp": datetime.fromisoformat(row["timestamp"]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
            "login": row["login"],
            "attachments": attachments
        })
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee_data = cursor.fetchone()
    login = employee_data[0] if employee_data else "Unknown"
    conn.close()
    return templates.TemplateResponse(
        "quickview.html",
        {
            "request": request,
            "ticket_id": ticket_id,
            "messages": messages_list,
            "login": login,
            "employee": employee
        }
    )

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    logging.debug("Запрос к странице логина")
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/logout")
async def logout(request: Request):
    session_token = request.cookies.get("session_token")
    if session_token:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
        conn.commit()
        conn.close()
        logging.debug(f"Сессия с session_token={session_token} удалена")
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    logging.debug("Запрос на выход, cookie удалены")
    return response

@app.post("/cleanup_sessions")
async def cleanup_sessions():
    conn = get_db_connection()
    cursor = conn.cursor()
    threshold = (datetime.utcnow() - timedelta(days=30)).isoformat()
    cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (threshold,))
    deleted_count = cursor.rowcount
    conn.commit()
    conn.close()
    logging.debug(f"Очищено {deleted_count} устаревших сессий")
    return {"status": "ok", "deleted_count": deleted_count}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, employee: dict = Depends(get_current_user)):
    logging.debug("Запрос к главной странице /")
    async with db_lock:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT t.ticket_id, t.telegram_id, e.login, 
                        m.text AS last_message, m.timestamp AS last_message_timestamp,
                        m.message_id,
                        t.issue_type, t.assigned_to, e2.login AS assigned_login
                    FROM tickets t 
                    JOIN employees e ON t.telegram_id = e.telegram_id 
                    LEFT JOIN (
                        SELECT ticket_id, text, timestamp, message_id, is_from_bot, employee_telegram_id
                        FROM messages
                        WHERE (ticket_id, timestamp) IN (
                            SELECT ticket_id, MAX(timestamp)
                            FROM messages
                            GROUP BY ticket_id
                        )
                    ) m ON t.ticket_id = m.ticket_id
                    LEFT JOIN employees e3 ON m.employee_telegram_id = e3.telegram_id
                    LEFT JOIN employees e2 ON t.assigned_to = e2.telegram_id
                    WHERE t.status = 'open'
                """)
                astana_tz = pytz.timezone('Asia/Almaty')
                tickets = []
                for row in cursor.fetchall():
                    attachments = []
                    if row["message_id"]:
                        try:
                            cursor.execute(
                                "SELECT file_path, file_name, file_type FROM attachments WHERE message_id = ?",
                                (row["message_id"],)
                            )
                            attachments = [{"file_path": a["file_path"], "file_name": a["file_name"], "file_type": a["file_type"]} for a in cursor.fetchall()]
                        except Exception as e:
                            logging.error(f"Ошибка при получении attachments для message_id={row['message_id']}: {e}")
                    tickets.append({
                        "id": row["ticket_id"],
                        "telegram_id": row["telegram_id"],
                        "login": row["login"],
                        "last_message": row["last_message"],
                        "last_message_timestamp": datetime.fromisoformat(row["last_message_timestamp"]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S') if row["last_message_timestamp"] else None,
                        "issue_type": row["issue_type"],
                        "assigned_to": row["assigned_to"],
                        "assigned_login": row["assigned_login"],
                        "attachments": attachments
                    })
                logging.debug(f"Получено тикетов: {len(tickets)}, пример: {tickets[:1]}")
        except Exception as e:
            logging.error(f"Ошибка в обработке SQL для /: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Internal Server Error: SQL Error {str(e)}")

    settings = {
        "registration_greeting": get_setting("registration_greeting", "Добро пожаловать!"),
        "new_ticket_response": get_setting("new_ticket_response", "Обращение принято."),
        "non_working_hours_message": get_setting("non_working_hours_message", "Сейчас нерабочее время."),
        "holiday_message": get_setting("holiday_message", "Сегодня выходной."),
        "working_hours_start": get_setting("working_hours_start", "09:00"),
        "working_hours_end": get_setting("working_hours_end", "18:00"),
        "weekend_days": [int(day) for day in get_setting("weekend_days", "0,6").split(",")],
        "is_holiday": get_setting("is_holiday", "0")
    }
    try:
        response = templates.TemplateResponse("index.html", {
            "request": request,
            "tickets": tickets,
            "employee": employee,
            "settings": settings,
            "BASE_URL": BASE_URL
        })
        logging.debug("Шаблон index.html успешно отрендерен")
        return response
    except Exception as e:
        logging.error(f"Ошибка рендеринга index.html: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal Server Error: Template Error {str(e)}")

@app.post("/save_settings")
async def save_settings(
    request: Request,
    registration_greeting: str = Form(...),
    new_ticket_response: str = Form(...),
    non_working_hours_message: str = Form(...),
    holiday_message: str = Form(...),
    working_hours_start: str = Form(...),
    working_hours_end: str = Form(...),
    weekend_days: list = Form(None),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        update_setting("registration_greeting", registration_greeting)
        update_setting("new_ticket_response", new_ticket_response)
        update_setting("non_working_hours_message", non_working_hours_message)
        update_setting("holiday_message", holiday_message)
        update_setting("working_hours_start", working_hours_start)
        update_setting("working_hours_end", working_hours_end)
        update_setting("weekend_days", ",".join(weekend_days or ["0", "6"]))
        logging.debug("Настройки сохранены")
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logging.error(f"Ошибка при сохранении настроек: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/reset_settings")
async def reset_settings(request: Request, employee: dict = Depends(get_current_user)):
    logging.debug(f"Получен запрос на /reset_settings от telegram_id={employee['telegram_id']}")
    if not employee["is_admin"]:
        logging.error(f"Пользователь telegram_id={employee['telegram_id']} не является администратором")
        raise HTTPException(status_code=403, detail="Not authorized")
    try:
        default_settings = [
            ('registration_greeting', 'Вы можете создавать обращения, отправив сообщение или файл.'),
            ('new_ticket_response', 'Обращение принято. При необходимости прикрепите скриншот или файл с логами.'),
            ('non_working_hours_message', 'Обратите внимание: сейчас нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени.'),
            ('holiday_message', 'Сегодня праздничный день, поэтому ответ может занять больше времени.'),
            ('working_hours_start', '12:00'),
            ('working_hours_end', '23:59'),
            ('weekend_days', '5,6'),
            ('is_holiday', '0')
        ]
        conn = get_db_connection()
        cursor = conn.cursor()
        for key, value in default_settings:
            try:
                cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
                logging.debug(f"Сброшена настройка {key}: {value}")
            except Exception as e:
                logging.error(f"Ошибка при сбросе настройки {key}: {e}")
                raise
        conn.commit()
        conn.close()
        logging.debug("Настройки сброшены до значений по умолчанию")
        return RedirectResponse(url="/settings", status_code=303)
    except Exception as e:
        logging.error(f"Общая ошибка при сбросе настроек: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_holiday")
async def update_holiday(
    request: Request,
    data: dict = Body(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        is_holiday = data.get("is_holiday")
        if is_holiday not in ["0", "1"]:
            raise HTTPException(status_code=400, detail="Invalid is_holiday value")
        update_setting("is_holiday", is_holiday)
        logging.debug(f"Статус праздника обновлен: {is_holiday}")
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при обновлении статуса праздника: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/ticket/{ticket_id}", response_class=HTMLResponse)
async def ticket(request: Request, ticket_id: int, employee: dict = Depends(get_current_user)):
    logging.debug(f"Запрос к тикету #{ticket_id}")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT telegram_id, issue_type, assigned_to, auto_close_enabled, auto_close_time FROM tickets WHERE ticket_id = ?",
        (ticket_id,)
    )
    ticket_data = cursor.fetchone()
    if not ticket_data:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    telegram_id = ticket_data["telegram_id"]
    issue_type = ticket_data["issue_type"]
    assigned_to = ticket_data["assigned_to"]
    auto_close_enabled = ticket_data["auto_close_enabled"] 
    auto_close_time = ticket_data["auto_close_time"]       
    notification_enabled = ticket_data["notification_enabled"]


    if not assigned_to:
        cursor.execute("UPDATE tickets SET assigned_to = ? WHERE ticket_id = ?", (employee["telegram_id"], ticket_id))
        conn.commit()
        assigned_to = employee["telegram_id"]
        await sio.emit("ticket_assigned", {
            "ticket_id": ticket_id,
            "assigned_to": employee["telegram_id"],
            "assigned_login": employee["login"]
        })

    cursor.execute("""
        SELECT m.message_id, m.ticket_id, m.telegram_id, m.text, m.is_from_bot, m.timestamp,
            CASE WHEN m.is_from_bot THEN COALESCE(e2.login, 'Техподдержка') ELSE e.login END AS login,
            a.file_path, a.file_name, a.file_type
        FROM messages m
        JOIN employees e ON m.telegram_id = e.telegram_id
        LEFT JOIN employees e2 ON m.employee_telegram_id = e2.telegram_id
        LEFT JOIN attachments a ON m.message_id = a.message_id
        WHERE m.ticket_id = ?
        ORDER BY m.timestamp
    """, (ticket_id,))

    rows = cursor.fetchall()
    astana_tz = pytz.timezone('Asia/Almaty')

    messages_dict = {}
    for row in rows:
        msg_id = row[0]
        if msg_id not in messages_dict:
            messages_dict[msg_id] = {
                "message_id": row[0],
                "ticket_id": row[1],
                "telegram_id": row[2],
                "text": row[3],
                "is_from_bot": bool(row[4]),
                "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
                "login": row[6],
                "attachments": []
            }
        if row[7]:  # если есть файл
            messages_dict[msg_id]["attachments"].append({
                "file_path": row[7],
                "file_name": row[8],
                "file_type": row[9]
            })

    messages = list(messages_dict.values())

    cursor.execute("""
        SELECT am.message_id, am.ticket_id, am.telegram_id, am.text, am.timestamp, e.login
        FROM admin_messages am
        JOIN employees e ON am.telegram_id = e.telegram_id
        WHERE am.ticket_id = ?
        ORDER BY am.timestamp
    """, (ticket_id,))
    admin_messages = [
        {
            "message_id": row[0],
            "ticket_id": row[1],
            "telegram_id": row[2],
            "text": row[3],
            "timestamp": datetime.fromisoformat(row[4]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
            "login": row[5]
        }
        for row in cursor.fetchall()
    ]
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee_data = cursor.fetchone()
    login = employee_data[0] if employee_data else "Unknown"
    cursor.execute("SELECT telegram_id, login FROM employees WHERE is_admin = 1")
    support_employees = [{"telegram_id": row["telegram_id"], "login": row["login"]} for row in cursor.fetchall()]

    cursor.execute("SELECT end_time FROM mutes WHERE user_id = ?", (telegram_id,))
    mute_data = cursor.fetchone()
    is_muted = False
    mute_end_time = None
    if mute_data:
        end_time = mute_data["end_time"]
        if end_time and datetime.fromisoformat(end_time) > datetime.now():
            is_muted = True
            mute_end_time = end_time

    cursor.execute("SELECT end_time FROM bans WHERE user_id = ?", (telegram_id,))
    ban_data = cursor.fetchone()
    is_banned = False
    ban_end_time = None
    if ban_data:
        end_time = ban_data["end_time"]
        if end_time is None or datetime.fromisoformat(end_time) > datetime.now():
            is_banned = True
            ban_end_time = end_time

    cursor.execute("SELECT id, title, text, color FROM quick_replies ORDER BY title")
    quick_replies = [
        {
            "id": row["id"],
            "title": row["title"],
            "text": row["text"],
            "color": row["color"]
        }
        for row in cursor.fetchall()
    ]

    conn.close()
    return templates.TemplateResponse(
        "ticket.html",
        {
            "request": request,
            "ticket_id": ticket_id,
            "messages": messages,
            "admin_messages": admin_messages,
            "telegram_id": telegram_id,
            "login": login,
            "employee": employee,
            "issue_type": issue_type,
            "assigned_to": assigned_to,
            "support_employees": support_employees,
            "is_muted": is_muted,
            "mute_end_time": mute_end_time,
            "is_banned": is_banned,
            "ban_end_time": ban_end_time,
            "quick_replies": quick_replies,
            "BASE_URL": BASE_URL,
            "auto_close_enabled": auto_close_enabled,  # Добавляем
            "auto_close_time": auto_close_time,
            "notification_enabled": notification_enabled,     
            "from_history": request.query_params.get("from_history", "false") == "true",
            "shorten_filename": shorten_filename  # Добавляем функцию в контекст
        }
    )

@app.post("/add_quick_reply")
async def add_quick_reply(
    request: Request,
    title: str = Form(...),
    text: str = Form(...),
    color: str = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    if color not in ["blue", "green", "red", "purple", "gray"]:
        raise HTTPException(status_code=400, detail="Invalid color")
    
    if len(title) > 50 or len(text) > 1000:
        raise HTTPException(status_code=400, detail="Title or text too long")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO quick_replies (title, text, color) VALUES (?, ?, ?)",
            (title, text, color)
        )
        quick_reply_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        quick_reply = {
            "id": quick_reply_id,
            "title": title,
            "text": text,
            "color": color
        }
        await sio.emit("quick_reply_added", quick_reply)
        logging.debug(f"Добавлен быстрый ответ: {quick_reply}")
        return {"status": "ok", "quick_reply": quick_reply}
    except Exception as e:
        logging.error(f"Ошибка при добавлении быстрого ответа: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/delete_quick_reply")
async def delete_quick_reply(
    request: Request,
    quick_reply_id: int = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM quick_replies WHERE id = ?", (quick_reply_id,))
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Quick reply not found")
        conn.commit()
        conn.close()
        
        await sio.emit("quick_reply_deleted", {"id": quick_reply_id})
        logging.debug(f"Удалён быстрый ответ: id={quick_reply_id}")
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при удалении быстрого ответа: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/employees/send_message")
async def send_employee_message(
    request: Request,
    telegram_id: int = Form(...),
    text: str = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        await bot.send_message(chat_id=telegram_id, text=text)
        logging.debug(f"Сообщение отправлено в ЛС сотруднику {telegram_id}: {text}")
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения в ЛС {telegram_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/employees/toggle_admin")
async def toggle_admin_status(
    request: Request,
    telegram_id: int = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_admin FROM employees WHERE telegram_id = ?", (telegram_id,))
        current_status = cursor.fetchone()
        if not current_status:
            conn.close()
            raise HTTPException(status_code=404, detail="Employee not found")
        
        new_status = not current_status["is_admin"]
        cursor.execute("UPDATE employees SET is_admin = ? WHERE telegram_id = ?", (new_status, telegram_id))
        conn.commit()
        conn.close()
        
        await sio.emit("employee_updated", {
            "telegram_id": telegram_id,
            "is_admin": new_status
        })
        logging.debug(f"Статус техподдержки для {telegram_id} изменён на {new_status}")
        return {"status": "ok", "is_admin": new_status}
    except Exception as e:
        logging.error(f"Ошибка при изменении статуса техподдержки для {telegram_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send_message")
async def send_message(
    request: Request,
    ticket_id: int = Form(...),
    telegram_id: int = Form(...),
    text: str = Form(None),
    file: UploadFile = File(None),
    issue_type: str = Form(None),
    employee: dict = Depends(get_current_user)
):
    try:
        logging.debug(f"Отправка сообщения: ticket_id={ticket_id}, telegram_id={telegram_id}, text={text}, file={file.filename if file else None}, issue_type={issue_type}")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
        employee_data = cursor.fetchone()
        if not employee_data:
            conn.close()
            logging.error(f"Неверный telegram_id: {telegram_id}")
            raise HTTPException(status_code=400, detail="Invalid telegram_id")

        astana_tz = pytz.timezone('Asia/Almaty')
        timestamp = datetime.now(astana_tz).isoformat()
        
        db_text = text if text else ""
        # Убрали добавление "[Файл] {filename}" — имя теперь только в attachments, не в тексте
        
        cursor.execute(
            "INSERT INTO messages (ticket_id, telegram_id, employee_telegram_id, text, is_from_bot, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, telegram_id, employee["telegram_id"], db_text, 1, timestamp)
        )
        message_id = cursor.lastrowid

        file_path = None
        file_name = None
        file_type = None
        if file and file.filename:
            logging.debug(f"Получен файл: {file.filename}, тип: {file.content_type}")
            # Расширенная логика: MIME + fallback на extension для Ctrl+V без content_type
            extensions_image = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
            ext = os.path.splitext(file.filename.lower())[1] if file.filename else ''
            is_image_by_ext = ext in extensions_image

            file_type = 'image' if (file.content_type and file.content_type.startswith('image/')) or is_image_by_ext else 'document'
            if file_type == 'image':
                file_name = f"image_{int(time.time())}{ext if ext else '.png'}"
            else:
                file_name = file.filename or 'document'
            file_name = get_unique_filename(file_name, directory="Uploads")
            file_path = f"Uploads/{file_name}"
            try:
                os.makedirs("Uploads", exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(await file.read())
                logging.debug(f"Файл сохранен: {file_path}")
                if not os.path.exists(file_path):
                    logging.error(f"Файл не найден после сохранения: {file_path}")
                    raise HTTPException(status_code=500, detail="File not found after saving")
                cursor.execute(
                    "INSERT INTO attachments (message_id, file_path, file_name, file_type) VALUES (?, ?, ?, ?)",
                    (message_id, file_path, file_name, file_type)
                )
            except PermissionError as e:
                logging.error(f"Ошибка прав доступа при сохранении файла: {e}")
                conn.close()
                raise HTTPException(status_code=500, detail="Permission denied when saving file")
            except Exception as e:
                logging.error(f"Ошибка при сохранении файла: {e}")
                conn.close()
                raise HTTPException(status_code=500, detail=f"Failed to save file: {str(e)}")

        if issue_type in ["tech", "org", "ins", "n/a"]:
            db_issue_type = None if issue_type == "n/a" else issue_type
            cursor.execute(
                "UPDATE tickets SET issue_type = ? WHERE ticket_id = ?",
                (db_issue_type, ticket_id)
            )
            await sio.emit("issue_type_updated", {
                "ticket_id": ticket_id,
                "issue_type": db_issue_type
            })

        conn.commit()
        conn.close()
        logging.debug(f"Сообщение сохранено в базе данных: ticket_id={ticket_id}")

        if loop is None:
            logging.error("Цикл событий не инициализирован")
            raise HTTPException(status_code=500, detail="Event loop not initialized")

        telegram_text = text if text else ""
        queue_data = {
            "telegram_id": telegram_id,
            "text": telegram_text,
            "message_id": message_id
        }
        if file_path:
            queue_data["file_path"] = file_path
            queue_data["file_type"] = file_type
        logging.debug(f"Добавляем в очередь: {queue_data}")
        await message_queue.put(queue_data)
        logging.debug(f"Сообщение добавлено в очередь")

        logging.debug(f"Отправляем уведомление через SocketIO для ticket_id={ticket_id}")
        attachments = []
        if file_path:
            attachments = [{"file_path": file_path, "file_name": file_name, "file_type": file_type}]

        await sio.emit("new_message", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "text": db_text,  # Чистый текст
            "is_from_bot": True,
            "timestamp": timestamp,
            "login": employee["login"],
            "attachments": attachments,  # <-- Добавь это — унифицирует с main.py
            "message_id": message_id
        })
        logging.debug("Уведомление через SocketIO отправлено")

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/delete_message")
async def delete_message(
    request: Request,
    message_id: int = Form(...),
    ticket_id: int = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        logging.error(f"Пользователь {employee['telegram_id']} не является администратором")
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        logging.debug(f"Удаление сообщения: message_id={message_id}, ticket_id={ticket_id}")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT telegram_id, is_from_bot, telegram_message_id FROM messages WHERE message_id = ? AND ticket_id = ?",
            (message_id, ticket_id)
        )
        message = cursor.fetchone()
        if not message:
            conn.close()
            logging.error(f"Сообщение message_id={message_id} не найдено")
            raise HTTPException(status_code=404, detail="Message not found")
        
        if not message["is_from_bot"]:
            conn.close()
            logging.error(f"Сообщение message_id={message_id} не от бота, удаление запрещено")
            raise HTTPException(status_code=403, detail="Can only delete bot messages")

        if message["telegram_message_id"]:
            try:
                await bot.delete_message(
                    chat_id=message["telegram_id"],
                    message_id=message["telegram_message_id"]
                )
                logging.debug(f"Сообщение telegram_message_id={message['telegram_message_id']} удалено в Telegram")
            except Exception as e:
                logging.warning(f"Не удалось удалить сообщение в Telegram: {e}")

        cursor.execute("SELECT file_path FROM attachments WHERE message_id = ?", (message_id,))
        attachment = cursor.fetchone()
        if attachment and os.path.exists(attachment["file_path"]):
            try:
                os.remove(attachment["file_path"])
                logging.debug(f"Файл {attachment['file_path']} удалён")
            except Exception as e:
                logging.error(f"Ошибка удаления файла {attachment['file_path']}: {e}")

        cursor.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
        cursor.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
        if cursor.rowcount == 0:
            conn.close()
            logging.error(f"Сообщение message_id={message_id} не найдено при удалении")
            raise HTTPException(status_code=404, detail="Message not found")
        
        conn.commit()
        conn.close()
        logging.debug(f"Сообщение message_id={message_id} удалено из базы")

        await sio.emit("message_deleted", {
            "ticket_id": ticket_id,
            "message_id": message_id
        })
        logging.debug(f"Событие message_deleted отправлено для message_id={message_id}")

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщения: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete message: {str(e)}")

@app.post("/edit_message")
async def edit_message(
    request: Request,
    message_id: int = Form(...),
    ticket_id: int = Form(...),
    text: str = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        logging.error(f"Пользователь {employee['telegram_id']} не является администратором")
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        logging.debug(f"Редактирование сообщения: message_id={message_id}, ticket_id={ticket_id}, new_text={text}")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT telegram_id, is_from_bot, telegram_message_id, timestamp FROM messages WHERE message_id = ? AND ticket_id = ?",
            (message_id, ticket_id)
        )
        message = cursor.fetchone()
        if not message:
            conn.close()
            logging.error(f"Сообщение message_id={message_id} не найдено")
            raise HTTPException(status_code=404, detail="Message not found")
        
        if not message["is_from_bot"]:
            conn.close()
            logging.error(f"Сообщение message_id={message_id} не от бота, редактирование запрещено")
            raise HTTPException(status_code=403, detail="Can only edit bot messages")

        cursor.execute(
            "UPDATE messages SET text = ? WHERE message_id = ?",
            (text, message_id)
        )
        if cursor.rowcount == 0:
            conn.close()
            logging.error(f"Сообщение message_id={message_id} не найдено при редактировании")
            raise HTTPException(status_code=404, detail="Message not found")
        
        conn.commit()
        conn.close()
        logging.debug(f"Сообщение message_id={message_id} отредактировано в базе")

        if loop is None:
            logging.error("Цикл событий не инициализирован")
            raise HTTPException(status_code=500, detail="Event loop not initialized")

        queue_data = {
            "telegram_id": message["telegram_id"],
            "text": text,
            "message_id": message_id,
            "telegram_message_id": message["telegram_message_id"]
        }
        logging.debug(f"Добавляем отредактированное сообщение в очередь: {queue_data}")
        await message_queue.put(queue_data)
        logging.debug(f"Отредактированное сообщение добавлено в очередь")

        await sio.emit("message_edited", {
            "ticket_id": ticket_id,
            "message_id": message_id,
            "text": text,
            "timestamp": message["timestamp"],
            "telegram_id": message["telegram_id"],
            "login": employee["login"],
            "is_from_bot": True
        })
        logging.debug(f"Событие message_edited отправлено для message_id={message_id}")

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при редактировании сообщения: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to edit message: {str(e)}")

@app.post("/send_admin_message")
async def send_admin_message(
    request: Request,
    ticket_id: int = Form(...),
    text: str = Form(...),
    employee: dict = Depends(get_current_user)
):
    try:
        logging.debug(f"Отправка сообщения в админ-чат: ticket_id={ticket_id}, text={text}")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
        ticket_data = cursor.fetchone()
        if not ticket_data:
            conn.close()
            logging.error(f"Тикет #{ticket_id} не найден")
            raise HTTPException(status_code=404, detail="Ticket not found")

        astana_tz = pytz.timezone('Asia/Almaty')
        timestamp = datetime.now(astana_tz).isoformat()
        
        cursor.execute(
            "INSERT INTO admin_messages (ticket_id, telegram_id, text, timestamp) VALUES (?, ?, ?, ?)",
            (ticket_id, employee["telegram_id"], text, timestamp)
        )
        conn.commit()
        conn.close()
        logging.debug(f"Сообщение в админ-чат сохранено: ticket_id={ticket_id}")

        await sio.emit("new_admin_message", {
            "ticket_id": ticket_id,
            "telegram_id": employee["telegram_id"],
            "text": text,
            "timestamp": timestamp,
            "login": employee["login"]
        })
        logging.debug("Уведомление о новом сообщении в админ-чате отправлено через SocketIO")

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения в админ-чат: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/close_ticket")
async def close_ticket(request: Request, ticket_id: int = Form(...), employee: dict = Depends(get_current_user)):
    try:
        logging.debug(f"Закрытие тикета #{ticket_id}")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
        ticket_data = cursor.fetchone()
        if not ticket_data:
            conn.close()
            raise HTTPException(status_code=404, detail="Ticket not found")
        telegram_id = ticket_data[0]

        cursor.execute("UPDATE tickets SET status = 'closed' WHERE ticket_id = ?", (ticket_id,))
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Ticket not found")
        # Remove existing ratings for this ticket
        cursor.execute("DELETE FROM ticket_ratings WHERE ticket_id = ?", (ticket_id,))
        cursor.execute("DELETE FROM employee_ratings WHERE ticket_id = ?", (ticket_id,))
        conn.commit()
        conn.close()
        logging.debug(f"Тикет #{ticket_id} закрыт")

        queue_data = {
            "telegram_id": telegram_id,
            "text": "Ваше обращение закрыто. Вы можете оценить работу техподдержки ниже.",
            "message_id": None,
            "ticket_id": ticket_id
        }
        logging.debug(f"Добавляем уведомление о закрытии в очередь: {queue_data}")
        await message_queue.put(queue_data)
        logging.debug(f"Уведомление о закрытии добавлено в очередь")

        await sio.emit("ticket_closed", {"ticket_id": ticket_id})
        logging.debug(f"Событие ticket_closed отправлено для ticket_id={ticket_id}")

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при закрытии тикета: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/assign_ticket")
async def assign_ticket_endpoint(
    request: Request, 
    ticket_id: int = Form(...), 
    assigned_to: str = Form(None),
    employee: dict = Depends(get_current_user)
):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        assigned_login = None
        assigned_to_id = None

        if assigned_to:
            try:
                assigned_to_id = int(assigned_to)
                cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (assigned_to_id,))
                employee_data = cursor.fetchone()
                if not employee_data:
                    conn.close()
                    raise HTTPException(status_code=400, detail="Invalid assigned_to telegram_id")
                assigned_login = employee_data[0]
            except ValueError:
                conn.close()
                raise HTTPException(status_code=400, detail="Invalid assigned_to telegram_id format")

        cursor.execute("UPDATE tickets SET assigned_to = ? WHERE ticket_id = ?", (assigned_to_id, ticket_id))
        if cursor.rowcount == 0:
            cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
            ticket_data = cursor.fetchone()
            conn.close()
            if not ticket_data:
                raise HTTPException(status_code=404, detail="Ticket not found")
            else:
                raise HTTPException(status_code=500, detail="Failed to assign ticket")

        cursor.execute(
            """
            SELECT m.text, m.timestamp, 
                CASE WHEN m.is_from_bot THEN COALESCE(e2.login, 'Техподдержка') ELSE e.login END AS login,
                a.file_name
            FROM messages m
            JOIN employees e ON m.telegram_id = e.telegram_id
            LEFT JOIN employees e2 ON m.employee_telegram_id = e2.telegram_id
            LEFT JOIN attachments a ON m.message_id = a.message_id
            WHERE m.ticket_id = ?
            ORDER BY m.timestamp DESC
            LIMIT 5
            """,
            (ticket_id,)
        )
        messages = cursor.fetchall()
        conn.commit()
        conn.close()

        history_text = f"Вам назначен тикет #{ticket_id}.\n\nПоследние сообщения:\n"
        if messages:
            astana_tz = pytz.timezone('Asia/Almaty')
            for msg in reversed(messages):
                timestamp = datetime.fromisoformat(msg["timestamp"]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S')
                file_info = f" [Файл: {msg['file_name']}]" if msg['file_name'] else ""
                history_text += f"[{timestamp}] {msg['login']}: {msg['text']}{file_info}\n"
        else:
            history_text += "Сообщений пока нет.\n"

        await sio.emit("ticket_assigned", {
            "ticket_id": ticket_id,
            "assigned_to": assigned_to_id,
            "assigned_login": assigned_login
        })

        # Отправляем уведомление в чат только если тикет назначен конкретному сотруднику
        if assigned_to_id:
            await send_notification_to_topic(ticket_id, assigned_login, f"Тикет переназначен на {assigned_login}")
            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Открыть тикет", url=f"{BASE_URL}/ticket/{ticket_id}")
                    ]
                ])
                await bot.send_message(
                    chat_id=assigned_to_id,
                    text=history_text,
                    reply_markup=keyboard
                )
                logging.debug(f"Персональное уведомление отправлено сотруднику {assigned_to_id} о назначении тикета #{ticket_id}")
            except Exception as e:
                logging.error(f"Ошибка отправки персонального уведомления сотруднику {assigned_to_id}: {e}")

        return {"status": "ok", "assigned_to": assigned_login}
    except Exception as e:
        logging.error(f"Ошибка при переназначении тикета: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_issue_type")
async def update_issue_type(
    request: Request,
    ticket_id: int = Form(...),
    issue_type: str = Form(...),
    employee: dict = Depends(get_current_user)
):
    if issue_type not in ["tech", "org", "ins", "n/a"]:
        raise HTTPException(status_code=400, detail="Invalid issue type")
    db_issue_type = None if issue_type == "n/a" else issue_type
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET issue_type = ? WHERE ticket_id = ?", (db_issue_type, ticket_id))
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    conn.commit()
    conn.close()
    await sio.emit("issue_type_updated", {
        "ticket_id": ticket_id,
        "issue_type": db_issue_type
    })
    return {"status": "ok"}

@app.post("/cleanup")
async def cleanup(request: Request, employee: dict = Depends(get_current_user)):
    from dateutil.relativedelta import relativedelta
    conn = get_db_connection()
    cursor = conn.cursor()
    astana_tz = pytz.timezone('Asia/Almaty')
    threshold = (datetime.now(astana_tz) - relativedelta(months=6)).isoformat()
    cursor.execute("DELETE FROM tickets WHERE status = 'closed' AND created_at < ?", (threshold,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/fetch_telegram_history")
async def fetch_telegram_history(
    request: Request, 
    ticket_id: int = Form(...), 
    telegram_id: int = Form(...), 
    displayed_ticket_ids: str = Form(""),  # Comma-separated list of ticket IDs
    employee: dict = Depends(get_current_user)
):
    try:
        logging.debug(f"Fetching history for ticket_id={ticket_id}, telegram_id={telegram_id}, displayed_ticket_ids={displayed_ticket_ids}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ? AND status = 'open'", (ticket_id,))
        ticket_data = cursor.fetchone()
        if not ticket_data or ticket_data[0] != telegram_id:
            conn.close()
            raise HTTPException(status_code=404, detail="Ticket not found or invalid telegram_id")

        # Parse displayed ticket IDs
        displayed_ids = [int(tid) for tid in displayed_ticket_ids.split(",") if tid.strip().isdigit()]

        # Fetch the next closed ticket, excluding displayed ones
        query = """
            SELECT t.ticket_id
            FROM tickets t
            WHERE t.telegram_id = ? AND t.status = 'closed'
        """
        params = [telegram_id]
        if displayed_ids:
            query += " AND t.ticket_id NOT IN ({})".format(",".join("?" * len(displayed_ids)))
            params.extend(displayed_ids)
        query += " ORDER BY t.created_at DESC LIMIT 1"
        
        cursor.execute(query, params)
        next_ticket = cursor.fetchone()

        if not next_ticket:
            conn.close()
            await sio.emit("no_more_history", {"ticket_id": ticket_id, "message": "No more history available"})
            logging.debug("No more closed tickets to fetch")
            return {"status": "no_more_history"}

        fetched_ticket_id = next_ticket[0]
        astana_tz = pytz.timezone('Asia/Almaty')
        
        cursor.execute(
            """
            SELECT m.message_id, m.ticket_id, m.telegram_id, m.text, m.is_from_bot, m.timestamp,
                CASE WHEN m.is_from_bot THEN COALESCE(e2.login, 'Техподдержка') ELSE e.login END AS login,
                a.file_path, a.file_name, a.file_type
            FROM messages m
            JOIN employees e ON m.telegram_id = e.telegram_id
            LEFT JOIN employees e2 ON m.employee_telegram_id = e2.telegram_id
            LEFT JOIN attachments a ON m.message_id = a.message_id
            WHERE m.ticket_id = ?
            ORDER BY m.timestamp DESC
            """,
            (fetched_ticket_id,)
        )
        rows = cursor.fetchall()

        # Группируем вложения по message_id
        messages_dict = {}
        for row in rows:
            msg_id = row[0]
            if msg_id not in messages_dict:
                messages_dict[msg_id] = {
                    "message_id": row[0],
                    "ticket_id": row[1],
                    "telegram_id": row[2],
                    "text": row[3],
                    "is_from_bot": bool(row[4]),
                    "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
                    "login": row[6],
                    "attachments": [],
                    "is_history": True,
                    "history_ticket_id": row[1]
                }
            if row[7]:  # есть вложение
                messages_dict[msg_id]["attachments"].append({
                    "file_path": row[7],
                    "file_name": row[8],
                    "file_type": row[9]
                })

        messages = list(messages_dict.values())

        conn.close()

        for msg in messages:
            await sio.emit("new_message", msg)
            logging.debug(f"Sent message from ticket #{msg['ticket_id']}: {msg}")

        logging.debug(f"Fetched history from ticket #{fetched_ticket_id}, sent {len(messages)} messages")
        return {"status": "ok", "fetched_ticket_ids": [fetched_ticket_id], "messages_count": len(messages)}
    except Exception as e:
        logging.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/employee/{telegram_id}/ratings")
async def get_employee_ratings(telegram_id: int, employee: dict = Depends(get_current_user)):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 
                (SELECT COUNT(*) FROM employee_ratings WHERE employee_id = ? AND rating = 'up') AS thumbs_up,
                (SELECT COUNT(*) FROM employee_ratings WHERE employee_id = ? AND rating = 'down') AS thumbs_down
            """,
            (telegram_id, telegram_id)
        )
        ratings = cursor.fetchone()
        conn.close()
        return {
            "status": "ok",
            "thumbs_up": ratings["thumbs_up"],
            "thumbs_down": ratings["thumbs_down"]
        }
    except Exception as e:
        logging.error(f"Error fetching ratings for employee {telegram_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/admin/employees", response_class=HTMLResponse)
async def admin_employees(request: Request, employee: dict = Depends(get_current_user)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, login, is_admin, full_name FROM employees")
    employees = [
        {
            "telegram_id": row["telegram_id"],
            "login": row["login"],
            "is_admin": row["is_admin"],
            "full_name": row["full_name"]
        }
        for row in cursor.fetchall()
    ]
    conn.close()

    async def update_full_name(emp):
        try:
            tg_user: User = await bot.get_chat(emp["telegram_id"])
            new_full_name = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])).strip() or f"User {emp['telegram_id']}"
            if new_full_name != emp["full_name"]:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE employees SET full_name = ? WHERE telegram_id = ?",
                    (new_full_name, emp["telegram_id"])
                )
                conn.commit()
                conn.close()
                emp["full_name"] = new_full_name
        except Exception as e:
            logging.error(f"Ошибка обновления full_name для telegram_id={emp['telegram_id']}: {e}")

    await asyncio.gather(*[update_full_name(emp) for emp in employees])
    return templates.TemplateResponse("admin_employees.html", {"request": request, "employees": employees, "employee": employee})

@app.post("/admin/employees/add")
async def add_employee(
    request: Request,
    telegram_id: str = Form(...),
    login: str = Form(...),
    is_admin: bool = Form(False),
    employee: dict = Depends(get_current_user)
):
    try:
        telegram_id = int(telegram_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный Telegram ID")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO employees (telegram_id, login, is_admin) VALUES (?, ?, ?)",
            (telegram_id, login, is_admin)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Этот Telegram ID или логин уже занят")
    conn.close()
    return RedirectResponse(url="/admin/employees", status_code=303)

@app.post("/admin/employees/delete")
async def delete_employee(request: Request, telegram_id: str = Form(...), employee: dict = Depends(get_current_user)):
    try:
        telegram_id = int(telegram_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный Telegram ID")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM employees WHERE telegram_id = ?", (telegram_id,))
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Сотрудник не найден")
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin/employees", status_code=303)

@app.post("/mute_user")
async def mute_user(
    request: Request,
    ticket_id: int = Form(...),
    telegram_id: int = Form(...),
    mute_duration: int = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    end_time = (datetime.now() + timedelta(minutes=mute_duration)).isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO mutes (user_id, end_time) VALUES (?, ?)", (telegram_id, end_time))
    conn.commit()
    conn.close()
    logging.debug(f"Пользователь {telegram_id} замучен на {mute_duration} минут")
    return {"status": "ok"}

@app.post("/ban_user")
async def ban_user(
    request: Request,
    ticket_id: int = Form(...),
    telegram_id: int = Form(...),
    ban_duration: int = Form(None),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    end_time = (datetime.now() + timedelta(minutes=ban_duration)).isoformat() if ban_duration else None
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO bans (user_id, end_time) VALUES (?, ?)", (telegram_id, end_time))
    conn.commit()
    conn.close()
    logging.debug(f"Пользователь {telegram_id} забанен на {ban_duration if ban_duration else 'навсегда'} минут")
    return {"status": "ok"}

@app.post("/unmute_user")
async def unmute_user(
    request: Request,
    ticket_id: int = Form(...),
    telegram_id: int = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mutes WHERE user_id = ?", (telegram_id,))
    conn.commit()
    conn.close()
    logging.debug(f"Мут снят с пользователя {telegram_id}")
    return {"status": "ok"}

@app.get("/search", response_class=HTMLResponse)
async def search_tickets(
    request: Request,
    query: str = Query("", description="Search query"),
    status: str = Query("", description="Ticket status filter: open, closed"),
    issue_type: str = Query("", description="Issue type filter: tech, org, ins, n/a"),
    sort: str = Query("timestamp_desc", description="Sort order: timestamp_desc, timestamp_asc, ticket_id_desc, ticket_id_asc"),
    employee: dict = Depends(get_current_user)
):
    logging.debug(f"Поиск тикетов: query={query}, status={status}, issue_type={issue_type}, sort={sort}")
    async with db_lock:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Базовый SQL-запрос
            base_query = """
                SELECT DISTINCT t.ticket_id, t.telegram_id, t.status, e.login,
                       m.text AS last_message, m.timestamp AS last_message_timestamp,
                       a.file_path, a.file_name, a.file_type,
                       t.issue_type, t.assigned_to, e2.login AS assigned_login
                FROM tickets t
                JOIN employees e ON t.telegram_id = e.telegram_id
                LEFT JOIN messages m ON t.ticket_id = m.ticket_id
                LEFT JOIN attachments a ON m.message_id = a.message_id
                LEFT JOIN employees e2 ON t.assigned_to = e2.telegram_id
                WHERE 1=1
            """
            params = []
            
            # Условия поиска
            if query:
                query_lower = f"%{query.lower()}%"
                base_query += """
                    AND (
                        LOWER(m.text) LIKE ?
                        OR LOWER(e.login) LIKE ?
                        OR LOWER(t.issue_type) LIKE ?
                    )
                """
                params.extend([query_lower, query_lower, query_lower])

            
            # Фильтр по статусу
            if status in ["open", "closed"]:
                base_query += " AND t.status = ?"
                params.append(status)
            
            # Фильтр по типу проблемы
            if issue_type in ["tech", "org", "ins"]:
                base_query += " AND t.issue_type = ?"
                params.append(issue_type)
            elif issue_type == "n/a":
                base_query += " AND t.issue_type IS NULL"
            
            # Сортировка
            if sort == "timestamp_asc":
                base_query += " ORDER BY m.timestamp ASC"
            elif sort == "ticket_id_desc":
                base_query += " ORDER BY t.ticket_id DESC"
            elif sort == "ticket_id_asc":
                base_query += " ORDER BY t.ticket_id ASC"
            else:  # timestamp_desc (default)
                base_query += " ORDER BY m.timestamp DESC"
            
            cursor.execute(base_query, params)
            astana_tz = pytz.timezone('Asia/Almaty')
            tickets = [
                {
                    "id": row["ticket_id"],
                    "telegram_id": row["telegram_id"],
                    "status": row["status"],
                    "login": row["login"],
                    "last_message": row["last_message"],
                    "last_message_timestamp": datetime.fromisoformat(row["last_message_timestamp"]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S') if row["last_message_timestamp"] else None,
                    "file_path": row["file_path"],
                    "file_name": row["file_name"],
                    "file_type": row["file_type"],
                    "issue_type": row["issue_type"],
                    "assigned_to": row["assigned_to"],
                    "assigned_login": row["assigned_login"]
                }
                for row in cursor.fetchall()
            ]
    
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "tickets": tickets,
            "employee": employee,
            "query": query.strip("%") if query else "",
            "status": status,
            "issue_type": issue_type,
            "sort": sort,
            "BASE_URL": BASE_URL
        }
    )

@app.get("/ticket/{ticket_id}/ratings")
async def get_ticket_ratings(ticket_id: int, employee: dict = Depends(get_current_user)):
    """
    Retrieve the count of thumbs up and thumbs down ratings for a specific ticket.
    """
    try:
        logging.debug(f"Запрос рейтингов для тикета #{ticket_id}")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT 
                (SELECT COUNT(*) FROM ticket_ratings WHERE ticket_id = ? AND rating = 'up') AS thumbs_up,
                (SELECT COUNT(*) FROM ticket_ratings WHERE ticket_id = ? AND rating = 'down') AS thumbs_down
            """,
            (ticket_id, ticket_id)
        )
        ratings = cursor.fetchone()
        
        conn.close()
        
        if not ratings:
            logging.error(f"Рейтинги для тикета #{ticket_id} не найдены")
            raise HTTPException(status_code=404, detail="Ratings not found")
        
        return {
            "status": "ok",
            "ticket_id": ticket_id,
            "thumbs_up": ratings["thumbs_up"],
            "thumbs_down": ratings["thumbs_down"]
        }
    except Exception as e:
        logging.error(f"Ошибка при получении рейтингов тикета #{ticket_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/unban_user")
async def unban_user(
    request: Request,
    ticket_id: int = Form(...),
    telegram_id: int = Form(...),
    employee: dict = Depends(get_current_user)
):
    if not employee["is_admin"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bans WHERE user_id = ?", (telegram_id,))
    conn.commit()
    conn.close()
    logging.debug(f"Бан снят с пользователя {telegram_id}")
    return {"status": "ok"}

@sio.event
async def new_ticket(sid, data):
    logging.debug(f"Получено событие new_ticket: {data}")
    await sio.emit("update_tickets", data)
    logging.debug("Событие update_tickets отправлено")

@sio.event
async def connect(sid, environ):
    logging.debug(f"Клиент подключился: {sid}")

@sio.event
async def disconnect(sid):
    logging.debug(f"Клиент отключился: {sid}")

@sio.event
async def ticket_reopened(sid, data):
    logging.debug(f"Получено событие ticket_reopened: {data}")
    await sio.emit("update_tickets", data)
    logging.debug("Событие update_tickets отправлено для переоткрытого тикета")

auto_close_tasks = {}  # Dict для хранения задач по ticket_id

async def close_ticket_after_delay(ticket_id, telegram_id, delay_hours=1):
    delay_seconds = delay_hours * 3600
    logging.debug(f"Started timer for ticket #{ticket_id}: sleep for {delay_seconds} seconds")
    try:
        await asyncio.sleep(delay_seconds)
        logging.debug(f"Timer expired for ticket #{ticket_id}: checking for close")
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Проверяем, все еще включено ли
        cursor.execute("SELECT auto_close_enabled, auto_close_time FROM tickets WHERE ticket_id = ?", (ticket_id,))
        ticket = cursor.fetchone()
        if not ticket or not ticket["auto_close_enabled"]:
            logging.debug(f"Auto-close disabled for ticket #{ticket_id} - skipping")
            conn.close()
            return
        
        auto_close_time = ticket["auto_close_time"]
        start_check_time = (datetime.fromisoformat(auto_close_time) - timedelta(hours=delay_hours)).isoformat()
        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE ticket_id = ? AND is_from_bot = 0 AND timestamp > ?",
            (ticket_id, start_check_time)
        )
        user_replies = cursor.fetchone()[0]
        
        if user_replies == 0:
            cursor.execute("UPDATE tickets SET status = 'closed', auto_close_enabled = 0, auto_close_time = NULL WHERE ticket_id = ?", (ticket_id,))
            logging.info(f"Auto-closed ticket #{ticket_id} due to no user replies")
            await message_queue.put({
                "telegram_id": telegram_id,
                "text": "Ваше обращение закрыто автоматически из-за отсутствия ответа.",
                "ticket_id": ticket_id
            })
            await sio.emit('ticket_closed', {"ticket_id": ticket_id})
        else:
            logging.debug(f"Ticket #{ticket_id} not closed - has {user_replies} user replies")
        
        conn.commit()
        conn.close()
    except asyncio.CancelledError:
        logging.debug(f"Timer for ticket #{ticket_id} cancelled")
    finally:
        if ticket_id in auto_close_tasks:
            del auto_close_tasks[ticket_id]

@sio.event
async def toggle_auto_close(sid, data):
    ticket_id = data['ticket_id']
    enabled = data['enabled']
    logging.debug(f"Toggle auto-close for ticket #{ticket_id}: {enabled}")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    if enabled:
        auto_close_time = (datetime.now(astana_tz) + timedelta(hours=1)).isoformat()
        cursor.execute(
            "UPDATE tickets SET auto_close_enabled = 1, auto_close_time = ? WHERE ticket_id = ?",
            (auto_close_time, ticket_id)
        )
        telegram_id = cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]
        message = "Тикет будет закрыт автоматически в течение часа при отсутствии ответа."
        await message_queue.put({
            "telegram_id": telegram_id,
            "text": message,
            "ticket_id": ticket_id
        })
        # Запускаем индивидуальный таймер
        if ticket_id in auto_close_tasks:
            auto_close_tasks[ticket_id].cancel()  # Отменяем старый, если был
        auto_close_tasks[ticket_id] = asyncio.create_task(close_ticket_after_delay(ticket_id, telegram_id))
    else:
        cursor.execute(
            "UPDATE tickets SET auto_close_enabled = 0, auto_close_time = NULL WHERE ticket_id = ?",
            (ticket_id,)
        )
        if ticket_id in auto_close_tasks:
            auto_close_tasks[ticket_id].cancel()
            del auto_close_tasks[ticket_id]
            logging.debug(f"Cancelled auto-close timer for ticket #{ticket_id}")
    conn.commit()
    conn.close()
    
    await sio.emit('auto_close_updated', {
        "ticket_id": ticket_id,
        "enabled": enabled,
        "auto_close_time": auto_close_time if enabled else None
    })

@sio.event
async def toggle_notification(sid, data):
    ticket_id = data['ticket_id']
    enabled = data['enabled']
    logging.debug(f"Toggle notification for ticket #{ticket_id}: {enabled}")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tickets SET notification_enabled = ? WHERE ticket_id = ?",
        (1 if enabled else 0, ticket_id)
    )
    conn.commit()
    conn.close()
    
    await sio.emit('notification_updated', {
        "ticket_id": ticket_id,
        "enabled": enabled
    })