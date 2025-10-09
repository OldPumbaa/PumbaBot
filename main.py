import asyncio
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, BaseFilter
from aiogram.types import Message, ContentType, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from app import app, sio, message_queue, set_event_loop, send_notification_to_topic, get_setting
from dotenv import load_dotenv
import os
import logging
import sqlite3
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import pytz
import time
import re

from collections import defaultdict
media_group_collector = defaultdict(list)
media_group_timer = {}

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
BASE_URL = os.getenv("BASE_URL", "http://localhost:8081")

if not BOT_TOKEN or not ADMIN_TELEGRAM_ID:
    logging.error("BOT_TOKEN или ADMIN_TELEGRAM_ID не указаны в .env")
    raise ValueError("BOT_TOKEN and ADMIN_TELEGRAM_ID must be set in .env file")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

conn = sqlite3.connect("support.db", timeout=10)
cursor = conn.cursor()

# Define registration states
class RegistrationStates(StatesGroup):
    waiting_for_login = State()

class ChatTopicFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        is_private = message.chat.type == "private"
        logging.debug(f"Проверка сообщения: chat_id={message.chat.id}, chat_type={message.chat.type}, is_private={is_private}")
        return is_private

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
            is_admin BOOLEAN DEFAULT FALSE,
            full_name TEXT
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
    cursor.execute("PRAGMA table_info(tickets)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'is_reopened_recently' not in columns:
        logging.debug("Добавление столбца is_reopened_recently в таблицу tickets")
        cursor.execute("ALTER TABLE tickets ADD COLUMN is_reopened_recently INTEGER DEFAULT 0")
    if 'auto_close_enabled' not in columns:
        logging.debug("Добавление столбца auto_close_enabled в таблицу tickets")
        cursor.execute("ALTER TABLE tickets ADD COLUMN auto_close_enabled INTEGER DEFAULT 0")
    if 'auto_close_time' not in columns:
        logging.debug("Добавление столбца auto_close_time в таблицу tickets")
        cursor.execute("ALTER TABLE tickets ADD COLUMN auto_close_time TEXT")
    if 'notification_enabled' not in columns:
        logging.debug("Добавление столбца notification_enabled в таблицу tickets")
        cursor.execute("ALTER TABLE tickets ADD COLUMN notification_enabled INTEGER DEFAULT 0")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            telegram_id INTEGER,
            employee_telegram_id INTEGER,
            text TEXT,
            is_from_bot INTEGER,
            timestamp TEXT,
            telegram_message_id INTEGER,
            FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id),
            FOREIGN KEY (telegram_id) REFERENCES employees (telegram_id),
            FOREIGN KEY (employee_telegram_id) REFERENCES employees (telegram_id)
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # Инициализация настроек по умолчанию
    default_settings = [
        ('registration_greeting', 'Вы можете создавать тикеты, отправив сообщение или файл.'),
        ('new_ticket_response', 'Обращение принято. При необходимости прикрепите скриншот или файл с логами.'),
        ('non_working_hours_message', 'Обратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени.'),
        ('holiday_message', 'Сегодня праздничный день, поэтому ответ может занять больше времени.'),
        ('working_hours_start', '12:00'),
        ('working_hours_end', '00:00'),
        ('weekend_days', '0,6'),  # 0=воскресенье, 6=суббота
        ('is_holiday', '0')  # 0=не праздник, 1=праздник
    ]
    for key, value in default_settings:
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    conn.commit()
    conn.close()
    logging.debug("Инициализация базы данных завершена")

