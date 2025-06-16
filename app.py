from fastapi import FastAPI, Request, Form, HTTPException, Depends, File, UploadFile, Query
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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)

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

def verify_telegram_auth(data: dict, bot_token: str) -> bool:
    received_hash = data.pop("hash", None)
    if not received_hash:
        logging.error("Отсутствует hash в Telegram данных")
        return False
    
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()) if v)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    logging.debug(f"Проверка подписи: computed_hash={computed_hash}, received_hash={received_hash}")
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

async def send_notification_to_topic(ticket_id: int, login: str, message: str):
    if not NOTIFICATION_CHAT_ID or not NOTIFICATION_TOPIC_ID:
        logging.warning("NOTIFICATION_CHAT_ID или NOTIFICATION_TOPIC_ID не заданы, уведомление не отправлено")
        return
    try:
        history_text = f"Тикет #{ticket_id} ({login}): {message}"
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
        SELECT m.message_id, m.ticket_id, m.telegram_id, m.text, m.is_from_bot, m.timestamp, e.login,
               a.file_path, a.file_name, a.file_type
        FROM messages m
        JOIN employees e ON m.telegram_id = e.telegram_id
        LEFT JOIN attachments a ON m.message_id = a.message_id
        WHERE m.ticket_id = ?
        ORDER BY m.timestamp
        """,
        (ticket_id,)
    )
    astana_tz = pytz.timezone('Asia/Almaty')
    messages = [
        {
            "message_id": row[0],
            "ticket_id": row[1],
            "telegram_id": row[2],
            "text": row[3],
            "is_from_bot": bool(row[4]),
            "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
            "login": row[6],
            "file_path": row[7],
            "file_name": row[8],
            "file_type": row[9]
        }
        for row in cursor.fetchall()
    ]
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee_data = cursor.fetchone()
    login = employee_data[0] if employee_data else "Unknown"
    conn.close()
    return templates.TemplateResponse(
        "quickview.html",
        {
            "request": request,
            "ticket_id": ticket_id,
            "messages": messages,
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
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.ticket_id, t.telegram_id, e.login, 
                       m.text AS last_message, m.timestamp AS last_message_timestamp,
                       a.file_path, a.file_name, a.file_type,
                       t.issue_type, t.assigned_to, e2.login AS assigned_login
                FROM tickets t 
                JOIN employees e ON t.telegram_id = e.telegram_id 
                LEFT JOIN (
                    SELECT ticket_id, text, timestamp, message_id
                    FROM messages
                    WHERE (ticket_id, timestamp) IN (
                        SELECT ticket_id, MAX(timestamp)
                        FROM messages
                        GROUP BY ticket_id
                    )
                ) m ON t.ticket_id = m.ticket_id
                LEFT JOIN attachments a ON m.message_id = a.message_id
                LEFT JOIN employees e2 ON t.assigned_to = e2.telegram_id
                WHERE t.status = 'open'
            """)
            astana_tz = pytz.timezone('Asia/Almaty')
            tickets = [
                {
                    "id": row["ticket_id"],
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
    return templates.TemplateResponse("index.html", {"request": request, "tickets": tickets, "employee": employee})

@app.get("/ticket/{ticket_id}", response_class=HTMLResponse)
async def ticket(request: Request, ticket_id: int, employee: dict = Depends(get_current_user)):
    logging.debug(f"Запрос к тикету #{ticket_id}")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, issue_type, assigned_to FROM tickets WHERE ticket_id = ?", (ticket_id,))
    ticket_data = cursor.fetchone()
    if not ticket_data:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    telegram_id = ticket_data["telegram_id"]
    issue_type = ticket_data["issue_type"]
    assigned_to = ticket_data["assigned_to"]

    if not assigned_to:
        cursor.execute("UPDATE tickets SET assigned_to = ? WHERE ticket_id = ?", (employee["telegram_id"], ticket_id))
        conn.commit()
        assigned_to = employee["telegram_id"]
        await sio.emit("ticket_assigned", {
            "ticket_id": ticket_id,
            "assigned_to": employee["telegram_id"],
            "assigned_login": employee["login"]
        })

    cursor.execute(
        """
        SELECT m.message_id, m.ticket_id, m.telegram_id, m.text, m.is_from_bot, m.timestamp, e.login,
               a.file_path, a.file_name, a.file_type
        FROM messages m
        JOIN employees e ON m.telegram_id = e.telegram_id
        LEFT JOIN attachments a ON m.message_id = a.message_id
        WHERE m.ticket_id = ?
        ORDER BY m.timestamp
        """,
        (ticket_id,)
    )
    astana_tz = pytz.timezone('Asia/Almaty')
    messages = [
        {
            "message_id": row[0],
            "ticket_id": row[1],
            "telegram_id": row[2],
            "text": row[3],
            "is_from_bot": bool(row[4]),
            "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
            "login": row[6],
            "file_path": row[7],
            "file_name": row[8],
            "file_type": row[9]
        }
        for row in cursor.fetchall()
    ]
    cursor.execute(
        """
        SELECT am.message_id, am.ticket_id, am.telegram_id, am.text, am.timestamp, e.login
        FROM admin_messages am
        JOIN employees e ON am.telegram_id = e.telegram_id
        WHERE am.ticket_id = ?
        ORDER BY am.timestamp
        """,
        (ticket_id,)
    )
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
            "quick_replies": quick_replies
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
        if file and file.filename:
            db_text = f"[Файл] {file.filename}" if not text else text
        
        cursor.execute(
            "INSERT INTO messages (ticket_id, telegram_id, text, is_from_bot, timestamp) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, telegram_id, db_text, 1, timestamp)
        )
        message_id = cursor.lastrowid

        file_path = None
        file_name = None
        file_type = None
        if file and file.filename:
            logging.debug(f"Получен файл: {file.filename}, тип: {file.content_type}")
            file_type = 'image' if file.content_type.startswith('image/') else 'document'
            if file_type == 'image':
                file_name = f"image_{int(time.time())}{os.path.splitext(file.filename)[1]}"
            else:
                file_name = file.filename
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
        await sio.emit("new_message", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "text": db_text,
            "is_from_bot": True,
            "timestamp": timestamp,
            "login": employee["login"],
            "file_path": file_path,
            "file_name": file_name,
            "file_type": file_type,
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
            SELECT m.text, m.timestamp, e.login, a.file_name
            FROM messages m
            JOIN employees e ON m.telegram_id = e.telegram_id
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

        await send_notification_to_topic(ticket_id, assigned_login or "Никто", f"Тикет переназначен на {assigned_login or 'никого'}")
        if assigned_to_id:
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
    employee: dict = Depends(get_current_user)
):
    try:
        logging.debug(f"Подтягивание истории для ticket_id={ticket_id}, telegram_id={telegram_id}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ? AND status = 'open'", (ticket_id,))
        ticket_data = cursor.fetchone()
        if not ticket_data or ticket_data[0] != telegram_id:
            conn.close()
            raise HTTPException(status_code=404, detail="Ticket not found or invalid telegram_id")

        cursor.execute(
            """
            SELECT t.ticket_id
            FROM tickets t
            WHERE t.telegram_id = ? AND t.status = 'closed'
            AND t.ticket_id NOT IN (
                SELECT fetched_ticket_id FROM history_fetched WHERE current_ticket_id = ?
            )
            ORDER BY t.created_at DESC
            LIMIT 1
            """,
            (telegram_id, ticket_id)
        )
        next_ticket = cursor.fetchone()

        if not next_ticket:
            conn.close()
            await sio.emit("no_more_history", {"ticket_id": ticket_id, "message": "Больше истории нет"})
            logging.debug("Нет больше закрытых тикетов для подтягивания")
            return {"status": "no_more_history"}

        fetched_ticket_id = next_ticket[0]

        cursor.execute(
            """
            SELECT m.message_id, m.ticket_id, m.telegram_id, m.text, m.is_from_bot, m.timestamp, e.login,
                   a.file_path, a.file_name, a.file_type
            FROM messages m
            JOIN employees e ON m.telegram_id = e.telegram_id
            LEFT JOIN attachments a ON m.message_id = a.message_id
            WHERE m.ticket_id = ?
            ORDER BY m.timestamp
            """,
            (fetched_ticket_id,)
        )
        astana_tz = pytz.timezone('Asia/Almaty')
        messages = [
            {
                "message_id": row[0],
                "ticket_id": row[1],
                "telegram_id": row[2],
                "text": f"[Тикет #{row[1]}] {row[3]}",
                "is_from_bot": bool(row[4]),
                "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S'),
                "login": row[6],
                "file_path": row[7],
                "file_name": row[8],
                "file_type": row[9]
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            "INSERT INTO history_fetched (current_ticket_id, fetched_ticket_id, fetched_at) VALUES (?, ?, ?)",
            (ticket_id, fetched_ticket_id, datetime.now(astana_tz).isoformat())
        )
        conn.commit()
        conn.close()

        for msg in messages:
            await sio.emit("new_message", msg)
            logging.debug(f"Отправлено сообщение из тикета #{fetched_ticket_id}: {msg}")

        logging.debug(f"Подтянута история из тикета #{fetched_ticket_id}, отправлено {len(messages)} сообщений")
        return {"status": "ok", "fetched_ticket_id": fetched_ticket_id, "messages_count": len(messages)}
    except Exception as e:
        logging.error(f"Ошибка при подтягивании истории: {e}")
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
    cursor.execute("SELECT telegram_id, login, is_admin FROM employees")
    employees = [
        {
            "telegram_id": row["telegram_id"],
            "login": row["login"],
            "is_admin": row["is_admin"]
        }
        for row in cursor.fetchall()
    ]
    conn.close()
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
async def search_tickets(request: Request, query: str = Query(...), employee: dict = Depends(get_current_user)):
    logging.debug(f"Поиск тикетов с запросом: {query}")
    async with db_lock:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT t.ticket_id, t.telegram_id, e.login, 
                       m.text AS last_message, m.timestamp AS last_message_timestamp,
                       a.file_path, a.file_name, a.file_type,
                       t.issue_type, t.assigned_to, e2.login AS assigned_login
                FROM tickets t 
                JOIN employees e ON t.telegram_id = e.telegram_id 
                JOIN messages m ON t.ticket_id = m.ticket_id
                LEFT JOIN attachments a ON m.message_id = a.message_id
                LEFT JOIN employees e2 ON t.assigned_to = e2.telegram_id
                WHERE m.text LIKE ?
                ORDER BY m.timestamp DESC
            """, (f"%{query}%",))
            astana_tz = pytz.timezone('Asia/Almaty')
            tickets = [
                {
                    "id": row["ticket_id"],
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
    return templates.TemplateResponse("index.html", {"request": request, "tickets": tickets, "employee": employee})

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