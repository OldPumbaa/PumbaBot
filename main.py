import asyncio
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ContentType, FSInputFile
from app import app, sio, message_queue, set_event_loop
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

if not BOT_TOKEN or not ADMIN_TELEGRAM_ID:
    logging.error("BOT_TOKEN или ADMIN_TELEGRAM_ID не указаны в .env")
    raise ValueError("BOT_TOKEN and ADMIN_TELEGRAM_ID must be set in .env file")

if not NOTIFICATION_CHAT_ID or not NOTIFICATION_TOPIC_ID:
    logging.warning("NOTIFICATION_CHAT_ID или NOTIFICATION_TOPIC_ID не указаны в .env, уведомления не будут отправляться")

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
            return True  # Перманентный бан
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

async def send_notification_to_topic(ticket_id: int, login: str, message: str):
    if NOTIFICATION_CHAT_ID and NOTIFICATION_TOPIC_ID:
        try:
            await bot.send_message(
                chat_id=NOTIFICATION_CHAT_ID,
                message_thread_id=int(NOTIFICATION_TOPIC_ID),
                text=f"Тикет #{ticket_id} ({login}): {message}"
            )
            logging.debug(f"Уведомление отправлено в чат {NOTIFICATION_CHAT_ID}, топик {NOTIFICATION_TOPIC_ID}: {message}")
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления в чат {NOTIFICATION_CHAT_ID}, топик {NOTIFICATION_TOPIC_ID}: {e}")

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
            await message.reply(f"Добро пожаловать, {login}! Вы администратор (техподдержка). Можете создавать тикеты и работать в веб-интерфейсе: http://localhost:8080")
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
        "INSERT INTO messages (ticket_id, telegram_id, text, is_from_bot, timestamp) VALUES (?, ?, ?, ?, ?)",
        (ticket_id, telegram_id, text, 0, timestamp)
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
        "file_type": file_type
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
            "INSERT INTO messages (ticket_id, telegram_id, text, is_from_bot, timestamp) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, telegram_id, message.text, 0, timestamp)
        )
        conn.commit()

        logging.debug(f"Отправка события new_message для ticket_id={ticket_id}, text={message.text}")
        await sio.emit("new_message", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "text": message.text,
            "is_from_bot": False,
            "timestamp": timestamp,
            "login": login
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
            logging.debug(f"Получено сообщение из очереди: telegram_id={telegram_id}, text={text}, file={file_path}")
            try:
                if file_path and file_type:
                    if not os.path.exists(file_path):
                        logging.error(f"Файл не найден: {file_path}")
                        raise FileNotFoundError(f"File not found: {file_path}")
                    logging.debug(f"Отправка файла: {file_path}, тип: {file_type}")
                    file = FSInputFile(path=file_path)
                    if file_type == 'image':
                        await bot.send_photo(
                            chat_id=telegram_id,
                            photo=file,
                            caption=text
                        )
                    else:
                        await bot.send_document(
                            chat_id=telegram_id,
                            document=file,
                            caption=text
                        )
                else:
                    await bot.send_message(chat_id=telegram_id, text=text)
                logging.debug(f"Сообщение отправлено пользователю {telegram_id}: {text}")
            except Exception as e:
                logging.error(f"Ошибка при отправке сообщения в Telegram: {e}")
                with open("error_log.txt", "a") as f:
                    f.write(f"[{datetime.now()}] Ошибка отправки {telegram_id}: {e}\n")
            finally:
                message_queue.task_done
        except Exception as e:
            logging.error(f"Ошибка в обработке очереди: {e}")
            with open("error_log.txt", "a") as f:
                f.write(f"[{datetime.now()}] Ошибка очереди: {e}\n")
        await asyncio.sleep(0.1)

async def cleanup_expired():
    while True:
        await asyncio.sleep(3600)  # Каждые 60 минут
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