def is_working_hours():
    from app import get_setting
    astana_tz = pytz.timezone('Asia/Almaty')
    now = datetime.now(astana_tz)
    is_holiday = get_setting("is_holiday", "0") == "1"
    weekend_days = [int(day) for day in get_setting("weekend_days", "0,6").split(",")]
    working_hours_start = get_setting("working_hours_start", "12:00")
    working_hours_end = get_setting("working_hours_end", "00:00")
    
    logging.debug(f"Проверка рабочего времени: is_holiday={is_holiday}, now={now}, weekend_days={weekend_days}, "
                  f"working_hours_start={working_hours_start}, working_hours_end={working_hours_end}")
    
    if is_holiday:
        logging.debug("Сегодня праздничный день, возвращаем False")
        return False
    
    is_weekday = now.weekday() not in weekend_days
    logging.debug(f"Проверка дня недели: now.weekday()={now.weekday()}, is_weekday={is_weekday}")
    
    start_time = datetime.strptime(working_hours_start, "%H:%M").time()
    end_time = datetime.strptime(working_hours_end, "%H:%M").time()
    current_time = now.time()
    
    is_working_time = start_time <= current_time <= end_time
    logging.debug(f"Проверка времени: current_time={current_time}, start_time={start_time}, end_time={end_time}, "
                  f"is_working_time={is_working_time}")
    
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
async def start_command(message: Message, state: FSMContext):
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
            greeting = get_setting("registration_greeting", "Вы можете создавать тикеты, отправив сообщение или файл.")
            await message.reply(f"Добро пожаловать, {login}! {greeting}")
    else:
        await message.reply("Вы не зарегистрированы. Пожалуйста, укажите ваш логин для регистрации.")
        await state.set_state(RegistrationStates.waiting_for_login)

@dp.message(RegistrationStates.waiting_for_login)
async def process_registration_login(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    login = message.text.strip()
    full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])).strip() or f"User {telegram_id}"

    # Validate login: must be an email (contains @) and no # allowed for regular users
    if not login or len(login) > 50:
        await message.reply("Некорректный логин. Логин должен быть до 50 символов.")
        return
    if '#' in login:
        await message.reply("Символ '#' зарезервирован для администраторов. Укажите логин в формате электронной почты.")
        return
    if not re.match(r'^[\w\-\.@]+$', login) or '@' not in login:
        await message.reply("Некорректный логин. Используйте формат электронной почты (например, user@domain.com).")
        return

    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO employees (telegram_id, login, is_admin, full_name) VALUES (?, ?, ?, ?)",
            (telegram_id, login, False, full_name)
        )
        conn.commit()
        greeting = get_setting("registration_greeting", "Вы можете создавать тикеты, отправив сообщение или файл.")
        await message.reply(f"Регистрация завершена! Добро пожаловать, {login}! {greeting}")
        logging.debug(f"Зарегистрирован новый пользователь: telegram_id={telegram_id}, login={login}")
    except sqlite3.IntegrityError:
        await message.reply("Этот логин уже занят. Пожалуйста, выберите другой логин.")
        return
    except Exception as e:
        logging.error(f"Ошибка при регистрации пользователя telegram_id={telegram_id}: {e}")
        await message.reply("Произошла ошибка при регистрации. Попробуйте снова.")
        return
    finally:
        conn.close()
        await state.clear()

@dp.message(Command(commands=["myid"]))
async def my_id_command(message: Message):
    await message.reply(f"Ваш Telegram ID: {message.from_user.id}")

@dp.message(ChatTopicFilter(), F.photo)
async def handle_photo(message: Message):
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
        await message.reply("Вы не зарегистрированы. Используйте команду /start для регистрации.")
        return

    login = employee[0]
    media_group_id = message.media_group_id

    if not media_group_id:
        # Одиночное фото
        await handle_file(message, 'image')
        return

    # Собираем группу
    media_group_collector[media_group_id].append(message)

    # Отменяем предыдущий таймер
    if media_group_id in media_group_timer:
        media_group_timer[media_group_id].cancel()

    # Вспомогательная функция для задержки
    async def delayed_process(mg_id):
        await asyncio.sleep(1)  # Ждём 1 сек на сбор всех фото
        await process_media_group(mg_id)

    # Запускаем таймер
    media_group_timer[media_group_id] = asyncio.create_task(delayed_process(media_group_id))

