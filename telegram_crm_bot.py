"""
Telegram CRM для тренеров — Расширенный MVP с ролями и заявками (кнопки)
Автор: сгенерировано ChatGPT

Ключевые возможности:
- Разделение ролей: Тренер / Клиент (выбор при /start)
- Для клиента: выбор тренера из списка, создание заявки, уведомление тренеру
- Для тренера: список заявок (одобрить/отклонить), свои клиенты, расписание, платежи, статистика
- Поддержка множества тренеров и их независимых групп клиентов
- Напоминания за 24ч и 2ч — только соответствующему тренеру и клиенту
- Полностью на кнопках (ReplyKeyboard + InlineKeyboard), формы через FSM
- Дополнения: быстрый поиск тренера/клиента, завершение сессии кнопкой, редактирование профиля клиента, авто-слоты времени (сегодня/завтра 9/12/18)

Зависимости:
    pip install aiogram==2.25 python-dateutil

Запуск:
    export BOT_TOKEN="<твой_токен>"
    python telegram_crm_bot.py
"""

import os
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from dateutil import parser as dateparser

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    CallbackQuery
)
from aiogram.utils import executor
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv('BOT_TOKEN')
if not API_TOKEN:
    logger.warning("BOT_TOKEN не установлен. Установи переменную окружения BOT_TOKEN перед запуском.")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

DB_FILE = 'crm.db'

# --- Database init ---
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute('''CREATE TABLE IF NOT EXISTS trainers (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER UNIQUE,
    name TEXT,
    created_at TEXT
)''')

cur.execute('''CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    notes TEXT,
    balance REAL DEFAULT 0,
    chat_id INTEGER,
    trainer_id INTEGER,
    status TEXT DEFAULT 'approved', -- pending/approved/rejected
    FOREIGN KEY(trainer_id) REFERENCES trainers(id)
)''')

cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    client_id INTEGER,
    datetime TEXT,
    status TEXT DEFAULT 'planned',
    comment TEXT,
    remind24_sent INTEGER DEFAULT 0,
    remind2_sent INTEGER DEFAULT 0,
    FOREIGN KEY(client_id) REFERENCES clients(id)
)''')

cur.execute('''CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    client_id INTEGER,
    amount REAL,
    date TEXT,
    note TEXT,
    FOREIGN KEY(client_id) REFERENCES clients(id)
)''')
conn.commit()

# Миграции на случай старой БД
try:
    cur.execute("ALTER TABLE clients ADD COLUMN trainer_id INTEGER")
except Exception:
    pass
try:
    cur.execute("ALTER TABLE clients ADD COLUMN status TEXT DEFAULT 'approved'")
except Exception:
    pass
conn.commit()

# --- Helpers & Keyboards ---
PER_PAGE = 10

TRAINER_KB = ReplyKeyboardMarkup(resize_keyboard=True)
TRAINER_KB.row(KeyboardButton('📋 Мои клиенты'), KeyboardButton('📝 Заявки'))
TRAINER_KB.row(KeyboardButton('➕ Добавить клиента'), KeyboardButton('📅 Расписание'))
TRAINER_KB.row(KeyboardButton('💸 Должники'), KeyboardButton('📈 Статистика'))

CLIENT_KB = ReplyKeyboardMarkup(resize_keyboard=True)
CLIENT_KB.row(KeyboardButton('🧑‍🏫 Выбрать тренера'), KeyboardButton('📅 Мои тренировки'))
CLIENT_KB.row(KeyboardButton('💸 Мой баланс'))

ROLE_KB = ReplyKeyboardMarkup(resize_keyboard=True)
ROLE_KB.row(KeyboardButton('Я тренер'), KeyboardButton('Я клиент'))


def parse_dt(text: str) -> datetime:
    text = text.strip()
    try:
        dt = dateparser.parse(text, dayfirst=True)
        if dt is None:
            raise ValueError
        return dt
    except Exception:
        try:
            parts = text.split()
            if len(parts) >= 2:
                datepart, timepart = parts[0], parts[1]
                if datepart.count('.') == 1:
                    year = datetime.now().year
                    dt = datetime.strptime(f"{datepart}.{year} {timepart}", "%d.%m.%Y %H:%M")
                    return dt
        except Exception:
            pass
    raise ValueError("Не удалось распознать дату. Пример: 12.08.2025 18:00")


def get_trainer_chat_ids():
    cur.execute("SELECT chat_id FROM trainers")
    return [r[0] for r in cur.fetchall()]


def ensure_trainer(chat_id: int, name: str = None):
    cur.execute("SELECT id FROM trainers WHERE chat_id = ?", (chat_id,))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO trainers (chat_id, name, created_at) VALUES (?, ?, ?)", (chat_id, name or 'trainer', datetime.utcnow().isoformat()))
        conn.commit()


def get_trainer_id_by_chat(chat_id: int):
    cur.execute("SELECT id FROM trainers WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    return row[0] if row else None


def get_client_by_chat(chat_id: int):
    cur.execute("SELECT id, name, phone, trainer_id, status, balance FROM clients WHERE chat_id = ?", (chat_id,))
    return cur.fetchone()


def get_role(chat_id: int) -> str:
    if get_trainer_id_by_chat(chat_id):
        return 'trainer'
    if get_client_by_chat(chat_id):
        return 'client'
    return 'none'


def build_trainers_kb(page: int = 0) -> InlineKeyboardMarkup:
    cur.execute('SELECT id, name, chat_id FROM trainers ORDER BY id')
    all_rows = cur.fetchall()
    start = page * PER_PAGE
    end = start + PER_PAGE
    rows = all_rows[start:end]
    kb = InlineKeyboardMarkup(row_width=1)
    for tid, name, tchat in rows:
        title = name or f"Тренер {tid}"
        kb.add(InlineKeyboardButton(f"{tid}. {title}", callback_data=f"pick_trainer:{tid}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Назад', callback_data=f"trainers_page:{page-1}"))
    if end < len(all_rows):
        nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data=f"trainers_page:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(InlineKeyboardButton('🔎 Поиск тренера', callback_data='search_trainers'))
    return kb


def build_clients_kb_for_trainer(trainer_id: int, page: int = 0) -> InlineKeyboardMarkup:
    cur.execute('SELECT id, name FROM clients WHERE trainer_id = ? AND status = ? ORDER BY id', (trainer_id, 'approved'))
    all_rows = cur.fetchall()
    start = page * PER_PAGE
    end = start + PER_PAGE
    rows = all_rows[start:end]
    kb = InlineKeyboardMarkup(row_width=1)
    for cid, name in rows:
        kb.add(InlineKeyboardButton(f"{cid}. {name}", callback_data=f"client:{cid}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Назад', callback_data=f"myclients_page:{page-1}"))
    if end < len(all_rows):
        nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data=f"myclients_page:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(InlineKeyboardButton('🔎 Поиск клиента', callback_data='search_clients'))
    return kb


def build_client_card_kb(cid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton('➕ Тренировка', callback_data=f"add_session:{cid}"),
        InlineKeyboardButton('💸 Платёж', callback_data=f"add_payment:{cid}")
    )
    kb.row(
        InlineKeyboardButton('📜 История', callback_data=f"history:{cid}"),
        InlineKeyboardButton('✏️ Редактировать', callback_data=f"edit_client:{cid}")
    )
    kb.add(InlineKeyboardButton('🔗 Привязать чат', callback_data=f"link_client:{cid}"))
    return kb


def build_requests_kb(trainer_id: int, page: int = 0) -> InlineKeyboardMarkup:
    cur.execute('SELECT id, name, phone FROM clients WHERE trainer_id = ? AND status = ? ORDER BY id', (trainer_id, 'pending'))
    all_rows = cur.fetchall()
    start = page * PER_PAGE
    end = start + PER_PAGE
    rows = all_rows[start:end]
    kb = InlineKeyboardMarkup(row_width=2)
    for cid, name, phone in rows:
        kb.row(
            InlineKeyboardButton(f"{cid}. {name}", callback_data=f"client:{cid}"),
            InlineKeyboardButton('✅ Одобрить', callback_data=f"approve:{cid}")
        )
        kb.row(InlineKeyboardButton('❌ Отклонить', callback_data=f"reject:{cid}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ Назад', callback_data=f"req_page:{page-1}"))
    if end < len(all_rows):
        nav.append(InlineKeyboardButton('Вперёд ➡️', callback_data=f"req_page:{page+1}"))
    if nav:
        kb.row(*nav)
    return kb

# --- FSM States ---
class AddClient(StatesGroup):
    name = State()
    phone = State()
    notes = State()

class AddSession(StatesGroup):
    client_id = State()
    when = State()
    comment = State()

class AddPayment(StatesGroup):
    client_id = State()
    amount = State()
    note = State()

class ClientOnboard(StatesGroup):
    name = State()
    phone = State()

class SearchTrainer(StatesGroup):
    query = State()

class SearchClient(StatesGroup):
    query = State()

class EditClient(StatesGroup):
    client_id = State()
    field = State()
    value = State()

# --- Commands & Role entry ---
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    role = get_role(message.chat.id)
    if role == 'trainer':
        await message.answer("Вы зарегистрированы как тренер. Добро пожаловать!", reply_markup=TRAINER_KB)
        return
    if role == 'client':
        await message.answer("Вы зарегистрированы как клиент.", reply_markup=CLIENT_KB)
        return
    await message.answer("Кто вы?", reply_markup=ROLE_KB)

@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    role = get_role(message.chat.id)
    if role == 'trainer':
        await message.answer("Доступно: 📋 Мои клиенты, 📝 Заявки, ➕ Добавить клиента, 📅 Расписание, 💸 Должники, 📈 Статистика", reply_markup=TRAINER_KB)
    elif role == 'client':
        await message.answer("Доступно: 🧑‍🏫 Выбрать тренера, 📅 Мои тренировки, 💸 Мой баланс", reply_markup=CLIENT_KB)
    else:
        await message.answer("Нажмите: Я тренер / Я клиент", reply_markup=ROLE_KB)

# --- Role choose ---
@dp.message_handler(lambda m: m.text == 'Я тренер')
async def i_am_trainer(message: types.Message):
    ensure_trainer(message.chat.id, message.from_user.full_name)
    await message.answer('Чат зарегистрирован как тренер ✅', reply_markup=TRAINER_KB)

@dp.message_handler(lambda m: m.text == 'Я клиент')
async def i_am_client(message: types.Message, state: FSMContext):
    client = get_client_by_chat(message.chat.id)
    if client:
        await message.answer('Вы уже зарегистрированы как клиент.', reply_markup=CLIENT_KB)
        return
    await ClientOnboard.name.set()
    await message.answer('Введите ваше имя:', reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=ClientOnboard.name)
async def onboard_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await ClientOnboard.phone.set()
    await message.answer('Введите ваш телефон:')

@dp.message_handler(state=ClientOnboard.phone)
async def onboard_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    phone = message.text.strip()
    cur.execute('INSERT INTO clients (name, phone, chat_id, status) VALUES (?, ?, ?, ?)', (name, phone, message.chat.id, 'pending'))
    conn.commit()
    await state.finish()
    await message.answer('Выберите тренера из списка ниже:', reply_markup=build_trainers_kb(0))

# --- Client actions ---
@dp.message_handler(lambda m: m.text == '🧑‍🏫 Выбрать тренера')
async def client_pick_trainer(message: types.Message):
    await message.answer('Выберите тренера:', reply_markup=build_trainers_kb(0))

@dp.callback_query_handler(lambda c: c.data == 'search_trainers')
async def cb_search_trainers(call: CallbackQuery, state: FSMContext):
    await SearchTrainer.query.set()
    await call.message.answer('Введите часть имени тренера для поиска:')
    await call.answer()

@dp.message_handler(state=SearchTrainer.query)
async def st_search_trainers_query(message: types.Message, state: FSMContext):
    q = f"%{message.text.strip()}%"
    cur.execute('SELECT id, name FROM trainers WHERE name LIKE ? ORDER BY id LIMIT 30', (q,))
    rows = cur.fetchall()
    if not rows:
        await message.answer('Ничего не найдено. Попробуйте ещё раз или откройте список тренеров кнопкой.')
        await state.finish()
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for tid, name in rows:
        kb.add(InlineKeyboardButton(f"{tid}. {name}", callback_data=f"pick_trainer:{tid}"))
    await message.answer('Результаты поиска:', reply_markup=kb)
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('trainers_page:'))
async def cb_trainers_page(call: CallbackQuery):
    page = int(call.data.split(':')[1])
    await call.message.edit_text('Выберите тренера:')
    await call.message.edit_reply_markup(build_trainers_kb(page))
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('pick_trainer:'))
async def cb_pick_trainer(call: CallbackQuery):
    tid = int(call.data.split(':')[1])
    cur.execute('UPDATE clients SET trainer_id = ?, status = ? WHERE chat_id = ?', (tid, 'pending', call.message.chat.id))
    conn.commit()
    cur.execute('SELECT name, phone, id FROM clients WHERE chat_id = ?', (call.message.chat.id,))
    cname, cphone, cid = cur.fetchone()
    cur.execute('SELECT chat_id, name FROM trainers WHERE id = ?', (tid,))
    trow = cur.fetchone()
    tchat, tname = (trow[0], trow[1]) if trow else (None, 'тренер')
    kb = InlineKeyboardMarkup().row(
        InlineKeyboardButton('✅ Одобрить', callback_data=f"approve:{cid}"),
        InlineKeyboardButton('❌ Отклонить', callback_data=f"reject:{cid}")
    )
    if tchat:
        try:
            await bot.send_message(
                tchat,
                f"Новая заявка от клиента:\n{cid}. {cname} — {cphone}",
                reply_markup=kb
            )
        except Exception:
            logger.exception('Не удалось отправить заявку тренеру')
    await call.message.edit_text(f"Заявка отправлена тренеру {tname}. Ожидайте подтверждения.")
    await call.answer()

@dp.message_handler(lambda m: m.text == '📅 Мои тренировки')
async def my_sessions(message: types.Message):
    cur.execute('SELECT id FROM clients WHERE chat_id = ?', (message.chat.id,))
    row = cur.fetchone()
    if not row:
        await message.answer('Вы ещё не зарегистрированы как клиент. Нажмите /start.')
        return
    cid = row[0]
    now = datetime.utcnow()
    end = now + timedelta(days=60)
    cur.execute('SELECT id, datetime, status, comment FROM sessions WHERE client_id = ? AND datetime BETWEEN ? AND ? ORDER BY datetime', (cid, now.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    if not rows:
        await message.answer('Пока нет запланированных тренировок.', reply_markup=CLIENT_KB)
        return
    text = "\n".join([f"{r[0]}. {datetime.fromisoformat(r[1]).strftime('%d.%m.%Y %H:%M')} — {r[2]} — {r[3] or ''}" for r in rows])
    await message.answer(text, reply_markup=CLIENT_KB)

@dp.message_handler(lambda m: m.text == '💸 Мой баланс')
async def my_balance(message: types.Message):
    cur.execute('SELECT id, balance FROM clients WHERE chat_id = ?', (message.chat.id,))
    row = cur.fetchone()
    if not row:
        await message.answer('Вы ещё не зарегистрированы как клиент. Нажмите /start.')
        return
    cid, bal = row
    cur.execute('SELECT amount, date, note FROM payments WHERE client_id = ? ORDER BY date DESC LIMIT 5', (cid,))
    pays = cur.fetchall()
    text = f"Ваш баланс: {bal:.2f}\nПоследние платежи:"
    if pays:
        for p in pays:
            text += f"\n{p[0]:.2f} — {p[1]} — {p[2] or ''}"
    else:
        text += "\n—"
    await message.answer(text, reply_markup=CLIENT_KB)

# --- Trainer actions ---
@dp.message_handler(lambda m: m.text == '📋 Мои клиенты')
async def my_clients(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Вы не тренер. Нажмите /start.')
        return
    await message.answer('Ваши клиенты:', reply_markup=build_clients_kb_for_trainer(tid, 0))

@dp.callback_query_handler(lambda c: c.data == 'search_clients')
async def cb_search_clients(call: CallbackQuery, state: FSMContext):
    await SearchClient.query.set()
    await call.message.answer('Введите часть имени клиента для поиска:')
    await call.answer()

@dp.message_handler(state=SearchClient.query)
async def st_search_clients_query(message: types.Message, state: FSMContext):
    tid = get_trainer_id_by_chat(message.chat.id)
    q = f"%{message.text.strip()}%"
    cur.execute('SELECT id, name FROM clients WHERE trainer_id = ? AND status = ? AND name LIKE ? ORDER BY id LIMIT 50', (tid, 'approved', q))
    rows = cur.fetchall()
    if not rows:
        await message.answer('Ничего не найдено. Попробуйте ещё раз или откройте список клиентов кнопкой.')
        await state.finish()
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for cid, name in rows:
        kb.add(InlineKeyboardButton(f"{cid}. {name}", callback_data=f"client:{cid}"))
    await message.answer('Результаты поиска:', reply_markup=kb)
    await state.finish()

@dp.message_handler(lambda m: m.text == '📝 Заявки')
async def my_requests(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Вы не тренер.')
        return
    await message.answer('Заявки от клиентов:', reply_markup=build_requests_kb(tid, 0))

@dp.callback_query_handler(lambda c: c.data.startswith('req_page:'))
async def cb_requests_page(call: CallbackQuery):
    tid = get_trainer_id_by_chat(call.message.chat.id)
    page = int(call.data.split(':')[1])
    await call.message.edit_text('Заявки от клиентов:')
    await call.message.edit_reply_markup(build_requests_kb(tid, page))
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('approve:'))
async def cb_approve(call: CallbackQuery):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT trainer_id, chat_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row or row[0] != tid:
        await call.answer('Эта заявка не для вас.', show_alert=True)
        return
    cur.execute("UPDATE clients SET status = 'approved' WHERE id = ?", (cid,))
    conn.commit()
    await call.answer('Клиент одобрен ✅', show_alert=False)
    await call.message.edit_reply_markup(build_requests_kb(tid, 0))
    client_chat = row[1]
    if client_chat:
        try:
            await bot.send_message(client_chat, 'Ваша заявка подтверждена ✅', reply_markup=CLIENT_KB)
        except Exception:
            logger.exception('Не удалось уведомить клиента об одобрении')

@dp.callback_query_handler(lambda c: c.data.startswith('reject:'))
async def cb_reject(call: CallbackQuery):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT trainer_id, chat_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row or row[0] != tid:
        await call.answer('Эта заявка не для вас.', show_alert=True)
        return
    cur.execute("UPDATE clients SET status = 'rejected', trainer_id = NULL WHERE id = ?", (cid,))
    conn.commit()
    await call.answer('Заявка отклонена ❌', show_alert=False)
    await call.message.edit_reply_markup(build_requests_kb(tid, 0))
    client_chat = row[1]
    if client_chat:
        try:
            await bot.send_message(client_chat, 'К сожалению, заявка отклонена. Вы можете выбрать другого тренера.', reply_markup=CLIENT_KB)
        except Exception:
            logger.exception('Не удалось уведомить клиента об отклонении')

@dp.callback_query_handler(lambda c: c.data.startswith('client:'))
async def cb_client_card(call: CallbackQuery):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT id, name, phone, notes, balance, chat_id, trainer_id, status FROM clients WHERE id = ?', (cid,))
    r = cur.fetchone()
    if not r:
        await call.answer('Клиент не найден', show_alert=True)
        return
    if r[6] != tid:
        await call.answer('Этот клиент не относится к вам.', show_alert=True)
        return
    text = (f"ID: {r[0]}\n"
            f"Имя: {r[1]}\n"
            f"Телефон: {r[2]}\n"
            f"Примечание: {r[3]}\n"
            f"Баланс: {r[4]:.2f}\n"
            f"Chat_id: {r[5]}\n"
            f"Статус: {r[7]}")
    await call.message.edit_text(text)
    if r[7] == 'approved':
        await call.message.edit_reply_markup(build_client_card_kb(cid))
    else:
        await call.message.edit_reply_markup(None)
    await call.answer()

# Редактирование профиля клиента (тренер)
@dp.callback_query_handler(lambda c: c.data.startswith('edit_client:'))
async def cb_edit_client(call: CallbackQuery, state: FSMContext):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT trainer_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row or row[0] != tid:
        await call.answer('Этот клиент не ваш.', show_alert=True)
        return
    await state.update_data(client_id=cid)
    kb = InlineKeyboardMarkup(row_width=3)
    kb.row(InlineKeyboardButton('Имя', callback_data='edit_field:name'),
           InlineKeyboardButton('Телефон', callback_data='edit_field:phone'),
           InlineKeyboardButton('Заметка', callback_data='edit_field:notes'))
    await call.message.answer('Что изменить?', reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('edit_field:'))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    field = call.data.split(':')[1]
    await state.update_data(field=field)
    await EditClient.value.set()
    human = 'Имя' if field == 'name' else 'Телефон' if field == 'phone' else 'Заметка'
    await call.message.answer(f'Введите новое значение для: {human}')
    await call.answer()

@dp.message_handler(state=EditClient.value)
async def st_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cid = data['client_id']
    field = data['field']
    value = message.text.strip()
    if field not in ('name', 'phone', 'notes'):
        await state.finish()
        await message.answer('Неверное поле.', reply_markup=TRAINER_KB)
        return
    cur.execute(f'UPDATE clients SET {field} = ? WHERE id = ?', (value, cid))
    conn.commit()
    await state.finish()
    await message.answer('Изменено ✅', reply_markup=TRAINER_KB)

# Добавление клиента (тренер)
@dp.message_handler(lambda m: m.text == '➕ Добавить клиента')
async def btn_add_client(message: types.Message, state: FSMContext):
    if not get_trainer_id_by_chat(message.chat.id):
        await message.answer('Только для тренера.', reply_markup=CLIENT_KB)
        return
    await AddClient.name.set()
    await message.answer('Введите имя клиента:', reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=AddClient.name)
async def add_client_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await AddClient.phone.set()
    await message.answer('Введите телефон клиента:')

@dp.message_handler(state=AddClient.phone)
async def add_client_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await AddClient.notes.set()
    await message.answer('Примечание (или "-" чтобы пропустить):')

@dp.message_handler(state=AddClient.notes)
async def add_client_notes(message: types.Message, state: FSMContext):
    notes = '' if message.text.strip() == '-' else message.text.strip()
    data = await state.get_data()
    tid = get_trainer_id_by_chat(message.chat.id)
    cur.execute('INSERT INTO clients (name, phone, notes, trainer_id, status) VALUES (?, ?, ?, ?, ?)', (data['name'], data['phone'], notes, tid, 'approved'))
    conn.commit()
    cid = cur.lastrowid
    await state.finish()
    await message.answer(f'Клиент добавлен: {data["name"]} (id={cid})', reply_markup=TRAINER_KB)

# Добавление тренировки (тренер) + авто-слоты
@dp.callback_query_handler(lambda c: c.data.startswith('add_session:'))
async def cb_add_session(call: CallbackQuery, state: FSMContext):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT trainer_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row or row[0] != tid:
        await call.answer('Этот клиент не ваш.', show_alert=True)
        return
    await state.update_data(client_id=cid)
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    def iso(day, h):
        return (day + timedelta(hours=h)).isoformat()
    kb = InlineKeyboardMarkup(row_width=3)
    kb.row(InlineKeyboardButton('Сегодня 09:00', callback_data=f"slot:{cid}:{iso(today,9)}"),
           InlineKeyboardButton('Сегодня 12:00', callback_data=f"slot:{cid}:{iso(today,12)}"),
           InlineKeyboardButton('Сегодня 18:00', callback_data=f"slot:{cid}:{iso(today,18)}"))
    kb.row(InlineKeyboardButton('Завтра 09:00', callback_data=f"slot:{cid}:{iso(tomorrow,9)}"),
           InlineKeyboardButton('Завтра 12:00', callback_data=f"slot:{cid}:{iso(tomorrow,12)}"),
           InlineKeyboardButton('Завтра 18:00', callback_data=f"slot:{cid}:{iso(tomorrow,18)}"))
    kb.add(InlineKeyboardButton('📝 Ввести вручную', callback_data='slot_manual'))
    await call.message.answer('Выберите слот или введите вручную:', reply_markup=kb)
    await AddSession.when.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('slot:'))
async def cb_pick_slot(call: CallbackQuery, state: FSMContext):
    _ , cid, dt_iso = call.data.split(':', 2)
    await state.update_data(client_id=int(cid), when=datetime.fromisoformat(dt_iso))
    await AddSession.comment.set()
    await call.message.answer('Комментарий (или "-" чтобы пропустить):')
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == 'slot_manual')
async def cb_slot_manual(call: CallbackQuery):
    await call.message.answer('Введите дату/время вручную (например: 12.08 18:00):')
    await call.answer()

@dp.message_handler(state=AddSession.when)
async def st_add_session_when(message: types.Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await message.answer('Не удалось распознать дату. Пример: 12.08.2025 18:00')
        return
    await state.update_data(when=dt)
    await AddSession.comment.set()
    await message.answer('Комментарий (или "-" чтобы пропустить):')

@dp.message_handler(state=AddSession.comment)
async def st_add_session_comment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    comment = '' if message.text.strip() == '-' else message.text.strip()
    dt_iso = data['when'].isoformat()
    cur.execute('INSERT INTO sessions (client_id, datetime, comment) VALUES (?, ?, ?)', (data['client_id'], dt_iso, comment))
    conn.commit()
    sid = cur.lastrowid
    await state.finish()
    await message.answer(f"Сессия добавлена (id={sid}) на {data['when'].strftime('%d.%m.%Y %H:%M')}", reply_markup=TRAINER_KB)

# Платёж (тренер)
@dp.callback_query_handler(lambda c: c.data.startswith('add_payment:'))
async def cb_add_payment(call: CallbackQuery, state: FSMContext):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT trainer_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row or row[0] != tid:
        await call.answer('Этот клиент не ваш.', show_alert=True)
        return
    await state.update_data(client_id=cid)
    await AddPayment.amount.set()
    await call.message.answer('Введите сумму платежа:')
    await call.answer()

@dp.message_handler(state=AddPayment.amount)
async def st_payment_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer('Введите число. Например: 1500')
        return
    await state.update_data(amount=amount)
    await AddPayment.note.set()
    await message.answer('Комментарий (или "-" чтобы пропустить):')

@dp.message_handler(state=AddPayment.note)
async def st_payment_note(message: types.Message, state: FSMContext):
    data = await state.get_data()
    note = '' if message.text.strip() == '-' else message.text.strip()
    now = datetime.utcnow().isoformat()
    cur.execute('INSERT INTO payments (client_id, amount, date, note) VALUES (?, ?, ?, ?)', (data['client_id'], data['amount'], now, note))
    cur.execute('UPDATE clients SET balance = balance + ? WHERE id = ?', (data['amount'], data['client_id']))
    conn.commit()
    await state.finish()
    await message.answer(f"Платёж записан: client={data['client_id']}, amount={data['amount']:.2f}", reply_markup=TRAINER_KB)

# Расписание (тренер) с завершением сессий
@dp.message_handler(lambda m: m.text == '📅 Расписание')
async def trainer_schedule(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Только для тренера.', reply_markup=CLIENT_KB)
        return
    now = datetime.utcnow()
    end = now + timedelta(days=30)
    cur.execute('''SELECT s.id, s.client_id, s.datetime, s.status, c.name
                   FROM sessions s LEFT JOIN clients c ON s.client_id=c.id
                   WHERE c.trainer_id = ? AND s.datetime BETWEEN ? AND ?
                   ORDER BY s.datetime''', (tid, now.isoformat(), end.isoformat()))
    rows = cur.fetchall()
    if not rows:
        await message.answer('Нет тренировок в ближайшие 30 дней.', reply_markup=TRAINER_KB)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for sid, cid, dt_iso, status, cname in rows[:50]:
        dt = datetime.fromisoformat(dt_iso).strftime('%d.%m %H:%M')
        label = f"{sid}: {dt} — {cname} — {status}"
        if status != 'completed':
            kb.add(InlineKeyboardButton(f"✅ Завершить {label}", callback_data=f"done_session:{sid}"))
        else:
            kb.add(InlineKeyboardButton(f"✅ Завершено — {label}", callback_data="noop"))
    await message.answer('Ваше расписание (30 дней):', reply_markup=kb)

# Завершение сессии кнопкой
@dp.callback_query_handler(lambda c: c.data.startswith('done_session:'))
async def cb_done_session(call: CallbackQuery):
    sid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('''SELECT s.id FROM sessions s
                   JOIN clients c ON s.client_id = c.id
                   WHERE s.id = ? AND c.trainer_id = ?''', (sid, tid))
    if not cur.fetchone():
        await call.answer('Сессия не относится к вам.', show_alert=True)
        return
    cur.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (sid,))
    conn.commit()
    await call.answer('Готово ✅')

# Команда завершить сессию (альтернатива)
@dp.message_handler(commands=['complete_session'])
async def complete_session_cmd(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Только для тренера.', reply_markup=CLIENT_KB)
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer('Используй: /complete_session <id>', reply_markup=TRAINER_KB)
        return
    try:
        sid = int(parts[1])
    except ValueError:
        await message.answer('Неверный id', reply_markup=TRAINER_KB)
        return
    cur.execute('''SELECT s.id FROM sessions s
                   JOIN clients c ON s.client_id = c.id
                   WHERE s.id = ? AND c.trainer_id = ?''', (sid, tid))
    if not cur.fetchone():
        await message.answer('Эта сессия не относится к вашим клиентам.', reply_markup=TRAINER_KB)
        return
    cur.execute('UPDATE sessions SET status = ? WHERE id = ?', ('completed', sid))
    conn.commit()
    await message.answer(f'Сессия {sid} помечена как выполненная', reply_markup=TRAINER_KB)

# --- Background reminders ---
async def reminders_loop():
    logger.info('Reminders loop started')
    while True:
        try:
            now = datetime.utcnow()
            t24_from, t24_to = now + timedelta(hours=24), now + timedelta(hours=24, minutes=1)
            t2_from, t2_to = now + timedelta(hours=2), now + timedelta(hours=2, minutes=1)

            cur.execute('''SELECT s.id, s.client_id, s.datetime, s.comment, c.chat_id, c.name, c.trainer_id
                           FROM sessions s
                           JOIN clients c ON s.client_id=c.id
                           WHERE s.remind24_sent = 0 AND s.status = 'planned' AND s.datetime BETWEEN ? AND ?''',
                        (t24_from.isoformat(), t24_to.isoformat()))
            for sid, cid, dt_iso, comment, client_chat, client_name, trainer_id in cur.fetchall():
                dt = datetime.fromisoformat(dt_iso)
                txt = f"Напоминание: тренировка {dt.strftime('%d.%m.%Y %H:%M')} — {client_name} (id={cid})."
                if comment:
                    txt += "\n" + comment
                cur.execute('SELECT chat_id FROM trainers WHERE id = ?', (trainer_id,))
                row = cur.fetchone()
                if row:
                    tchat = row[0]
                    try:
                        await bot.send_message(tchat, "За 24 часа — " + txt)
                    except Exception:
                        logger.exception('Failed send 24h to trainer')
                if client_chat:
                    try:
                        await bot.send_message(client_chat, f"Привет! Напоминаем о тренировке {dt.strftime('%d.%m.%Y %H:%M')}.")
                    except Exception:
                        logger.exception('Failed send 24h to client')
                cur.execute('UPDATE sessions SET remind24_sent = 1 WHERE id = ?', (sid,))
                conn.commit()

            cur.execute('''SELECT s.id, s.client_id, s.datetime, s.comment, c.chat_id, c.name, c.trainer_id
                           FROM sessions s
                           JOIN clients c ON s.client_id=c.id
                           WHERE s.remind2_sent = 0 AND s.status = 'planned' AND s.datetime BETWEEN ? AND ?''',
                        (t2_from.isoformat(), t2_to.isoformat()))
            for sid, cid, dt_iso, comment, client_chat, client_name, trainer_id in cur.fetchall():
                dt = datetime.fromisoformat(dt_iso)
                txt = f"Напоминание: тренировка {dt.strftime('%d.%m.%Y %H:%M')} — {client_name} (id={cid})."
                if comment:
                    txt += "\n" + comment
                cur.execute('SELECT chat_id FROM trainers WHERE id = ?', (trainer_id,))
                row = cur.fetchone()
                if row:
                    tchat = row[0]
                    try:
                        await bot.send_message(tchat, "За 2 часа — " + txt)
                    except Exception:
                        logger.exception('Failed send 2h to trainer')
                if client_chat:
                    try:
                        await bot.send_message(client_chat, f"Привет! Напоминаем о тренировке через 2 часа: {dt.strftime('%d.%m.%Y %H:%M')}.")
                    except Exception:
                        logger.exception('Failed send 2h to client')
                cur.execute('UPDATE sessions SET remind2_sent = 1 WHERE id = ?', (sid,))
                conn.commit()
        except Exception:
            logger.exception('Error in reminders loop')
        await asyncio.sleep(60)

async def on_startup(dp):
    asyncio.create_task(reminders_loop())
    logger.info('on_startup finished — reminders loop scheduled.')

if __name__ == '__main__':
    logger.info('Bot is starting...')
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
