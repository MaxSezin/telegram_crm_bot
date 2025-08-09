"""
Telegram CRM для тренеров — MVP (один файл)
Автор: сгенерировано ChatGPT

Функции:
- Управление клиентами (добавление, просмотр, редактирование)
- Управление сессиями/тренировками (добавление, список, пометка о выполнении)
- Учёт платежей (запись платежа, задолженности)
- Напоминания за 24h и за 2h тренеру и клиенту (если привязан chat_id клиента)
- Простая статистика

Запуск:
1) Установить зависимости: pip install aiogram==2.25 python-dateutil
2) Установить переменную окружения BOT_TOKEN
   или вписать токен в переменную API_TOKEN внизу
3) Запустить: python telegram_crm_bot.py

Форматы дат в командах: DD.MM.YYYY HH:MM (например: 12.08.2025 18:00)
Альтернативно: DD.MM HH:MM — будет подставлен текущий год.

Команды (MVP):
/trainer_register — зарегистрировать текущий чат как тренер (админ)
/add_client Имя Телефон [примечание]
/clients — список клиентов
/client <id> — карточка клиента
/link_client <id> — связать текущий chat_id с клиентом (чтобы бот мог отправлять напоминания клиенту)
/add_session <client_id> <DD.MM.YYYY HH:MM> [коммент] — добавить тренировку
/schedule — ближайшие тренировки (по умолчанию 30 дней)
/complete_session <session_id> — отметить тренировку выполненной
/paid <client_id> <сумма> [комментарий]
/debts — список клиентов с отрицательным балансом
/stats <days> — статистика за последние N дней (по умолчанию 30)
/help — помощь

Ограничения MVP: один бот, таблица sqlite в файле crm.db рядом со скриптом.
"""

import os
import sqlite3
import asyncio
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.executor import start_polling
from datetime import datetime, timedelta
from dateutil import parser as dateparser
import logging

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv('BOT_TOKEN', '8235146977:AAFlcjiVT-LBfOJBAfOYi2_A99C-c6-4QyI')
if API_TOKEN == 'PUT_YOUR_TOKEN_HERE':
    logging.warning('TOKEN not set. Put your token in BOT_TOKEN env or edit the file.')

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

DB_FILE = 'crm.db'

# --- Database init ---
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute('''CREATE TABLE IF NOT EXISTS trainers (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER UNIQUE,
    name TEXT,
    created_at TEXT
)
''')

cur.execute('''CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    notes TEXT,
    balance REAL DEFAULT 0,
    chat_id INTEGER
)
''')

cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
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

cur.execute('''CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    client_id INTEGER,
    amount REAL,
    date TEXT,
    note TEXT,
    FOREIGN KEY(client_id) REFERENCES clients(id)
)
''')

conn.commit()

# --- Helpers ---

def parse_dt(text: str) -> datetime:
    """Пытаемся распарсить дату в удобных форматах. Ожидаем: DD.MM.YYYY HH:MM или DD.MM HH:MM"""
    text = text.strip()
    try:
        # сначала явный ISO or flexible parse
        dt = dateparser.parse(text, dayfirst=True)
        if dt is None:
            raise ValueError
        return dt
    except Exception:
        # last resort: try to append year
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
    raise ValueError("Не удалось распознать дату. Используй формат DD.MM.YYYY HH:MM")


def get_trainer_chat_ids():
    cur.execute("SELECT chat_id FROM trainers")
    return [r[0] for r in cur.fetchall()]


def ensure_trainer(chat_id: int, name: str = None):
    cur.execute("SELECT id FROM trainers WHERE chat_id = ?", (chat_id,))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO trainers (chat_id, name, created_at) VALUES (?, ?, ?)", (chat_id, name or 'trainer', datetime.utcnow().isoformat()))
        conn.commit()


# --- Command handlers ---

@dp.message_handler(commands=['help'])
async def help_command(msg: types.Message):
    await msg.answer("""Команды:
/trainer_register — зарегистрировать этот чат как тренер
/add_client Имя Телефон [примечание]
/clients
/client <id>
/link_client <id> — связать текущий chat с клиентом
/add_session <client_id> <DD.MM.YYYY HH:MM> [комментарий]
/schedule
/complete_session <session_id>
/paid <client_id> <сумма> [комментарий]
/debts
/stats [days]
/help
""")