async def process_media_group(mg_id):
    messages = media_group_collector.pop(mg_id, [])
    if not messages:
        return
    
    astana_tz = pytz.timezone('Asia/Almaty')
    timestamp = messages[0].date.astimezone(astana_tz).isoformat()
    caption = messages[0].caption if messages[0].caption else None
    telegram_id = messages[0].from_user.id
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    login = cursor.fetchone()[0]

    cursor.execute(
        "SELECT ticket_id FROM tickets WHERE telegram_id = ? AND status = 'open'",
        (telegram_id,)
    )
    ticket = cursor.fetchone()
    is_new_ticket = not ticket

    # Проверяем недавно закрытый тикет (в пределах часа)
    one_hour_ago = (datetime.now(astana_tz) - timedelta(hours=1)).isoformat()
    cursor.execute(
        """
        SELECT ticket_id FROM tickets 
        WHERE telegram_id = ? AND status = 'closed' AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (telegram_id, one_hour_ago)
    )
    recent_ticket = cursor.fetchone()

    if recent_ticket and not ticket:  # Если есть недавний закрытый тикет и нет открытого
        ticket_id = recent_ticket[0]
        cursor.execute(
            "UPDATE tickets SET status = 'open', is_reopened_recently = 1 WHERE ticket_id = ?",
            (ticket_id,)
        )
        # Удаляем старые рейтинги
        cursor.execute(
            "DELETE FROM ticket_ratings WHERE ticket_id = ?",
            (ticket_id,)
        )
        cursor.execute(
            "DELETE FROM employee_ratings WHERE ticket_id = ?",
            (ticket_id,)
        )
        conn.commit()
        logging.debug(f"Reopened ticket #{ticket_id} for telegram_id={telegram_id}")
        is_new_ticket = False
    elif is_new_ticket:
        cursor.execute(
            "INSERT INTO tickets (telegram_id, status, created_at, issue_type) VALUES (?, ?, ?, ?)",
            (telegram_id, "open", datetime.now(astana_tz).isoformat(), None)
        )
        ticket_id = cursor.lastrowid
    else:
        ticket_id = ticket[0]

    # Проверяем, включено ли автозакрытие, и отключаем, если оно активно
    cursor.execute("SELECT auto_close_enabled FROM tickets WHERE ticket_id = ?", (ticket_id,))
    if cursor.fetchone()[0]:  # Если auto_close_enabled = 1
        cursor.execute(
            "UPDATE tickets SET auto_close_enabled = 0, auto_close_time = NULL WHERE ticket_id = ?",
            (ticket_id,)
        )
        await sio.emit('auto_close_updated', {
            "ticket_id": ticket_id,
            "enabled": False,
            "auto_close_time": None
        })


    text = caption or ""
    cursor.execute(
        "INSERT INTO messages (ticket_id, telegram_id, employee_telegram_id, text, is_from_bot, timestamp, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, telegram_id, None, text, 0, timestamp, messages[0].message_id)
    )
    message_id = cursor.lastrowid

    attachments_list = []
    for i, msg in enumerate(messages):
        file_id = msg.photo[-1].file_id
        file_name_original = f"image_{int(time.time())}_{i}.jpg"
        file_name = get_unique_filename(file_name_original, directory="Uploads")
        file_path = f"Uploads/{file_name}"

        os.makedirs("Uploads", exist_ok=True)
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, file_path)

        cursor.execute(
            "INSERT INTO attachments (message_id, file_path, file_name, file_type) VALUES (?, ?, ?, ?)",
            (message_id, file_path, file_name, 'image')
        )
        attachments_list.append({"file_path": file_path, "file_name": file_name, "file_type": "image"})

    conn.commit()
    conn.close()

    await sio.emit("new_message", {
        "ticket_id": ticket_id,
        "telegram_id": telegram_id,
        "text": text,
        "is_from_bot": False,
        "timestamp": timestamp,
        "login": login,
        "message_id": message_id,
        "attachments": attachments_list
    })

    skip_standard_reply = recent_ticket and not ticket
    await sio.emit("update_tickets", {
        "ticket_id": ticket_id,
        "telegram_id": telegram_id,
        "login": login,
        "last_message": text,
        "last_message_timestamp": timestamp,
        "issue_type": None,
        "attachments": attachments_list
    })
    if is_new_ticket or skip_standard_reply:
        await send_notification_to_topic(ticket_id, login, "Новый тикет создан", is_reopened=(recent_ticket and not ticket))
        if not skip_standard_reply:
            reply_text = get_setting("new_ticket_response", "Обращение принято. При необходимости прикрепите скриншот или файл с логами.")
            if not is_working_hours():
                if get_setting("is_holiday", "0") == "1":
                    reply_text += "\n\n" + get_setting("holiday_message", "Сегодня праздничный день, поэтому ответ может занять больше времени.")
                else:
                    reply_text += "\n\n" + get_setting("non_working_hours_message", "Обратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени.")
            await messages[0].reply(reply_text)
            if skip_standard_reply:
                cursor.execute(
                    "UPDATE tickets SET is_reopened_recently = 0 WHERE ticket_id = ?",
                    (ticket_id,)
                )
                conn.commit()
                logging.debug(f"Флаг is_reopened_recently сброшен для ticket_id={ticket_id}")
        else:
            await messages[0].reply("Обращение открыто повторно.")
            logging.debug(f"Стандартный ответ пропущен для переоткрытого ticket_id={ticket_id}")

    if mg_id in media_group_timer:
        del media_group_timer[mg_id]

@dp.message(ChatTopicFilter(), F.document)
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
        await message.reply("Вы не зарегистрированы. Используйте команду /start для регистрации.")
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

    # Проверяем недавно закрытый тикет (в пределах часа)
    one_hour_ago = (datetime.now(astana_tz) - timedelta(hours=1)).isoformat()
    cursor.execute(
        """
        SELECT ticket_id FROM tickets 
        WHERE telegram_id = ? AND status = 'closed' AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (telegram_id, one_hour_ago)
    )
    recent_ticket = cursor.fetchone()

    if recent_ticket and not ticket:  # Если есть недавний закрытый тикет и нет открытого
        ticket_id = recent_ticket[0]
        cursor.execute(
            "UPDATE tickets SET status = 'open', is_reopened_recently = 1 WHERE ticket_id = ?",
            (ticket_id,)
        )
        # Удаляем старые рейтинги
        cursor.execute(
            "DELETE FROM ticket_ratings WHERE ticket_id = ?",
            (ticket_id,)
        )
        cursor.execute(
            "DELETE FROM employee_ratings WHERE ticket_id = ?",
            (ticket_id,)
        )
        conn.commit()
        logging.debug(f"Reopened ticket #{ticket_id} for telegram_id={telegram_id}")
        is_new_ticket = False
    elif is_new_ticket:
        cursor.execute(
            "INSERT INTO tickets (telegram_id, status, created_at, issue_type) VALUES (?, ?, ?, ?)",
            (telegram_id, "open", datetime.now(astana_tz).isoformat(), None)
        )
        ticket_id = cursor.lastrowid
    else:
        ticket_id = ticket[0]

    # Проверяем, включено ли автозакрытие, и отключаем, если оно активно
    cursor.execute("SELECT auto_close_enabled FROM tickets WHERE ticket_id = ?", (ticket_id,))
    if cursor.fetchone()[0]:  # Если auto_close_enabled = 1
        cursor.execute(
            "UPDATE tickets SET auto_close_enabled = 0, auto_close_time = NULL WHERE ticket_id = ?",
            (ticket_id,)
        )
        await sio.emit('auto_close_updated', {
            "ticket_id": ticket_id,
            "enabled": False,
            "auto_close_time": None
        })

    if file_type == 'image':
        file_id = message.photo[-1].file_id
        file_name_original = f"image_{int(time.time())}.jpg"
    else:
        file_id = message.document.file_id
        file_name_original = message.document.file_name if message.document.file_name else 'document'
    file_name = get_unique_filename(file_name_original, directory="Uploads")
    file_path = f"Uploads/{file_name}"

    os.makedirs("Uploads", exist_ok=True)
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, file_path)

    text = message.caption or ""
    cursor.execute(
        "INSERT INTO messages (ticket_id, telegram_id, employee_telegram_id, text, is_from_bot, timestamp, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, telegram_id, None, text, 0, timestamp, message.message_id)
    )
    message_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO attachments (message_id, file_path, file_name, file_type) VALUES (?, ?, ?, ?)",
        (message_id, file_path, file_name, file_type)
    )
    conn.commit()
    conn.close()

    attachments_list = [{"file_path": file_path, "file_name": file_name, "file_type": file_type}]

    await sio.emit("new_message", {
        "ticket_id": ticket_id,
        "telegram_id": telegram_id,
        "text": text,
        "is_from_bot": False,
        "timestamp": timestamp,
        "login": login,
        "message_id": message_id,
        "attachments": attachments_list
    })

    skip_standard_reply = recent_ticket and not ticket
    await sio.emit("update_tickets", {
        "ticket_id": ticket_id,
        "telegram_id": telegram_id,
        "login": login,
        "last_message": text,
        "last_message_timestamp": timestamp,
        "issue_type": None,
        "attachments": attachments_list
    })
    if is_new_ticket or skip_standard_reply:
        await send_notification_to_topic(ticket_id, login, "Новый тикет создан", is_reopened=(recent_ticket and not ticket))
        if not skip_standard_reply:
            reply_text = get_setting("new_ticket_response", "Обращение принято. При необходимости прикрепите скриншот или файл с логами.")
            if not is_working_hours():
                if get_setting("is_holiday", "0") == "1":
                    reply_text += "\n\n" + get_setting("holiday_message", "Сегодня праздничный день, поэтому ответ может занять больше времени.")
                else:
                    reply_text += "\n\n" + get_setting("non_working_hours_message", "Обратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени.")
            await message.reply(reply_text)
            if skip_standard_reply:
                cursor.execute(
                    "UPDATE tickets SET is_reopened_recently = 0 WHERE ticket_id = ?",
                    (ticket_id,)
                )
                conn.commit()
                logging.debug(f"Флаг is_reopened_recently сброшен для ticket_id={ticket_id}")
        else:
            await message.reply("Обращение открыто повторно.")
            logging.debug(f"Стандартный ответ пропущен для переоткрытого ticket_id={ticket_id}")

