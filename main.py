import asyncio
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ContentType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from app import app, sio, message_queue, set_event_loop, socketio_app
from dotenv import load_dotenv
import os
import logging
import sqlite3
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytz

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)

load_dotenv()
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

# Определяем состояния для FSM
class Registration(StatesGroup):
    waiting_for_login = State()

def init_db():
    conn = sqlite3.connect("support.db")
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
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id),
            FOREIGN KEY (assigned_to) REFERENCES employees (id)
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
    conn.commit()
    conn.close()

def is_working_hours():
    astana_tz = pytz.timezone('Asia/Almaty')
    now = datetime.now(astana_tz)
    is_weekday = now.weekday() < 5
    hour = now.hour
    is_working_time = 12 <= hour < 24
    return is_weekday and is_working_time

@dp.message(Command(commands=["start"]))
async def start_command(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    conn = sqlite3.connect("support.db")
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    conn.close()

    if employee:
        await message.reply(f"Добро пожаловать, {employee[0]}! Вы уже зарегистрированы.")
    else:
        await message.reply("Пожалуйста, введите ваш рабочий логин.")
        await state.set_state(Registration.waiting_for_login)

@dp.message(Registration.waiting_for_login)
async def process_login(message: Message, state: FSMContext):
    login = message.text.strip()
    telegram_id = message.from_user.id

    if not login:
        await message.reply("Логин не может быть пустым. Пожалуйста, введите ваш рабочий логин.")
        return

    conn = sqlite3.connect("support.db")
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO employees (telegram_id, login, is_admin) VALUES (?, ?, ?)",
            (telegram_id, login, False)
        )
        conn.commit()
        await message.reply(f"Регистрация завершена! Ваш логин: {login}")
        await state.clear()
    except sqlite3.IntegrityError:
        await message.reply("Этот логин уже занят. Пожалуйста, выберите другой.")
    finally:
        conn.close()

@dp.message(F.content_type == ContentType.PHOTO)
async def handle_photo(message: Message):
    await handle_file(message, 'image')

@dp.message(F.content_type == ContentType.DOCUMENT)
async def handle_document(message: Message):
    await handle_file(message, 'document')

async def handle_file(message: Message, file_type: str):
    telegram_id = message.from_user.id
    conn = sqlite3.connect("support.db")
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()

    if not employee:
        await message.reply("Вы не зарегистрированы. Пожалуйста, используйте команду /start и введите логин.")
        conn.close()
        return

    login = employee[0]
    astana_tz = pytz.timezone('Asia/Almaty')
    timestamp = message.date.astimezone(astana_tz).isoformat()

    cursor.execute(
        "SELECT ticket_id FROM tickets WHERE telegram_id = ? AND status = 'open'",
        (telegram_id,)
    )
    ticket = cursor.fetchone()
    is_new_ticket = not ticket

    if is_new_ticket:
        cursor.execute(
            "INSERT INTO tickets (telegram_id, status, created_at) VALUES (?, ?, ?)",
            (telegram_id, "open", datetime.now(astana_tz).isoformat())
        )
        ticket_id = cursor.lastrowid
    else:
        ticket_id = ticket[0]

    file_id = message.photo[-1].file_id if file_type == 'image' else message.document.file_id
    file_name = f"{file_id}.{file_type}"
    file_path = f"uploads/{file_name}"

    os.makedirs("uploads", exist_ok=True)
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, file_path)

    cursor.execute(
        "INSERT INTO messages (ticket_id, telegram_id, text, is_from_bot, timestamp) VALUES (?, ?, ?, ?, ?)",
        (ticket_id, telegram_id, f"[{file_type}] {file_name}", 0, timestamp)
    )
    message_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO attachments (message_id, file_path, file_name, file_type) VALUES (?, ?, ?, ?)",
        (message_id, file_path, file_name, file_type)
    )
    conn.commit()
    conn.close()

    await sio.emit("new_message", {
        "ticket_id": ticket_id,
        "telegram_id": telegram_id,
        "text": f"[{file_type}] {file_name}",
        "is_from_bot": False,
        "timestamp": timestamp,
        "login": login,
        "file_path": file_path,
        "file_type": file_type
    })

    if is_new_ticket:
        await sio.emit("update_tickets", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "login": login,
            "last_message": f"[{file_type}] {file_name}",
            "last_message_timestamp": timestamp
        })
        reply_text = "Обращение принято. Файл получен."
        if not is_working_hours():
            reply_text += "\n\nОбратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени."
        await message.reply(reply_text)

@dp.message(F.content_type == ContentType.TEXT)
async def handle_text_message(message: Message):
    telegram_id = message.from_user.id
    conn = sqlite3.connect("support.db")
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()

    if not employee:
        await message.reply("Вы не зарегистрированы. Пожалуйста, используйте команду /start и введите логин.")
        conn.close()
        return

    login = employee[0]
    astana_tz = pytz.timezone('Asia/Almaty')
    timestamp = message.date.astimezone(astana_tz).isoformat()

    cursor.execute(
        "SELECT ticket_id FROM tickets WHERE telegram_id = ? AND status = 'open'",
        (telegram_id,)
    )
    ticket = cursor.fetchone()
    is_new_ticket = not ticket

    if is_new_ticket:
        cursor.execute(
            "INSERT INTO tickets (telegram_id, status, created_at) VALUES (?, ?, ?)",
            (telegram_id, "open", datetime.now(astana_tz).isoformat())
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
        await sio.emit("update_tickets", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "login": login,
            "last_message": message.text,
            "last_message_timestamp": timestamp
        })
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
            logging.debug(f"Получено сообщение из очереди: telegram_id={telegram_id}, text={text}")
            try:
                await bot.send_message(chat_id=telegram_id, text=text)
                logging.debug(f"Сообщение отправлено пользователю {telegram_id}: {text}")
            except Exception as e:
                logging.error(f"Ошибка при отправке сообщения в Telegram: {e}")
                with open("error_log.txt", "a") as f:
                    f.write(f"Ошибка отправки {telegram_id}: {e}\n")
            finally:
                message_queue.task_done()
        except Exception as e:
            logging.error(f"Ошибка в обработке очереди: {e}")
            with open("error_log.txt", "a") as f:
                f.write(f"Ошибка очереди: {e}\n")
            await asyncio.sleep(1)

async def on_startup():
    print("Бот запущен!")
    asyncio.create_task(process_message_queue())

async def run_bot():
    await on_startup()
    await dp.start_polling(bot)

async def main():
    init_db()
    loop = asyncio.get_event_loop()
    set_event_loop(loop)
    bot_task = asyncio.create_task(run_bot())
    config = uvicorn.Config(app=socketio_app, host="0.0.0.0", port=8080, loop="asyncio")
    server = uvicorn.Server(config)
    await asyncio.gather(bot_task, server.serve())

if __name__ == "__main__":
    asyncio.run(main())