@dp.message_handler(commands=['trainer_register'])
async def trainer_register(msg: types.Message):
    ensure_trainer(msg.chat.id, msg.from_user.full_name)
    await msg.answer(f'Готово. Этот чат ({msg.chat.id}) зарегистрирован как тренер.')


@dp.message_handler(commands=['add_client'])
async def add_client(msg: types.Message):
    # формат: /add_client Имя Телефон Примечание...
    parts = msg.text.split(maxsplit=3)
    if len(parts) < 3:
        await msg.answer('Используй: /add_client Имя Телефон [примечание]')
        return
    _, name, phone = parts[:3]
    notes = parts[3] if len(parts) == 4 else ''
    cur.execute('INSERT INTO clients (name, phone, notes) VALUES (?, ?, ?)', (name, phone, notes))
    conn.commit()
    cid = cur.lastrowid
    await msg.answer(f'Клиент добавлен: {name} (id={cid})')


@dp.message_handler(commands=['clients'])
async def list_clients(msg: types.Message):
    cur.execute('SELECT id, name, phone, balance FROM clients ORDER BY id')
    rows = cur.fetchall()
    if not rows:
        await msg.answer('Клиентов пока нет')
        return
    text = '\n'.join([f"{r[0]}. {r[1]} — {r[2]} — balance: {r[3]:.2f}" for r in rows])
    await msg.answer(text)


