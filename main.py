import asyncio
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ContentType, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from app import app, sio, message_queue, set_event_loop, send_notification_to_topic
from dotenv import load_dotenv
import os
import logging
import sqlite3
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pytz
import time
import re

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")
NOTIFICATION_CHAT_ID = os.getenv("NOTIFICATION_CHAT_ID")
NOTIFICATION_TOPIC_ID = os.getenv("NOTIFICATION_TOPIC_ID")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")

if not BOT_TOKEN or not ADMIN_TELEGRAM_ID:
    logging.error("BOT_TOKEN или ADMIN_TELEGRAM_ID не указаны в .env")
    raise ValueError("BOT_TOKEN and ADMIN_TELEGRAM_ID must be set in .env file")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def init_db():
    logging.debug("Начало инициализации базы данных")
    conn = sqlite3.connect("support.db", timeout=10)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE,
            telegram_id INTEGER UNIQUE,
            is_admin BOOLEAN DEFAULT FALSE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            status TEXT DEFAULT 'open',
            assigned_to INTEGER,
            created_at TEXT,
            issue_type TEXT,
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id),
            FOREIGN KEY (assigned_to) REFERENCES employees (telegram_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            telegram_id INTEGER,
            text TEXT,
            is_from_bot INTEGER,
            timestamp TEXT,
            telegram_message_id INTEGER,
            FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id),
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            telegram_id INTEGER,
            text TEXT,
            timestamp TEXT,
            FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id),
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history_fetched (
            current_ticket_id INTEGER,
            fetched_ticket_id INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (current_ticket_id, fetched_ticket_id),
            FOREIGN KEY (current_ticket_id) REFERENCES tickets (ticket_id),
            FOREIGN KEY (fetched_ticket_id) REFERENCES tickets (ticket_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            file_path TEXT,
            file_name TEXT,
            file_type TEXT,
            FOREIGN KEY (message_id) REFERENCES messages (message_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_token TEXT PRIMARY KEY,
            telegram_id INTEGER,
            expires_at TEXT,
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mutes (
            user_id INTEGER PRIMARY KEY,
            end_time TEXT,
            FOREIGN KEY (user_id) REFERENCES employees (telegram_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            end_time TEXT,
            FOREIGN KEY (user_id) REFERENCES employees (telegram_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quick_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            color TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticket_ratings (
            rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            telegram_id INTEGER,
            rating TEXT CHECK(rating IN ('up', 'down')),
            timestamp TEXT,
            FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id),
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id),
            UNIQUE (ticket_id, telegram_id)
        )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS employee_ratings (
        rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER,
        employee_id INTEGER,
        rating TEXT CHECK(rating IN ('up', 'down')),
        timestamp TEXT,
        FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id),
        FOREIGN KEY (employee_id) REFERENCES employees (telegram_id),
        UNIQUE (ticket_id, employee_id)
    )
""")
    cursor.execute("PRAGMA table_info(messages)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'telegram_message_id' not in columns:
        logging.debug("Добавление столбца telegram_message_id в таблицу messages")
        cursor.execute("ALTER TABLE messages ADD COLUMN telegram_message_id INTEGER")
    try:
        admin_telegram_id = int(ADMIN_TELEGRAM_ID)
        logging.debug(f"Проверка ADMIN_TELEGRAM_ID: {admin_telegram_id}")
        cursor.execute("SELECT telegram_id, is_admin FROM employees WHERE telegram_id = ?", (admin_telegram_id,))
        employee = cursor.fetchone()
        if not employee:
            logging.debug(f"Добавление администратора: telegram_id={admin_telegram_id}")
            cursor.execute(
                "INSERT INTO employees (telegram_id, login, is_admin) VALUES (?, ?, ?)",
                (admin_telegram_id, "admin", True)
            )
        elif not employee["is_admin"]:
            logging.debug(f"Обновление is_admin для telegram_id={admin_telegram_id}")
            cursor.execute(
                "UPDATE employees SET is_admin = ? WHERE telegram_id = ?",
                (True, admin_telegram_id)
            )
        else:
            logging.debug(f"Администратор уже существует: telegram_id={admin_telegram_id}, is_admin={employee['is_admin']}")
    except ValueError as e:
        logging.error(f"Некорректный ADMIN_TELEGRAM_ID в .env: {e}")
        raise
    conn.commit()
    cursor.execute("SELECT telegram_id, login, is_admin FROM employees")
    employees = cursor.fetchall()
    logging.debug(f"Содержимое таблицы employees после init_db: {employees}")
    conn.close()
    logging.debug("Инициализация базы данных завершена")

def is_working_hours():
    astana_tz = pytz.timezone('Asia/Almaty')
    now = datetime.now(astana_tz)
    is_weekday = now.weekday() < 5
    hour = now.hour
    is_working_time = 12 <= hour < 24
    return is_weekday and is_working_time

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

def is_muted(user_id: int) -> bool:
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT end_time FROM mutes WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        end_time = datetime.fromisoformat(row[0])
        if datetime.now() < end_time:
            return True
        else:
            remove_mute(user_id)
    return False

def is_banned(user_id: int) -> bool:
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT end_time FROM bans WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        if row[0] is None:
            return True
        end_time = datetime.fromisoformat(row[0])
        if datetime.now() < end_time:
            return True
        else:
            remove_ban(user_id)
    return False

def remove_mute(user_id: int):
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM mutes WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id: int):
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

@dp.message(Command(commands=["start"]))
async def start_command(message: Message):
    telegram_id = message.from_user.id
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT login, is_admin FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    conn.close()

    if employee:
        login, is_admin = employee
        if is_admin:
            await message.reply(f"Добро пожаловать, {login}! Вы администратор (техподдержка). Можете создавать тикеты и работать в веб-интерфейсе: {BASE_URL}")
        else:
            await message.reply(f"Добро пожаловать, {login}! Вы можете создавать тикеты, отправив сообщение или файл.")
    else:
        await message.reply(f"Вы не авторизованы. Попросите администратора добавить ваш Telegram ID. Ваш ID: {telegram_id}")

@dp.message(Command(commands=["myid"]))
async def my_id_command(message: Message):
    await message.reply(f"Ваш Telegram ID: {message.from_user.id}")

@dp.message(F.photo)
async def handle_photo(message: Message):
    await handle_file(message, 'image')

@dp.message(F.document)
async def handle_document(message: Message):
    await handle_file(message, 'document')

async def handle_file(message: Message, file_type: str):
    telegram_id = message.from_user.id
    if is_banned(telegram_id):
        return
    if is_muted(telegram_id):
        await message.reply("Вам временно запрещено писать в бота!")
        return

    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    conn.close()

    if not employee:
        await message.reply("Вы не авторизованы. Попросите администратора добавить ваш Telegram ID.")
        return

    login = employee[0]
    astana_tz = pytz.timezone('Asia/Almaty')
    timestamp = message.date.astimezone(astana_tz).isoformat()

    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ticket_id FROM tickets WHERE telegram_id = ? AND status = 'open'",
        (telegram_id,)
    )
    ticket = cursor.fetchone()
    is_new_ticket = not ticket

    if is_new_ticket:
        cursor.execute(
            "INSERT INTO tickets (telegram_id, status, created_at, issue_type) VALUES (?, ?, ?, ?)",
            (telegram_id, "open", datetime.now(astana_tz).isoformat(), None)
        )
        ticket_id = cursor.lastrowid
    else:
        ticket_id = ticket[0]

    file_id = message.photo[-1].file_id if file_type == 'image' else message.document.file_id
    if file_type == 'image':
        file_name = f"image_{int(time.time())}.jpg"
    else:
        file_name_original = message.document.file_name if message.document.file_name else 'document.txt'
        file_name = get_unique_filename(file_name_original, directory="Uploads")
    file_path = f"Uploads/{file_name}"

    os.makedirs("Uploads", exist_ok=True)
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, file_path)

    text = message.caption if message.caption else f"[{file_type}] {file_name}"
    cursor.execute(
        "INSERT INTO messages (ticket_id, telegram_id, text, is_from_bot, timestamp, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, telegram_id, text, 0, timestamp, message.message_id)
    )
    message_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO attachments (message_id, file_path, file_name, file_type) VALUES (?, ?, ?, ?)",
        (message_id, file_path, file_name, file_type)
    )
    conn.commit()
    conn.close()

    logging.debug(f"Отправка события new_message для ticket_id={ticket_id}, file={file_name}")
    await sio.emit("new_message", {
        "ticket_id": ticket_id,
        "telegram_id": telegram_id,
        "text": text,
        "is_from_bot": False,
        "timestamp": timestamp,
        "login": login,
        "file_path": file_path,
        "file_name": file_name,
        "file_type": file_type,
        "message_id": message_id
    })

    if is_new_ticket:
        logging.debug(f"Отправка события update_tickets для нового ticket_id={ticket_id}")
        await sio.emit("update_tickets", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "login": login,
            "last_message": text,
            "last_message_timestamp": timestamp,
            "file_path": file_path,
            "file_name": file_name,
            "file_type": file_type,
            "issue_type": None
        })
        await send_notification_to_topic(ticket_id, login, "Новый тикет создан")
        reply_text = "Обращение принято. Файл получен."
        if not is_working_hours():
            reply_text += "\n\nОбратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени."
        await message.reply(reply_text)

@dp.message(F.voice)
async def handle_voice(message: Message):
    telegram_id = message.from_user.id
    logging.debug(f"Получено голосовое сообщение от telegram_id={telegram_id}")

    if is_banned(telegram_id):
        logging.debug(f"Пользователь {telegram_id} забанен, игнорируем голосовое сообщение")
        return
    if is_muted(telegram_id):
        logging.debug(f"Пользователь {telegram_id} замучен, отправляем уведомление")
        await message.reply("Вам временно запрещено писать в бота!")
        return

    await message.reply("Извините, мы не обрабатываем голосовые сообщения. Пожалуйста, отправьте ваш запрос в текстовом виде.")
    logging.debug(f"Отправлен ответ на голосовое сообщение для telegram_id={telegram_id}")

@dp.message(F.text)
async def handle_text_message(message: Message):
    telegram_id = message.from_user.id
    if is_banned(telegram_id):
        return
    if is_muted(telegram_id):
        await message.reply("Вам временно запрещено писать в бота!")
        return

    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    conn.close()

    if not employee:
        await message.reply("Вы не авторизованы. Попросите администратора добавить ваш Telegram ID.")
        return

    login = employee[0]
    astana_tz = pytz.timezone('Asia/Almaty')
    timestamp = message.date.astimezone(astana_tz).isoformat()

    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ticket_id FROM tickets WHERE telegram_id = ? AND status = 'open'",
        (telegram_id,)
    )
    ticket = cursor.fetchone()
    is_new_ticket = not ticket

    if is_new_ticket:
        cursor.execute(
            "INSERT INTO tickets (telegram_id, status, created_at, issue_type) VALUES (?, ?, ?, ?)",
            (telegram_id, "open", datetime.now(astana_tz).isoformat(), None)
        )
        ticket_id = cursor.lastrowid
    else:
        ticket_id = ticket[0]

    cursor.execute(
        """
        SELECT message_id FROM messages 
        WHERE ticket_id = ? AND telegram_id = ? AND text = ? AND timestamp = ?
        """,
        (ticket_id, telegram_id, message.text, timestamp)
    )
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO messages (ticket_id, telegram_id, text, is_from_bot, timestamp, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, telegram_id, message.text, 0, timestamp, message.message_id)
        )
        message_id = cursor.lastrowid
        conn.commit()

        logging.debug(f"Отправка события new_message для ticket_id={ticket_id}, text={message.text}")
        await sio.emit("new_message", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "text": message.text,
            "is_from_bot": False,
            "timestamp": timestamp,
            "login": login,
            "message_id": message_id
        })

    conn.close()

    logging.debug(f"Создан/обновлён тикет #{ticket_id} для telegram_id={telegram_id}, login={login}")

    if is_new_ticket:
        logging.debug(f"Отправка события update_tickets для нового ticket_id={ticket_id}")
        await sio.emit("update_tickets", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "login": login,
            "last_message": message.text,
            "last_message_timestamp": timestamp,
            "issue_type": None
        })
        await send_notification_to_topic(ticket_id, login, "Новый тикет создан")
        reply_text = "Обращение принято. При необходимости прикрепите скриншот или файл с логами."
        if not is_working_hours():
            reply_text += "\n\nОбратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени."
        await message.reply(reply_text)

@dp.callback_query(lambda c: c.data.startswith("minichat_"))
async def handle_minichat(callback: CallbackQuery):
    ticket_id = int(callback.data.split("_")[1])
    telegram_id = callback.from_user.id
    
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    if not employee:
        await callback.message.answer("Вы не авторизованы.")
        conn.close()
        await callback.answer()
        return
    
    cursor.execute(
        """
        SELECT m.message_id, m.text, m.timestamp, e.login, a.file_path, a.file_name, a.file_type
        FROM messages m
        JOIN employees e ON m.telegram_id = e.telegram_id
        LEFT JOIN attachments a ON m.message_id = a.message_id
        WHERE m.ticket_id = ?
        ORDER BY m.timestamp
        """,
        (ticket_id,)
    )
    messages = cursor.fetchall()
    conn.close()

    if not messages:
        await callback.message.answer(f"Тикет #{ticket_id}: Сообщений пока нет.")
        await callback.answer()
        return

    astana_tz = pytz.timezone('Asia/Almaty')
    for msg in messages:
        timestamp = datetime.fromisoformat(msg["timestamp"]).astimezone(astana_tz).strftime('%Y-%m-%d %H:%M:%S')
        text = f"[{timestamp}] {msg['login']}: {msg['text']}"
        if msg["file_name"]:
            text += f" [Файл: {msg['file_name']}]"
        
        if msg["file_path"] and msg["file_type"] == "image":
            try:
                file = FSInputFile(path=msg["file_path"])
                await bot.send_photo(
                    chat_id=telegram_id,
                    photo=file,
                    caption=text
                )
            except Exception as e:
                logging.error(f"Ошибка отправки изображения {msg['file_path']}: {e}")
                await bot.send_message(
                    chat_id=telegram_id,
                    text=f"{text}\n(Не удалось загрузить изображение)"
                )
        else:
            await bot.send_message(
                chat_id=telegram_id,
                text=text
            )
    
    await callback.message.answer(f"История тикета #{ticket_id} отправлена.")
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("rate_"))
async def handle_rating(callback: CallbackQuery):
    data = callback.data.split("_")
    ticket_id = int(data[1])
    rating = data[2]  # 'up' or 'down'
    telegram_id = callback.from_user.id

    logging.debug(f"Processing rating for ticket_id={ticket_id}, rating={rating}, telegram_id={telegram_id}")

    conn = sqlite3.connect("support.db", timeout=10)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT telegram_id, status, assigned_to FROM tickets WHERE ticket_id = ?", (ticket_id,))
    ticket = cursor.fetchone()
    if not ticket or ticket["status"] != "closed" or ticket["telegram_id"] != telegram_id:
        conn.close()
        await callback.message.answer("Cannot rate this ticket.")
        await callback.answer()
        logging.warning(f"Cannot rate ticket #{ticket_id}: status={ticket['status'] if ticket else 'not found'}, telegram_id={telegram_id}")
        return

    assigned_to = ticket["assigned_to"]
    if not assigned_to:
        conn.close()
        await callback.message.answer("No support employee assigned to rate.")
        await callback.answer()
        logging.warning(f"No assigned employee for ticket #{ticket_id}")
        return

    astana_tz = pytz.timezone('Asia/Almaty')
    timestamp = datetime.now(astana_tz).isoformat()
    
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO employee_ratings (ticket_id, employee_id, rating, timestamp) VALUES (?, ?, ?, ?)",
            (ticket_id, assigned_to, rating, timestamp)
        )
        conn.commit()
        logging.debug(f"Rating {rating} saved for ticket #{ticket_id}, employee_id={assigned_to}")

        # Emit event to update employee ratings in web app
        cursor.execute(
            """
            SELECT 
                (SELECT COUNT(*) FROM employee_ratings WHERE employee_id = ? AND rating = 'up') AS thumbs_up,
                (SELECT COUNT(*) FROM employee_ratings WHERE employee_id = ? AND rating = 'down') AS thumbs_down
            """,
            (assigned_to, assigned_to)
        )
        ratings = cursor.fetchone()
        await sio.emit("employee_rated", {
            "employee_id": assigned_to,
            "thumbs_up": ratings["thumbs_up"],
            "thumbs_down": ratings["thumbs_down"]
        })
        logging.debug(f"Emitted employee_rated event for employee_id={assigned_to}, thumbs_up={ratings['thumbs_up']}, thumbs_down={ratings['thumbs_down']}")
        
        try:
            await callback.message.edit_text(
                "Обращение закрыто! Спасибо за вашу обратную связь!",
                reply_markup=None
            )
            logging.debug(f"Message for ticket_id={ticket_id} edited, keyboard removed")
        except Exception as e:
            logging.error(f"Error editing message for ticket_id={ticket_id}: {e}")
            await callback.message.answer("Rating saved, but failed to update message.")
        
        await callback.answer("Rating submitted!")
    except Exception as e:
        logging.error(f"Error saving rating for ticket_id={ticket_id}: {e}")
        await callback.message.answer("Error saving rating.")
        await callback.answer("Error!")
    finally:
        conn.close()

async def process_message_queue():
    logging.debug("Запущена обработка очереди сообщений")
    while True:
        try:
            logging.debug("Ожидаем сообщение в очереди...")
            data = await message_queue.get()
            telegram_id = data["telegram_id"]
            text = data["text"]
            file_path = data.get("file_path")
            file_type = data.get("file_type")
            message_id = data.get("message_id")
            telegram_message_id = data.get("telegram_message_id")
            ticket_id = data.get("ticket_id")  # Extract ticket_id
            logging.debug(f"Получено сообщение из очереди: telegram_id={telegram_id}, text={text}, file={file_path}, message_id={message_id}, telegram_message_id={telegram_message_id}, ticket_id={ticket_id}")
            
            telegram_message = None
            try:
                if telegram_message_id:
                    logging.debug(f"Редактирование сообщения telegram_message_id={telegram_message_id}")
                    conn = sqlite3.connect("support.db", timeout=10)
                    cursor = conn.cursor()
                    cursor.execute("SELECT file_type FROM attachments WHERE message_id = ?", (message_id,))
                    attachment = cursor.fetchone()
                    conn.close()
                    
                    if attachment and attachment["file_type"] in ["image", "document"]:
                        await bot.edit_message_caption(
                            chat_id=telegram_id,
                            message_id=telegram_message_id,
                            caption=text
                        )
                    else:
                        await bot.edit_message_text(
                            chat_id=telegram_id,
                            message_id=telegram_message_id,
                            text=text
                        )
                    logging.debug(f"Сообщение telegram_message_id={telegram_message_id} отредактировано в Telegram")
                else:
                    if file_path and file_type:
                        if not os.path.exists(file_path):
                            logging.error(f"Файл не найден: {file_path}")
                            raise FileNotFoundError(f"File not found: {file_path}")
                        logging.debug(f"Отправка файла: {file_path}, тип: {file_type}")
                        file = FSInputFile(path=file_path)
                        if file_type == 'image':
                            telegram_message = await bot.send_photo(
                                chat_id=telegram_id,
                                photo=file,
                                caption=text
                            )
                        else:
                            telegram_message = await bot.send_document(
                                chat_id=telegram_id,
                                document=file,
                                caption=text
                            )
                    else:
                        if ticket_id and "ваше обращение закрыто. вы можете оценить работу техподдержки ниже." in text.lower():
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                                [
                                    InlineKeyboardButton(text="👍", callback_data=f"rate_{ticket_id}_up"),
                                    InlineKeyboardButton(text="👎", callback_data=f"rate_{ticket_id}_down")
                                ]
                            ])
                            telegram_message = await bot.send_message(
                                chat_id=telegram_id,
                                text=text,
                                reply_markup=keyboard
                            )
                            logging.debug(f"Sent closure message with rating buttons for ticket_id={ticket_id}")
                        else:
                            telegram_message = await bot.send_message(chat_id=telegram_id, text=text)
                
                    if telegram_message and message_id:
                        conn = sqlite3.connect("support.db", timeout=10)
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE messages SET telegram_message_id = ? WHERE message_id = ?",
                            (telegram_message.message_id, message_id)
                        )
                        conn.commit()
                        conn.close()
                        logging.debug(f"Сохранён telegram_message_id={telegram_message.message_id} для message_id={message_id}")
                
                logging.debug(f"Сообщение отправлено/отредактировано пользователю {telegram_id}: {text}")
            except Exception as e:
                logging.error(f"Ошибка при отправке/редактировании сообщения в Telegram: {e}")
                with open("error_log.txt", "a") as f:
                    f.write(f"[{datetime.now()}] Ошибка обработки {telegram_id}: {e}\n")
            finally:
                message_queue.task_done()
        except Exception as e:
            logging.error(f"Ошибка в обработке очереди: {e}")
            with open("error_log.txt", "a") as f:
                f.write(f"[{datetime.now()}] Ошибка очереди: {e}\n")
        await asyncio.sleep(0.1)

async def cleanup_expired():
    while True:
        await asyncio.sleep(3600)
        conn = sqlite3.connect("support.db", timeout=10)
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute("DELETE FROM mutes WHERE end_time < ?", (now,))
        cursor.execute("DELETE FROM bans WHERE end_time IS NOT NULL AND end_time < ?", (now,))
        conn.commit()
        conn.close()

async def on_startup():
    init_db()
    loop = asyncio.get_event_loop()
    set_event_loop(loop)
    asyncio.create_task(process_message_queue())
    asyncio.create_task(cleanup_expired())
    logging.debug("Бот запущен")

async def run_bot():
    await on_startup()
    await dp.start_polling(bot)

async def main():
    init_db()
    loop = asyncio.get_event_loop()
    set_event_loop(loop)
    bot_task = asyncio.create_task(run_bot())
    config = uvicorn.Config(app=app, host="0.0.0.0", port=8080, loop="asyncio")
    server = uvicorn.Server(config)
    await asyncio.gather(bot_task, server.serve())

if __name__ == "__main__":
    asyncio.run(main())