"""
MVP Telegram CRM для тренеров — один файл
Зависимости:
    pip install aiogram==2.25 python-dateutil

Запуск:
    export BOT_TOKEN="твой_токен"   # Linux / macOS
    set BOT_TOKEN="твой_токен"      # Windows (cmd)
    python telegram_crm_bot.py

Примечание:
- Токен не хранится в коде по безопасности — используй BOT_TOKEN.
- Файловая БД: crm.db (SQLite) рядом с файлом.
"""

import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from dateutil import parser as dateparser

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    logger.warning("Переменная окружения BOT_TOKEN не установлена. Подставь токен в переменной окружения перед запуском.")
    # Не ставим явный токен сюда — безопасность.

bot = Bot(token=API_TOKEN)  # если API_TOKEN is None — aiogram выдаст ошибку при старте
dp = Dispatcher(bot)

DB_FILE = 'crm.db'

# --- Инициализация БД ---
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute('''
CREATE TABLE IF NOT EXISTS trainers (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER UNIQUE,
    name TEXT,
    created_at TEXT
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    notes TEXT,
    balance REAL DEFAULT 0,
    chat_id INTEGER
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    client_id INTEGER,
    datetime TEXT,
    status TEXT DEFAULT 'planned',
    comment TEXT,
    remind24_sent INTEGER DEFAULT 0,
    remind2_sent INTEGER DEFAULT 0,
    FOREIGN KEY(client_id) REFERENCES clients(id)
)
''')

cur.execute('''
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    client_id INTEGER,
    amount REAL,
    date TEXT,
    note TEXT,
    FOREIGN KEY(client_id) REFERENCES clients(id)
)
''')

conn.commit()

# --- Вспомогательные функции ---


def parse_dt(text: str) -> datetime:
    """
    Парсит дату/время. Принимает гибко: "DD.MM.YYYY HH:MM", "DD.MM HH:MM", или общие форматы.
    Возвращает naive datetime в UTC-подходе (мы используем UTC для хранения).
    """
    text = text.strip()
    # Попробуем dateutil (день-перво)
    try:
        dt = dateparser.parse(text, dayfirst=True)
        if dt is None:
            raise ValueError
        # Если нет информации о временной зоне — считаем локальным и переводим в UTC naive (тут оставляем naive)
        return dt
    except Exception:
        # Попробуем формат без года: DD.MM HH:MM
        try:
            parts = text.split()
            if len(parts) >= 2:
                datepart = parts[0]
                timepart = parts[1]
                if datepart.count('.') == 1:  # DD.MM
                    year = datetime.now().year
                    text2 = f"{datepart}.{year} {timepart}"
                    dt = datetime.strptime(text2, "%d.%m.%Y %H:%M")
                    return dt
        except Exception:
            pass
    raise ValueError("Не удалось распознать дату. Используй, например: 12.08.2025 18:00 или 12.08 18:00")


def get_trainer_chat_ids():
    cur.execute("SELECT chat_id FROM trainers")
    return [r[0] for r in cur.fetchall()]


def ensure_trainer(chat_id: int, name: str = None):
    cur.execute("SELECT id FROM trainers WHERE chat_id = ?", (chat_id,))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO trainers (chat_id, name, created_at) VALUES (?, ?, ?)",
                    (chat_id, name or 'trainer', datetime.utcnow().isoformat()))
        conn.commit()


# --- Обработчики команд ---


@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я CRM-бот для тренеров.\nНапиши /help для списка команд.")


@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    await message.answer(
        "/trainer_register — зарегистрировать этот чат как тренер\n"
        "/add_client Имя Телефон [примечание]\n"
        "/clients — список клиентов\n"
        "/client <id> — карточка клиента\n"
        "/link_client <id> — связать этот чат с клиентом\n"
        "/add_session <client_id> <DD.MM[.YYYY] HH:MM> [комментарий]\n"
        "/schedule [days] — ближайшие тренировки (по умолчанию 30)\n"
        "/complete_session <session_id>\n"
        "/paid <client_id> <amount> [комментарий]\n"
        "/debts — список должников\n"
        "/stats [days]\n"
        "/help"
    )