@dp.message(ChatTopicFilter(), F.voice)
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

@dp.message(ChatTopicFilter(), F.text)
async def handle_text_message(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    current_state = await state.get_state()
    if current_state == RegistrationStates.waiting_for_login.state:
        return  # Let the registration handler process this message

    if is_banned(telegram_id):
        return
    if is_muted(telegram_id):
        await message.reply("Вам временно запрещено писать в бота!")
        return

    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()

    if not employee:
        await message.reply("Вы не зарегистрированы. Используйте команду /start для регистрации.")
        conn.close()
        return

    login = employee[0]
    astana_tz = pytz.timezone('Asia/Almaty')
    is_edited = message.edit_date is not None
    timestamp = message.edit_date.astimezone(astana_tz).isoformat() if is_edited else message.date.astimezone(astana_tz).isoformat()
    text = f"{message.text} (ред.)" if is_edited else message.text

    cursor.execute(
        "SELECT ticket_id, message_id FROM messages WHERE telegram_id = ? AND telegram_message_id = ?",
        (telegram_id, message.message_id)
    )
    message_data = cursor.fetchone()
    is_new_message = not message_data

    if is_new_message:
        cursor.execute(
            "SELECT ticket_id FROM tickets WHERE telegram_id = ? AND status = 'open'",
            (telegram_id,)
        )
        ticket = cursor.fetchone()
        is_new_ticket = not ticket

        if is_new_ticket:
            # Check for recently closed ticket (within 1 hour)
            one_hour_ago = (datetime.now(astana_tz) - timedelta(hours=1)).isoformat()
            cursor.execute(
                """
                SELECT ticket_id FROM tickets 
                WHERE telegram_id = ? AND status = 'closed' AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (telegram_id, one_hour_ago)
            )
            recent_ticket = cursor.fetchone()
            if recent_ticket:
                ticket_id = recent_ticket[0]
                cursor.execute(
                    "UPDATE tickets SET status = 'open' WHERE ticket_id = ?",
                    (ticket_id,)
                )
                # Remove existing ratings for this ticket
                cursor.execute(
                    "DELETE FROM ticket_ratings WHERE ticket_id = ?",
                    (ticket_id,)
                )
                cursor.execute(
                    "DELETE FROM employee_ratings WHERE ticket_id = ?",
                    (ticket_id,)
                )
                cursor.execute(
                    "UPDATE tickets SET is_reopened_recently = 1 WHERE ticket_id = ?",
                    (ticket_id,)
                )
                conn.commit()
                logging.debug(f"Reopened ticket #{ticket_id} for telegram_id={telegram_id}")
                # Полный emit update_tickets для фронта (как для нового)
                await sio.emit("update_tickets", {
                    "ticket_id": ticket_id,
                    "telegram_id": telegram_id,
                    "login": login,
                    "last_message": text,
                    "last_message_timestamp": timestamp,
                    "issue_type": None
                })
                reply_text = "Обращение открыто повторно."
                await message.reply(reply_text)
                await send_notification_to_topic(ticket_id, login, "", is_reopened=True)
                is_new_ticket = False  # Пропускаем дублирующий блок ниже
            else:
                # Create new ticket if no recent closed ticket
                cursor.execute(
                    "INSERT INTO tickets (telegram_id, status, created_at, issue_type) VALUES (?, ?, ?, ?)",
                    (telegram_id, "open", datetime.now(astana_tz).isoformat(), None)
                )
                ticket_id = cursor.lastrowid
        else:
            ticket_id = ticket[0]

        # Проверяем, включено ли автозакрытие, и отключаем, если оно активно
        cursor.execute("SELECT auto_close_enabled FROM tickets WHERE ticket_id = ?", (ticket_id,))
        if cursor.fetchone()[0]:  # Если auto_close_enabled = 1
            cursor.execute(
                "UPDATE tickets SET auto_close_enabled = 0, auto_close_time = NULL WHERE ticket_id = ?",
                (ticket_id,)
            )
            await sio.emit('auto_close_updated', {
                "ticket_id": ticket_id,
                "enabled": False,
                "auto_close_time": None
            })

        cursor.execute(
            "INSERT INTO messages (ticket_id, telegram_id, employee_telegram_id, text, is_from_bot, timestamp, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, telegram_id, None, text, 0, timestamp, message.message_id)
        )
        message_id = cursor.lastrowid
        conn.commit()

        logging.debug(f"Отправка события {'new_message' if not is_edited else 'message_edited'} для ticket_id={ticket_id}, text={text}")
        await sio.emit("new_message" if not is_edited else "message_edited", {
            "ticket_id": ticket_id,
            "telegram_id": telegram_id,
            "text": text,
            "is_from_bot": False,
            "timestamp": timestamp,
            "login": login,
            "message_id": message_id
        })

        if is_new_ticket:
            cursor.execute(
                "SELECT is_reopened_recently FROM tickets WHERE ticket_id = ?",
                (ticket_id,)
            )
            reopen_flag = cursor.fetchone()
            skip_standard_reply = reopen_flag and reopen_flag[0] == 1

            logging.debug(f"Отправка события update_tickets для нового ticket_id={ticket_id}, skip_standard_reply={skip_standard_reply}")
            await sio.emit("update_tickets", {
                "ticket_id": ticket_id,
                "telegram_id": telegram_id,
                "login": login,
                "last_message": text,
                "last_message_timestamp": timestamp,
                "issue_type": None
            })
            await send_notification_to_topic(ticket_id, login, "Новый тикет создан")
            if not skip_standard_reply:
                reply_text = get_setting("new_ticket_response", "Обращение принято. При необходимости прикрепите скриншот или файл с логами.")
                if not is_working_hours():
                    if get_setting("is_holiday", "0") == "1":
                        reply_text += "\n\n" + get_setting("holiday_message", "Сегодня праздничный день, поэтому ответ может занять больше времени.")
                    else:
                        reply_text += "\n\n" + get_setting("non_working_hours_message", "Обратите внимание: сейчас выходные или нерабочее время. Мы стараемся оперативно отвечать с 12:00 до 00:00 по будням, но в это время ответ может занять больше времени.")
                await message.reply(reply_text)
                # Сбрасываем флаг после отправки (если был)
                if skip_standard_reply:
                    cursor.execute(
                        "UPDATE tickets SET is_reopened_recently = 0 WHERE ticket_id = ?",
                        (ticket_id,)
                    )
                    conn.commit()
                    logging.debug(f"Флаг is_reopened_recently сброшен для ticket_id={ticket_id}")
            else:
                logging.debug(f"Стандартный ответ пропущен для переоткрытого ticket_id={ticket_id}")
    else:
        ticket_id, message_id = message_data
        cursor.execute(
            "UPDATE messages SET text = ?, timestamp = ? WHERE message_id = ?",
            (text, timestamp, message_id)
        )
        conn.commit()
        logging.debug(f"Обновлено отредактированное сообщение message_id={message_id} для ticket_id={ticket_id}")
        await sio.emit("message_edited", {
            "ticket_id": ticket_id,
            "message_id": message_id,
            "telegram_id": telegram_id,
            "text": text,
            "is_from_bot": False,
            "timestamp": timestamp,
            "login": login
        })

    conn.close()

@dp.callback_query(lambda c: c.data.startswith("minichat_"))
async def handle_minichat(callback: CallbackQuery):
    ticket_id = int(callback.data.split("_")[1])
    telegram_id = callback.from_user.id
    
    conn = sqlite3.connect("support.db", timeout=10)
    cursor = conn.cursor()
    
    cursor.execute("SELECT login FROM employees WHERE telegram_id = ?", (telegram_id,))
    employee = cursor.fetchone()
    if not employee:
        await callback.message.answer("Вы не зарегистрированы.")
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
                    conn.row_factory = sqlite3.Row
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
        # Сброс флага переоткрытия для тикетов старше часа (созданных >1 часа назад)
        one_hour_ago = (datetime.now() - timedelta(hours=1)).isoformat()
        cursor.execute("UPDATE tickets SET is_reopened_recently = 0 WHERE is_reopened_recently = 1 AND created_at < ?", (one_hour_ago,))
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
    await dp.start_polling(bot, polling_timeout=30, tasks_concurrency_limit=10)

async def main():
    init_db()
    loop = asyncio.get_event_loop()
    set_event_loop(loop)
    bot_task = asyncio.create_task(run_bot())
    config = uvicorn.Config(app=app, host="0.0.0.0", port=8081, loop="asyncio")
    server = uvicorn.Server(config)
    await asyncio.gather(bot_task, server.serve())

if __name__ == "__main__":
    asyncio.run(main())