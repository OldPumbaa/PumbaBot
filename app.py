from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import socketio
import sqlite3
from datetime import datetime
import asyncio
import logging
from dotenv import load_dotenv
import os
import pytz
import time
import re

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)

load_dotenv()
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socketio_app = socketio.ASGIApp(sio, other_asgi_app=app)

message_queue = asyncio.Queue()
loop = None

def set_event_loop(event_loop):
    global loop
    loop = event_loop
    logging.debug(f"Установлен цикл событий: {loop}")

db_lock = asyncio.Lock()

def get_db_connection():
    conn = sqlite3.connect("support.db", timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def get_unique_filename(filename: str, directory: str = "uploads"):
    """Генерирует уникальное имя файла, заменяя специальные символы на '_' и добавляя суффикс при конфликте."""
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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    logging.debug("Запрос к главной странице /")
    async with db_lock:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT t.ticket_id, t.telegram_id, e.login, 
                       m.text AS last_message, m.timestamp AS last_message_timestamp,
                       a.file_path, a.file_name, a.file_type
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
                WHERE t.status = 'open'
            """)
            astana_tz = pytz.timezone('Asia/Almaty')
            tickets = [
                {
                    "id": row["ticket_id"],
                    "login": row["login"],
                    "last_message": row["last_message"],
                    "last_message_timestamp": datetime.fromisoformat(row["last_message_timestamp"]).astimezone(astana_tz).isoformat() if row["last_message_timestamp"] else None,
                    "file_path": row["file_path"],
                    "file_name": row["file_name"],
                    "file_type": row["file_type"]
                }
                for row in cursor.fetchall()
            ]
    return templates.TemplateResponse("index.html", {"request": request, "tickets": tickets})

@app.get("/ticket/{ticket_id}", response_class=HTMLResponse)
async def ticket(request: Request, ticket_id: int):
    logging.debug(f"Запрос к тикету #{ticket_id}")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
    ticket_data = cursor.fetchone()
    if not ticket_data:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")
    telegram_id = ticket_data[0]
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
            "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).isoformat(),
            "login": row[6],
            "file_path": row[7],
            "file_name": row[8],
            "file_type": row[9]
        }
        for row in cursor.fetchall()
    ]
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    login = employee[0] if employee else "Unknown"
    conn.close()
    return templates.TemplateResponse(
        "ticket.html",
        {"request": request, "ticket_id": ticket_id, "messages": messages, "telegram_id": telegram_id, "login": login}
    )

@app.post("/send_message")
async def send_message(
    ticket_id: int = Form(...),
    telegram_id: int = Form(...),
    text: str = Form(None),
    file: UploadFile = File(None)
):
    try:
        logging.debug(f"Отправка сообщения: ticket_id={ticket_id}, telegram_id={telegram_id}, text={text}, file={file.filename if file else None}")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
        employee = cursor.fetchone()
        if not employee:
            conn.close()
            logging.error(f"Неверный telegram_id: {telegram_id}")
            raise HTTPException(status_code=400, detail="Invalid telegram_id")

        astana_tz = pytz.timezone('Asia/Almaty')
        timestamp = datetime.now(astana_tz).isoformat()
        
        # Текст для базы данных и веб-интерфейса (для сотрудников техподдержки)
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
            file_name = get_unique_filename(file_name, directory="uploads")
            file_path = f"uploads/{file_name}"
            try:
                os.makedirs("uploads", exist_ok=True)
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

        conn.commit()
        conn.close()
        logging.debug(f"Сообщение сохранено в базе данных: ticket_id={ticket_id}")

        if loop is None:
            logging.error("Цикл событий не инициализирован")
            raise HTTPException(status_code=500, detail="Event loop not initialized")

        # Текст для отправки в Telegram (без автоматической подписи)
        telegram_text = text if text else ""
        queue_data = {"telegram_id": telegram_id, "text": telegram_text}
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
            "login": "Bot",
            "file_path": file_path,
            "file_name": file_name,
            "file_type": file_type
        })
        logging.debug("Уведомление через SocketIO отправлено")

        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/close_ticket")
async def close_ticket(ticket_id: int = Form(...)):
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
            "text": "Ваше обращение закрыто"
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
async def assign_ticket_endpoint(ticket_id: int = Form(...), employee_id: int = Form(...)):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tickets SET assigned_to = ? WHERE ticket_id = ?", (employee_id, ticket_id))
    cursor.execute("SELECT login FROM employees WHERE id = ?", (employee_id,))
    employee = cursor.fetchone()
    conn.commit()
    conn.close()
    await sio.emit("ticket_assigned", {"ticket_id": ticket_id, "assigned_to": employee[0] if employee else "Unknown"})
    return {"status": "ok", "assigned_to": employee[0] if employee else "Unknown"}

@app.post("/cleanup")
async def cleanup():
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
async def fetch_telegram_history(ticket_id: int = Form(...), telegram_id: int = Form(...)):
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
                "timestamp": datetime.fromisoformat(row[5]).astimezone(astana_tz).isoformat(),
                "login": row[6],
                "file_path": row[7],
                "file_name": row[8],
                "file_type": row[9]
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            INSERT INTO history_fetched (current_ticket_id, fetched_ticket_id, fetched_at)
            VALUES (?, ?, ?)
            """,
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

@sio.event
async def new_ticket(sid, data):
    logging.debug(f"Получено событие new_ticket: {data}")
    await sio.emit("update_tickets", data)
    logging.debug("Событие update_tickets отправлено")