@dp.message_handler(commands=['trainer_register'])
async def cmd_trainer_register(message: types.Message):
    ensure_trainer(message.chat.id, message.from_user.full_name)
    await message.answer(f"Чат {message.chat.id} зарегистрирован как тренер.")


@dp.message_handler(commands=['add_client'])
async def cmd_add_client(message: types.Message):
    # /add_client Имя Телефон [примечание]
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer("Используй: /add_client Имя Телефон [примечание]")
        return
    _, name, phone = parts[:3]
    notes = parts[3] if len(parts) == 4 else ""
    cur.execute("INSERT INTO clients (name, phone, notes) VALUES (?, ?, ?)", (name, phone, notes))
    conn.commit()
    cid = cur.lastrowid
    await message.answer(f"Клиент добавлен: {name} (id={cid})")


@dp.message_handler(commands=['clients', 'list_clients'])
async def cmd_list_clients(message: types.Message):
    cur.execute("SELECT id, name, phone, balance FROM clients ORDER BY id")
    rows = cur.fetchall()
    if not rows:
        await message.answer("Клиентов пока нет.")
        return
    text = "\n".join([f"{r[0]}. {r[1]} — {r[2]} — balance: {r[3]:.2f}" for r in rows])
    await message.answer(text)


@dp.message_handler(commands=['client'])
async def cmd_client(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /client <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    cur.execute("SELECT id, name, phone, notes, balance, chat_id FROM clients WHERE id = ?", (cid,))
    r = cur.fetchone()
    if not r:
        await message.answer("Клиент не найден")
        return
    text = f"ID: {r[0]}\nИмя: {r[1]}\nТелефон: {r[2]}\nПримечание: {r[3]}\nБаланс: {r[4]:.2f}\nChat_id: {r[5]}"
    cur.execute("SELECT id, datetime, status, comment FROM sessions WHERE client_id = ? ORDER BY datetime DESC LIMIT 5", (cid,))
    sessions = cur.fetchall()
    if sessions:
        text += "\n\nПоследние тренировки:"
        for s in sessions:
            text += f"\n{s[0]}. {s[1]} — {s[2]} — {s[3] or ''}"
    cur.execute("SELECT id, amount, date, note FROM payments WHERE client_id = ? ORDER BY date DESC LIMIT 5", (cid,))
    pays = cur.fetchall()
    if pays:
        text += "\n\nПоследние платежи:"
        for p in pays:
            text += f"\n{p[0]}. {p[1]:.2f} — {p[2]} — {p[3] or ''}"
    await message.answer(text)


@dp.message_handler(commands=['link_client'])
async def cmd_link_client(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /link_client <id>")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    cur.execute("UPDATE clients SET chat_id = ? WHERE id = ?", (message.chat.id, cid))
    conn.commit()
    await message.answer(f"Клиент {cid} привязан к этому чату (chat_id={message.chat.id}).")


@dp.message_handler(commands=['add_session'])
async def cmd_add_session(message: types.Message):
    # /add_session <client_id> <date time> [comment]
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer("Используй: /add_session <client_id> <DD.MM[.YYYY] HH:MM> [комментарий]")
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await message.answer("Неверный client_id")
        return
    # Попробуем аккуратно собрать дату+время (могут быть пробелы)
    dt_part = parts[2]
    comment = parts[3] if len(parts) == 4 else ""
    # Если дата часть содержит только день.месяц, возможно время — в комментарии первым токеном
    try:
        dt = parse_dt(dt_part if len(parts) == 3 else dt_part + ' ' + (comment.split()[0] if comment else ''))
        # если первый токен комментария был временем — уберём его
        if comment:
            first = comment.split()[0]
            try:
                _ = parse_dt(first)
                comment = ' '.join(comment.split()[1:])
            except Exception:
                pass
    except Exception:
        # пробуем объединить parts[2] + ' ' + parts[3]
        try:
            dt = parse_dt(parts[2] + ' ' + (parts[3] if len(parts) >= 4 else ''))
            comment = ''
        except Exception:
            await message.answer("Не удалось распознать дату. Используй, например: 12.08.2025 18:00")
            return
    dt_iso = dt.isoformat()
    cur.execute("INSERT INTO sessions (client_id, datetime, comment) VALUES (?, ?, ?)", (cid, dt_iso, comment))
    conn.commit()
    sid = cur.lastrowid
    await message.answer(f"Сессия добавлена (id={sid}) для клиента {cid} на {dt.strftime('%d.%m.%Y %H:%M')}.")


@dp.message_handler(commands=['schedule'])
async def cmd_schedule(message: types.Message):
    parts = message.text.split(maxsplit=1)
    days = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 30
    now = datetime.utcnow()
    end = now + timedelta(days=days)
    cur.execute("SELECT s.id, s.client_id, s.datetime, s.status, c.name FROM sessions s LEFT JOIN clients c ON s.client_id=c.id WHERE s.datetime BETWEEN ? AND ? ORDER BY s.datetime", (now.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    if not rows:
        await message.answer("Нет тренировок в выбранном диапазоне.")
        return
    text = ""
    for r in rows:
        dt = datetime.fromisoformat(r[2])
        text += f"{r[0]}. {dt.strftime('%d.%m.%Y %H:%M')} — {r[4] or ('Клиент '+str(r[1]))} — {r[3]}\n"
    await message.answer(text)


@dp.message_handler(commands=['complete_session'])
async def cmd_complete_session(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /complete_session <id>")
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await message.answer("Неверный id")
        return
    cur.execute("UPDATE sessions SET status = ? WHERE id = ?", ('completed', sid))
    conn.commit()
    await message.answer(f"Сессия {sid} помечена как выполненная.")


@dp.message_handler(commands=['paid'])
async def cmd_paid(message: types.Message):
    # /paid <client_id> <amount> [note]
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer("Используй: /paid <client_id> <amount> [комментарий]")
        return
    try:
        cid = int(parts[1])
        amount = float(parts[2])
    except ValueError:
        await message.answer("Неверные параметры. Пример: /paid 3 1500")
        return
    note = parts[3] if len(parts) == 4 else ""
    now = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO payments (client_id, amount, date, note) VALUES (?, ?, ?, ?)", (cid, amount, now, note))
    cur.execute("UPDATE clients SET balance = balance + ? WHERE id = ?", (amount, cid))
    conn.commit()
    await message.answer(f"Платеж записан: client={cid}, amount={amount:.2f}")


@dp.message_handler(commands=['debts'])
async def cmd_debts(message: types.Message):
    cur.execute("SELECT id, name, phone, balance FROM clients WHERE balance < 0 ORDER BY balance")
    rows = cur.fetchall()
    if not rows:
        await message.answer("Должников нет.")
        return
    text = "\n".join([f"{r[0]}. {r[1]} — {r[2]} — balance: {r[3]:.2f}" for r in rows])
    await message.answer(text)


@dp.message_handler(commands=['stats'])
async def cmd_stats(message: types.Message):
    parts = message.text.split(maxsplit=1)
    days = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 30
    since = datetime.utcnow() - timedelta(days=days)
    cur.execute("SELECT COUNT(*) FROM sessions WHERE status = ? AND datetime >= ?", ('completed', since.isoformat()))
    sessions_done = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE date >= ?", (since.isoformat(),))
    income = cur.fetchone()[0]
    await message.answer(f"Статистика за последние {days} дней:\nПроведено тренировок: {sessions_done}\nПолучено оплат: {income:.2f}")


# --- Фоновая задача: reminders ---


async def reminders_loop():
    logger.info("Reminders loop started")
    while True:
        try:
            now = datetime.utcnow()
            # Промежутки для поиска: интервал 60 секунд (выполняется каждую минуту),
            # поэтому ищем с запасом 61 секунды.
            t24_from = now + timedelta(hours=24)
            t24_to = t24_from + timedelta(seconds=61)
            t2_from = now + timedelta(hours=2)
            t2_to = t2_from + timedelta(seconds=61)

            # 24h reminders
            cur.execute(
                "SELECT s.id, s.client_id, s.datetime, s.comment, c.chat_id, c.name "
                "FROM sessions s LEFT JOIN clients c ON s.client_id=c.id "
                "WHERE s.remind24_sent = 0 AND s.status = ? AND s.datetime BETWEEN ? AND ?",
                ('planned', t24_from.isoformat(), t24_to.isoformat())
            )
            rows24 = cur.fetchall()
            for r in rows24:
                sid, cid, dt_iso, comment, client_chat, client_name = r
                dt = datetime.fromisoformat(dt_iso)
                txt = f"Напоминание: тренировка {dt.strftime('%d.%m.%Y %H:%M')} — {client_name or cid} (id={cid})."
                if comment:
                    txt += "\n" + comment
                # отправляем тренерам
                trainers = get_trainer_chat_ids()
                for t in trainers:
                    try:
                        await bot.send_message(t, "За 24 часа — " + txt)
                    except Exception:
                        logger.exception("Failed send 24h to trainer")
                # отправляем клиенту (если привязан)
                if client_chat:
                    try:
                        await bot.send_message(client_chat, f"Привет! Напоминаем о тренировке {dt.strftime('%d.%m.%Y %H:%M')}.")
                    except Exception:
                        logger.exception("Failed send 24h to client")
                # помечаем
                cur.execute("UPDATE sessions SET remind24_sent = 1 WHERE id = ?", (sid,))
                conn.commit()

            # 2h reminders
            cur.execute(
                "SELECT s.id, s.client_id, s.datetime, s.comment, c.chat_id, c.name "
                "FROM sessions s LEFT JOIN clients c ON s.client_id=c.id "
                "WHERE s.remind2_sent = 0 AND s.status = ? AND s.datetime BETWEEN ? AND ?",
                ('planned', t2_from.isoformat(), t2_to.isoformat())
            )
            rows2 = cur.fetchall()
            for r in rows2:
                sid, cid, dt_iso, comment, client_chat, client_name = r
                dt = datetime.fromisoformat(dt_iso)
                txt = f"Напоминание: тренировка {dt.strftime('%d.%m.%Y %H:%M')} — {client_name or cid} (id={cid})."
                if comment:
                    txt += "\n" + comment
                trainers = get_trainer_chat_ids()
                for t in trainers:
                    try:
                        await bot.send_message(t, "За 2 часа — " + txt)
                    except Exception:
                        logger.exception("Failed send 2h to trainer")
                if client_chat:
                    try:
                        await bot.send_message(client_chat, f"Привет! Напоминаем о тренировке через 2 часа: {dt.strftime('%d.%m.%Y %H:%M')}.")
                    except Exception:
                        logger.exception("Failed send 2h to client")
                cur.execute("UPDATE sessions SET remind2_sent = 1 WHERE id = ?", (sid,))
                conn.commit()

        except Exception:
            logger.exception("Error in reminders loop")
        await asyncio.sleep(60)  # проверяем каждую минуту


# --- on_startup для правильного запуска фоновой задачи ---


async def on_startup(dp):
    # старт background task
    asyncio.create_task(reminders_loop())
    logger.info("on_startup finished — reminders loop scheduled.")


if __name__ == '__main__':
    # Запуск бота с передачей on_startup
    logger.info("Bot is starting...")
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)