@dp.message_handler(commands=['client'])
async def client_card(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer('Используй: /client <id>')
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await msg.answer('Неверный id')
        return
    cur.execute('SELECT id, name, phone, notes, balance, chat_id FROM clients WHERE id = ?', (cid,))
    r = cur.fetchone()
    if not r:
        await msg.answer('Клиент не найден')
        return
    text = f"ID: {r[0]}\nИмя: {r[1]}\nТелефон: {r[2]}\nПримечание: {r[3]}\nБаланс: {r[4]:.2f}\nChat_id: {r[5]}"
    # последние 5 сессий
    cur.execute('SELECT id, datetime, status, comment FROM sessions WHERE client_id = ? ORDER BY datetime DESC LIMIT 5', (cid,))
    sessions = cur.fetchall()
    if sessions:
        text += '\n\nПоследние тренировки:'
        for s in sessions:
            text += f"\n{s[0]}. {s[1]} — {s[2]} — {s[3] or ''}"
    # последние 5 платежей
    cur.execute('SELECT id, amount, date, note FROM payments WHERE client_id = ? ORDER BY date DESC LIMIT 5', (cid,))
    pays = cur.fetchall()
    if pays:
        text += '\n\nПоследние платежи:'
        for p in pays:
            text += f"\n{p[0]}. {p[1]:.2f} — {p[2]} — {p[3] or ''}"
    await msg.answer(text)


@dp.message_handler(commands=['link_client'])
async def link_client(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer('Используй: /link_client <id> — и этот чат будет связан с клиентом')
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await msg.answer('Неверный id')
        return
    cur.execute('UPDATE clients SET chat_id = ? WHERE id = ?', (msg.chat.id, cid))
    conn.commit()
    await msg.answer(f'Клиент {cid} привязан к этому чату (chat_id={msg.chat.id})')


@dp.message_handler(commands=['add_session'])
async def add_session(msg: types.Message):
    # /add_session <client_id> <date time> [comment]
    parts = msg.text.split(maxsplit=3)
    if len(parts) < 3:
        await msg.answer('Используй: /add_session <client_id> <DD.MM.YYYY HH:MM> [комментарий]')
        return
    try:
        cid = int(parts[1])
    except ValueError:
        await msg.answer('Неверный client_id')
        return
    dt_text = parts[2]
    # если дата содержит пробел (дата и время), и была объединена в parts[2] из-за split, но user может дать с пробелом
    # но мы splitted maxsplit=3 so parts[2] may include only date, parts[3] comment; let's try to parse combining parts[2] and part[3] if looks like time
    comment = parts[3] if len(parts) == 4 else ''
    # Попробуем соединить дату и комментарий, если комментарий начинается с времени (на случай, если пользователь написал дату и время с пробелом)
    try:
        dt = parse_dt(dt_text if len(parts) == 3 else dt_text + ' ' + comment.split()[0])
        # если мы использовали элемент из comment как время, убираем его
        if len(parts) == 4 and len(comment.split())>0:
            # если первый токен комментария был временем, убираем его
            maybe_time = comment.split()[0]
            try:
                _ = parse_dt(maybe_time)
                # убираем первый токен из комментария
                comment = ' '.join(comment.split()[1:])
            except Exception:
                pass
    except Exception:
        # пробуем распарсить комбинацию parts[2] + ' ' + parts[3]
        try:
            dt = parse_dt(parts[2] + ' ' + (parts[3] if len(parts) >=4 else ''))
            comment = ''
        except Exception as e:
            await msg.answer('Не удалось распознать дату. Используй формат: DD.MM.YYYY HH:MM')
            return
    # сохраняем
    dt_iso = dt.isoformat()
    cur.execute('INSERT INTO sessions (client_id, datetime, comment) VALUES (?, ?, ?)', (cid, dt_iso, comment))
    conn.commit()
    sid = cur.lastrowid
    await msg.answer(f'Сессия добавлена (id={sid}) для клиента {cid} на {dt.strftime("%d.%m.%Y %H:%M")}.')


@dp.message_handler(commands=['schedule'])
async def schedule(msg: types.Message):
    # /schedule [days]
    parts = msg.text.split(maxsplit=1)
    days = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 30
    now = datetime.utcnow()
    end = now + timedelta(days=days)
    cur.execute('SELECT s.id, s.client_id, s.datetime, s.status, c.name FROM sessions s LEFT JOIN clients c ON s.client_id=c.id WHERE s.datetime BETWEEN ? AND ? ORDER BY s.datetime', (now.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    if not rows:
        await msg.answer('Нет тренировок в выбранном диапазоне')
        return
    text = ''
    for r in rows:
        dt = datetime.fromisoformat(r[2])
        text += f"{r[0]}. {dt.strftime('%d.%m.%Y %H:%M')} — {r[4] or 'Клиент '+str(r[1])} — {r[3]}\n"
    await msg.answer(text)


@dp.message_handler(commands=['complete_session'])
async def complete_session(msg: types.Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer('Используй: /complete_session <id>')
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await msg.answer('Неверный id')
        return
    cur.execute('UPDATE sessions SET status = ? WHERE id = ?', ('completed', sid))
    conn.commit()
    await msg.answer(f'Сессия {sid} помечена как выполненная')


@dp.message_handler(commands=['paid'])
async def paid(msg: types.Message):
    # /paid <client_id> <amount> [note]
    parts = msg.text.split(maxsplit=3)
    if len(parts) < 3:
        await msg.answer('Используй: /paid <client_id> <amount> [комментарий]')
        return
    try:
        cid = int(parts[1])
        amount = float(parts[2])
    except ValueError:
        await msg.answer('Неверные параметры. Пример: /paid 3 1500')
        return
    note = parts[3] if len(parts) == 4 else ''
    now = datetime.utcnow().isoformat()
    cur.execute('INSERT INTO payments (client_id, amount, date, note) VALUES (?, ?, ?, ?)', (cid, amount, now, note))
    # обновляем баланс
    cur.execute('UPDATE clients SET balance = balance + ? WHERE id = ?', (amount, cid))
    conn.commit()
    await msg.answer(f'Платеж записан: client={cid}, amount={amount:.2f}')


@dp.message_handler(commands=['debts'])
async def debts(msg: types.Message):
    cur.execute('SELECT id, name, phone, balance FROM clients WHERE balance < 0 ORDER BY balance')
    rows = cur.fetchall()
    if not rows:
        await msg.answer('Должников нет')
        return
    text = '\n'.join([f"{r[0]}. {r[1]} — {r[2]} — balance: {r[3]:.2f}" for r in rows])
    await msg.answer(text)


@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message):
    # /stats [days]
    parts = msg.text.split(maxsplit=1)
    days = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 30
    since = datetime.utcnow() - timedelta(days=days)
    cur.execute('SELECT COUNT(*) FROM sessions WHERE status = ? AND datetime >= ?', ('completed', since.isoformat()))
    sessions_done = cur.fetchone()[0]
    cur.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE date >= ?', (since.isoformat(),))
    income = cur.fetchone()[0]
    await msg.answer(f'Статистика за последние {days} дней:\nПроведено тренировок: {sessions_done}\nПолучено оплат: {income:.2f}')


# --- Background task: reminders ---

async def reminders_loop():
    await bot.wait_until_ready()
    logging.info('Reminders loop started')
    while True:
        try:
            now = datetime.utcnow()
            # reminders 24h
            t24_from = now + timedelta(hours=24)
            t24_to = now + timedelta(hours=24, minutes=1)
            # reminders 2h
            t2_from = now + timedelta(hours=2)
            t2_to = now + timedelta(hours=2, minutes=1)

            # 24h reminders: find sessions scheduled between t24_from and t24_to and remind24_sent == 0
            cur.execute('SELECT s.id, s.client_id, s.datetime, s.comment, c.chat_id, c.name FROM sessions s LEFT JOIN clients c ON s.client_id=c.id WHERE s.remind24_sent = 0 AND s.status = ? AND s.datetime BETWEEN ? AND ?', ('planned', t24_from.isoformat(), t24_to.isoformat()))
            rows24 = cur.fetchall()
            for r in rows24:
                sid, cid, dt_iso, comment, client_chat, client_name = r
                dt = datetime.fromisoformat(dt_iso)
                txt = f'Напоминание: тренировка {dt.strftime("%d.%m.%Y %H:%M")} — {client_name or cid} (id={cid}).'
                if comment:
                    txt += '\n' + comment
                # отправляем тренеру(ам)
                trainers = get_trainer_chat_ids()
                for t in trainers:
                    try:
                        await bot.send_message(t, 'За 24 часа — ' + txt)
                    except Exception as e:
                        logging.exception('Failed send 24h to trainer')
                # отправляем клиенту (если связан)
                if client_chat:
                    try:
                        await bot.send_message(client_chat, f'Привет! Напоминаем о тренировке {dt.strftime("%d.%m.%Y %H:%M")}.')
                    except Exception:
                        logging.exception('Failed send 24h to client')
                # помечаем
                cur.execute('UPDATE sessions SET remind24_sent = 1 WHERE id = ?', (sid,))
                conn.commit()

            # 2h reminders
            cur.execute('SELECT s.id, s.client_id, s.datetime, s.comment, c.chat_id, c.name FROM sessions s LEFT JOIN clients c ON s.client_id=c.id WHERE s.remind2_sent = 0 AND s.status = ? AND s.datetime BETWEEN ? AND ?', ('planned', t2_from.isoformat(), t2_to.isoformat()))
            rows2 = cur.fetchall()
            for r in rows2:
                sid, cid, dt_iso, comment, client_chat, client_name = r
                dt = datetime.fromisoformat(dt_iso)
                txt = f'Напоминание: тренировка {dt.strftime("%d.%m.%Y %H:%M")} — {client_name or cid} (id={cid}).'
                if comment:
                    txt += '\n' + comment
                trainers = get_trainer_chat_ids()
                for t in trainers:
                    try:
                        await bot.send_message(t, 'За 2 часа — ' + txt)
                    except Exception:
                        logging.exception('Failed send 2h to trainer')
                if client_chat:
                    try:
                        await bot.send_message(client_chat, f'Привет! Напоминаем о тренировке через 2 часа: {dt.strftime("%d.%m.%Y %H:%M")}.')
                    except Exception:
                        logging.exception('Failed send 2h to client')
                cur.execute('UPDATE sessions SET remind2_sent = 1 WHERE id = ?', (sid,))
                conn.commit()

        except Exception as e:
            logging.exception('Error in reminders loop')
        await asyncio.sleep(60)  # проверяем каждую минуту


# --- graceful start ---

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    # запустим background task
    loop.create_task(reminders_loop())
    print('Bot is starting...')
    start_polling(dp, skip_updates=True)
