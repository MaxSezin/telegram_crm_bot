"""
Telegram CRM для тренеров — Расширенный MVP (всё в одном файле)

Фичи:
- Роли: тренер / клиент (выбор при /start), автосохранение Telegram-профиля (id, username, first/last/full name)
- Клиент: поиск города (локально + Nominatim без ключа) → выбор тренера; альтернатива — привязка по UUID
- Тренер: заявки (approve/reject), список клиентов, карточка клиента (редактирование), расписание, платежи
- Тарифы/пакеты: имя, описание, цена (редактирует тренер; клиент видит в «ℹ️ Мой тренер»)
- UUID-инвайт: тренер генерирует код, клиент вводит — мгновенная привязка
- Удаление клиента тренером; «уйти от тренера» у клиента (без удаления истории у клиента)
- Напоминания 24ч/2ч по тренировкам
- Полностью на кнопках (Reply/Inline), формы через FSM
- Ручной ввод даты: явное сообщение и пример формата (ДД.ММ.ГГГГ ЧЧ:ММ, 24ч)

Зависимости:
    pip install aiogram==2.25 python-dateutil aiohttp

Запуск:
    export BOT_TOKEN="<твой_токен>"
    python telegram_crm_bot.py
"""

import os
import sqlite3
import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from dateutil import parser as dateparser

import aiohttp

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

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Bot token ---
API_TOKEN = os.getenv('BOT_TOKEN')
if not API_TOKEN:
    logger.warning("BOT_TOKEN не установлен. Установи переменную окружения BOT_TOKEN перед запуском.")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

DB_FILE = 'crm.db'

# --- Список городов (крупные РФ + Другой) ---
CITIES = [
    'Москва','Санкт-Петербург','Новосибирск','Екатеринбург','Казань','Нижний Новгород','Челябинск','Самара','Омск','Ростов-на-Дону',
    'Уфа','Красноярск','Воронеж','Пермь','Волгоград','Краснодар','Саратов','Тюмень','Тольятти','Ижевск',
    'Барнаул','Иркутск','Ульяновск','Хабаровск','Ярославль','Владивосток','Махачкала','Томск','Оренбург','Кемерово',
    'Новокузнецк','Рязань','Астрахань','Набережные Челны','Пенза','Липецк','Киров','Чебоксары','Балашиха','Калининград',
    'Тула','Курск','Ставрополь','Севастополь','Улан-Удэ','Тверь','Магнитогорск','Иваново','Брянск','Сочи',
    'Белгород','Сургут','Владимир','Чита','Архангельск','Нижний Тагил','Калуга','Симферополь','Смоленск','Якутск',
    'Курган','Орёл','Череповец','Вологда','Подольск','Йошкар-Ола','Тамбов','Кострома','Новороссийск','Комсомольск-на-Амуре',
    'Другой'
]

# --- DB init ---
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute('''CREATE TABLE IF NOT EXISTS trainers (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER UNIQUE,
    name TEXT,
    created_at TEXT,
    city TEXT,
    pricing TEXT,
    tg_id INTEGER,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    invite_code TEXT
)''')

cur.execute('''CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT,
    phone TEXT,
    notes TEXT,
    balance REAL DEFAULT 0,
    chat_id INTEGER,
    trainer_id INTEGER,
    status TEXT DEFAULT 'approved',
    tg_id INTEGER,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
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

cur.execute('''CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY,
    trainer_id INTEGER,
    title TEXT,
    description TEXT,
    price REAL,
    FOREIGN KEY(trainer_id) REFERENCES trainers(id)
)''')
conn.commit()

# Миграции (мягкие)
for ddl in [
    "ALTER TABLE trainers ADD COLUMN city TEXT",
    "ALTER TABLE trainers ADD COLUMN pricing TEXT",
    "ALTER TABLE trainers ADD COLUMN tg_id INTEGER",
    "ALTER TABLE trainers ADD COLUMN username TEXT",
    "ALTER TABLE trainers ADD COLUMN first_name TEXT",
    "ALTER TABLE trainers ADD COLUMN last_name TEXT",
    "ALTER TABLE trainers ADD COLUMN invite_code TEXT",
    "ALTER TABLE clients ADD COLUMN tg_id INTEGER",
    "ALTER TABLE clients ADD COLUMN username TEXT",
    "ALTER TABLE clients ADD COLUMN first_name TEXT",
    "ALTER TABLE clients ADD COLUMN last_name TEXT"
]:
    try:
        cur.execute(ddl)
    except Exception:
        pass
conn.commit()

# --- Keyboards ---
PER_PAGE = 10

TRAINER_KB = ReplyKeyboardMarkup(resize_keyboard=True)
TRAINER_KB.row(KeyboardButton('📋 Мои клиенты'), KeyboardButton('📝 Заявки'))
TRAINER_KB.row(KeyboardButton('🔑 Пригласить клиента'), KeyboardButton('📅 Расписание'))
TRAINER_KB.row(KeyboardButton('💸 Должники'), KeyboardButton('📈 Статистика'))
TRAINER_KB.row(KeyboardButton('⚙️ Профиль'))

CLIENT_KB = ReplyKeyboardMarkup(resize_keyboard=True)
CLIENT_KB.row(KeyboardButton('🧑‍🏫 Выбрать тренера'), KeyboardButton('🔎 Найти тренера по UUID'))
CLIENT_KB.row(KeyboardButton('📅 Мои тренировки'), KeyboardButton('ℹ️ Мой тренер'))
CLIENT_KB.row(KeyboardButton('💸 Мой баланс'), KeyboardButton('🚪 Уйти от тренера'))

ROLE_KB = ReplyKeyboardMarkup(resize_keyboard=True)
ROLE_KB.row(KeyboardButton('Я тренер'), KeyboardButton('Я клиент'))

# --- Helpers ---
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
    raise ValueError("Не удалось распознать дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ, пример: 12.08.2025 18:00")

def ensure_trainer(chat_id: int, user: types.User):
    cur.execute("SELECT id FROM trainers WHERE chat_id = ?", (chat_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO trainers (chat_id, name, created_at, tg_id, username, first_name, last_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, user.full_name or 'trainer', datetime.utcnow().isoformat(), user.id, user.username, user.first_name, user.last_name)
        )
        conn.commit()

def get_trainer_id_by_chat(chat_id: int):
    cur.execute("SELECT id FROM trainers WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    return row[0] if row else None

def get_client_by_chat(chat_id: int):
    cur.execute("SELECT id, name, phone, trainer_id, status, balance, tg_id, username FROM clients WHERE chat_id = ?", (chat_id,))
    return cur.fetchone()

def get_role(chat_id: int) -> str:
    if get_trainer_id_by_chat(chat_id):
        return 'trainer'
    if get_client_by_chat(chat_id):
        return 'client'
    return 'none'

def build_trainers_kb(page: int = 0, city: str = None) -> InlineKeyboardMarkup:
    if city and city != 'Другой':
        cur.execute('SELECT id, name FROM trainers WHERE city = ? ORDER BY id', (city,))
    else:
        cur.execute('SELECT id, name FROM trainers ORDER BY id')
    all_rows = cur.fetchall()
    start = page * PER_PAGE
    end = start + PER_PAGE
    rows = all_rows[start:end]
    kb = InlineKeyboardMarkup(row_width=1)
    for tid, name in rows:
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
    kb.row(InlineKeyboardButton('🗑 Удалить клиента', callback_data=f"delete_client:{cid}"))
    kb.add(InlineKeyboardButton('🔗 Привязать чат', callback_data=f"link_client:{cid}"))
    return kb

def _unique_preserve(seq):
    seen = set()
    out = []
    for x in seq:
        k = x.strip()
        if k and k.lower() not in seen:
            out.append(k)
            seen.add(k.lower())
    return out

def search_cities_local(query: str, limit: int = 10):
    q = query.strip().lower()
    if not q:
        return []
    matches = [c for c in CITIES if q in c.lower() and c != 'Другой']
    return matches[:limit]

async def search_cities_nominatim(query: str, limit: int = 10):
    url = 'https://nominatim.openstreetmap.org/search'
    params = {
        'q': query,
        'format': 'json',
        'addressdetails': 1,
        'limit': str(limit),
        'countrycodes': 'ru'
    }
    headers = {'User-Agent': 'TrainerLinkBot/1.0 (contact: you@example.com)'}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as s:
        async with s.get(url, params=params) as r:
            data = await r.json(content_type=None)
    names = []
    for it in data:
        addr = it.get('address', {})
        city = addr.get('city') or addr.get('town') or addr.get('village') or ''
        if not city:
            dn = (it.get('display_name') or '').split(',')[0]
            city = dn.strip()
        if city:
            names.append(city)
    return _unique_preserve(names)[:limit]

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

class EditTrainerProfile(StatesGroup):
    field = State()
    value = State()

class LinkByUUID(StatesGroup):
    code = State()

class AddTariff(StatesGroup):
    title = State()
    description = State()
    price = State()

class DeleteTariff(StatesGroup):
    tariff_id = State()

class SearchCity(StatesGroup):
    query = State()

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
        await message.answer("Доступно: 📋 Мои клиенты, 📝 Заявки, 🔑 Пригласить клиента, 📅 Расписание, 💸 Должники, 📈 Статистика, ⚙️ Профиль", reply_markup=TRAINER_KB)
    elif role == 'client':
        await message.answer("Доступно: 🧑‍🏫 Выбрать тренера, 🔎 Найти тренера по UUID, 📅 Мои тренировки, 💸 Мой баланс, ℹ️ Мой тренер, 🚪 Уйти от тренера", reply_markup=CLIENT_KB)
    else:
        await message.answer("Нажмите: Я тренер / Я клиент", reply_markup=ROLE_KB)

# --- Role choose ---
@dp.message_handler(lambda m: m.text == 'Я тренер')
async def i_am_trainer(message: types.Message):
    ensure_trainer(message.chat.id, message.from_user)
    await message.answer('Чат зарегистрирован как тренер ✅', reply_markup=TRAINER_KB)

@dp.message_handler(lambda m: m.text == 'Я клиент')
async def i_am_client(message: types.Message, state: FSMContext):
    client = get_client_by_chat(message.chat.id)
    if client:
        await message.answer('Вы уже зарегистрированы как клиент.', reply_markup=CLIENT_KB)
        return
    cur.execute(
        'INSERT INTO clients (name, phone, chat_id, status, tg_id, username, first_name, last_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (message.from_user.full_name, '', message.chat.id, 'pending', message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    )
    conn.commit()
    await state.finish()
    await SearchCity.query.set()
    await message.answer('Введите город (например: "Екате" или "Ростов"):')
# --- City search (client + trainer profile) ---
@dp.message_handler(lambda m: m.text == '🧑‍🏫 Выбрать тренера')
async def client_pick_trainer(message: types.Message, state: FSMContext):
    await SearchCity.query.set()
    await message.answer('Введите название города (например: "Казань" или "Санкт"):')
@dp.message_handler(state=SearchCity.query)
async def st_city_query(message: types.Message, state: FSMContext):
    q = message.text.strip()
    local = search_cities_local(q, limit=8)
    try:
        ext = await search_cities_nominatim(q, limit=8)
    except Exception:
        ext = []
    cities = _unique_preserve(local + ext)[:10]
    kb = InlineKeyboardMarkup(row_width=2)
    if cities:
        for c in cities:
            kb.add(InlineKeyboardButton(c, callback_data=f"pick_city:{c}"))
    else:
        kb.add(InlineKeyboardButton('Другой', callback_data="pick_city:Другой"))
    # Если поиск вызвали из профиля тренера — восстановим состояние
    data = await state.get_data()
    return_to = data.get('return_to')
    if return_to == 'trainer_city':
        await EditTrainerProfile.field.set()
        await state.update_data(field='city')
    else:
        await state.finish()
    await message.answer('Выберите город из найденных вариантов:', reply_markup=kb)
@dp.callback_query_handler(lambda c: c.data.startswith('pick_city:'), state=EditTrainerProfile.field)
async def cb_set_city(call: CallbackQuery, state: FSMContext):
    city = call.data.split(':', 1)[1]
    tid = get_trainer_id_by_chat(call.message.chat.id)
    if not tid:
        await call.answer('Не тренер', show_alert=True); return
    cur.execute('UPDATE trainers SET city = ? WHERE id = ?', (city, tid))
    conn.commit()
    await state.finish()
    await call.message.answer(f'Город обновлён: {city}', reply_markup=TRAINER_KB)
    await call.answer()
@dp.callback_query_handler(lambda c: c.data.startswith('pick_city:'))
async def cb_pick_city_client(call: CallbackQuery):
    city = call.data.split(':',1)[1]
    await call.message.edit_text(f'Город: {city}. Выберите тренера:')
    await call.message.edit_reply_markup(build_trainers_kb(0, city=city))
    await call.answer()
@dp.callback_query_handler(lambda c: c.data.startswith('trainers_page:'))
async def cb_trainers_page(call: CallbackQuery):
    page = int(call.data.split(':')[1])
    await call.message.edit_text('Выберите тренера:')
    await call.message.edit_reply_markup(build_trainers_kb(page))
    await call.answer()

# --- Client actions ---
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
@dp.callback_query_handler(lambda c: c.data.startswith('pick_trainer:'))
async def cb_pick_trainer(call: CallbackQuery):
    tid = int(call.data.split(':')[1])
    cur.execute('UPDATE clients SET trainer_id = ?, status = ? WHERE chat_id = ?', (tid, 'pending', call.message.chat.id))
    conn.commit()
    cur.execute('SELECT name, phone, id, tg_id, username FROM clients WHERE chat_id = ?', (call.message.chat.id,))
    cname, cphone, cid, ctg, cuser = cur.fetchone()
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
                f"Новая заявка от клиента:\n{cid}. {cname} — {cphone or 'телефон не указан'}\nTG: @{cuser or '-'} (id={ctg})",
                reply_markup=kb
            )
        except Exception:
            logger.exception('Не удалось отправить заявку тренеру')
    await call.message.edit_text(f"Заявка отправлена тренеру {tname}. Ожидайте подтверждения.")
    await call.answer()
@dp.message_handler(lambda m: m.text == '🔎 Найти тренера по UUID')
async def client_find_trainer_by_uuid(message: types.Message, state: FSMContext):
    await LinkByUUID.code.set()
    await message.answer('Введите UUID (8 символов), который дал тренер. Пример: `A1B2C3D4`', parse_mode='Markdown')
@dp.message_handler(state=LinkByUUID.code)
async def link_by_uuid_submit(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    if len(code) != 8:
        await message.answer('Неверный формат. Введите точно 8 символов, например: `A1B2C3D4`', parse_mode='Markdown')
        return
    cur.execute('SELECT id, chat_id, name FROM trainers WHERE invite_code = ?', (code,))
    t = cur.fetchone()
    if not t:
        await message.answer('Код не найден. Проверьте и попробуйте снова.')
        return
    trainer_id, trainer_chat, trainer_name = t
    cur.execute('UPDATE clients SET trainer_id = ?, status = ? WHERE chat_id = ?', (trainer_id, 'approved', message.chat.id))
    conn.commit()
    await state.finish()
    await message.answer(f'Вы привязаны к тренеру: {trainer_name} ✅', reply_markup=CLIENT_KB)
    try:
        cur.execute('SELECT id, name, phone, tg_id, username FROM clients WHERE chat_id = ?', (message.chat.id,))
        cid, cname, cphone, ctg, cuser = cur.fetchone()
        msg = f"Клиент подключился по UUID:\n{cid}. {cname} — {cphone or 'телефон не указан'}\nTG: @{cuser or '-'} (id={ctg})"
        if trainer_chat:
            await bot.send_message(trainer_chat, msg)
    except Exception:
        pass
@dp.message_handler(lambda m: m.text == 'ℹ️ Мой тренер')
async def my_trainer_info(message: types.Message):
    cur.execute('SELECT trainer_id FROM clients WHERE chat_id = ? AND status = "approved"', (message.chat.id,))
    row = cur.fetchone()
    if not row or not row[0]:
        await message.answer('Тренер не выбран или заявка ещё не одобрена.')
        return
    tid = row[0]
    cur.execute('SELECT name, city, pricing, tg_id, username FROM trainers WHERE id = ?', (tid,))
    t = cur.fetchone()
    if not t:
        await message.answer('Информация о тренере недоступна.')
        return
    name, city, pricing, tg_id, username = t
    text = (
        f"Ваш тренер: {name}\n"
        f"Город: {city or '-'}\n"
        f"Тарифы (общее описание): {pricing or '-'}\n"
        f"Telegram: @{username or '-'} (id={tg_id})"
    )
    cur.execute('SELECT title, description, price FROM tariffs WHERE trainer_id = ? ORDER BY id', (tid,))
    rows = cur.fetchall()
    if rows:
        text += "\n\nТарифы/пакеты:"
        for title, desc, price in rows:
            text += f"\n• {title} — {price:.2f}\n  {desc or '-'}"
    await message.answer(text, reply_markup=CLIENT_KB)
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
    cur.execute(
        'SELECT id, datetime, status, comment FROM sessions WHERE client_id = ? AND datetime BETWEEN ? AND ? ORDER BY datetime',
        (cid, now.isoformat(), end.isoformat())
    )
    rows = cur.fetchall()
    if not rows:
        await message.answer('Пока нет запланированных тренировок.', reply_markup=CLIENT_KB)
        return
    text = "\n".join([
        f"{r[0]}. {datetime.fromisoformat(r[1]).strftime('%d.%m.%Y %H:%M')} — {r[2]} — {r[3] or ''}"
        for r in rows
    ])
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
@dp.message_handler(lambda m: m.text == '🚪 Уйти от тренера')
async def client_leave_trainer_start(message: types.Message):
    cur.execute('SELECT trainer_id FROM clients WHERE chat_id = ? AND status = "approved"', (message.chat.id,))
    row = cur.fetchone()
    if not row or not row[0]:
        await message.answer('Вы не привязаны к тренеру.')
        return
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton('✅ Да, уйти', callback_data='leave_trainer:yes'),
        InlineKeyboardButton('❌ Отмена', callback_data='leave_trainer:no')
    )
    await message.answer('Вы уверены, что хотите разорвать связь с тренером? Тренер будет уведомлён.', reply_markup=kb)
@dp.callback_query_handler(lambda c: c.data.startswith('leave_trainer:'))
async def cb_client_leave_trainer(call: CallbackQuery):
    action = call.data.split(':')[1]
    if action == 'no':
        await call.answer('Отменено')
        await call.message.edit_reply_markup(None)
        return
    cur.execute('SELECT id, name, trainer_id FROM clients WHERE chat_id = ? AND status = "approved"', (call.message.chat.id,))
    row = cur.fetchone()
    if not row:
        await call.answer('Связь уже отсутствует.')
        await call.message.edit_reply_markup(None)
        return
    cid, cname, tid = row
    cur.execute('SELECT chat_id, name FROM trainers WHERE id = ?', (tid,))
    t = cur.fetchone()
    tchat, _ = (t[0], t[1]) if t else (None, 'тренер')
    cur.execute("UPDATE clients SET trainer_id = NULL, status = 'pending' WHERE id = ?", (cid,))
    conn.commit()
    await call.message.edit_reply_markup(None)
    await call.message.answer('Вы вышли от тренера. Можете выбрать нового.', reply_markup=CLIENT_KB)
    await call.answer('Готово ✅')
    if tchat:
        try:
            await bot.send_message(tchat, f'Клиент {cname} (id={cid}) ушёл от тренера.')
        except Exception:
            pass

# --- Trainer actions ---
@dp.message_handler(lambda m: m.text == '📋 Мои клиенты')
async def my_clients(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Вы не тренер. Нажмите /start.')
        return
    await message.answer('Ваши клиенты:', reply_markup=build_clients_kb_for_trainer(tid, 0))
@dp.message_handler(lambda m: m.text == '📝 Заявки')
async def my_requests(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Вы не тренер.')
        return
    await message.answer('Заявки от клиентов:', reply_markup=build_requests_kb(tid, 0))
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

@dp.message_handler(lambda m: m.text == '🔑 Пригласить клиента')
async def trainer_invite_client(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Только для тренера.'); return
    code = uuid.uuid4().hex[:8].upper()
    cur.execute('UPDATE trainers SET invite_code = ? WHERE id = ?', (code, tid))
    conn.commit()
    text = (
        "Приглашение для клиента:\n"
        f"UUID: `{code}`\n\n"
        "Попросите клиента нажать «🔎 Найти тренера по UUID» и ввести этот код."
    )
    await message.answer(text, parse_mode='Markdown', reply_markup=TRAINER_KB)

@dp.message_handler(lambda m: m.text == '⚙️ Профиль')
async def trainer_profile(message: types.Message):
    tid = get_trainer_id_by_chat(message.chat.id)
    if not tid:
        await message.answer('Вы не тренер.')
        return
    cur.execute('SELECT name, city, pricing, tg_id, username, invite_code FROM trainers WHERE id = ?', (tid,))
    name, city, pricing, tg_id, username, inv = cur.fetchone()
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton('🏙️ Изменить город', callback_data='tprof_city'),
        InlineKeyboardButton('💰 Тарифы/пакеты', callback_data='tprof_pricing')
    )
    txt = (
        f"Профиль тренера: {name}\n"
        f"Город: {city or '-'}\n"
        f"Тарифы (общее описание): {pricing or '-'}\n"
        f"Telegram: @{username or '-'} (id={tg_id})\n"
        f"UUID-приглашение: {inv or '—'}"
    )
    await message.answer(txt, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'tprof_city')
async def tprof_city_start(call: CallbackQuery, state: FSMContext):
    await state.update_data(return_to='trainer_city')
    await SearchCity.query.set()
    await call.message.answer('Введите город для профиля тренера:')
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == 'tprof_pricing')
async def tprof_pricing_menu(call: CallbackQuery):
    tid = get_trainer_id_by_chat(call.message.chat.id)
    if not tid:
        await call.answer('Не тренер', show_alert=True); return
    cur.execute('SELECT id, title, price FROM tariffs WHERE trainer_id = ? ORDER BY id DESC', (tid,))
    tariffs = cur.fetchall()
    text = "Ваши тарифы/пакеты:\n"
    if tariffs:
        for t_id, title, price in tariffs:
            text += f"- [{t_id}] {title} — {price:.2f}\n"
    else:
        text += "— пока пусто."
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton('➕ Добавить тариф', callback_data='tariff:add'))
    if tariffs:
        kb.add(InlineKeyboardButton('🗑 Удалить тариф', callback_data='tariff:delete'))
    await call.message.answer(text, reply_markup=kb)
    await call.answer()

class AddTariff(StatesGroup):
    title = State()
    description = State()
    price = State()

@dp.callback_query_handler(lambda c: c.data == 'tariff:add')
async def tariff_add_start(call: CallbackQuery, state: FSMContext):
    await AddTariff.title.set()
    await call.message.answer('Введите *название* тарифа/пакета:', parse_mode='Markdown')
    await call.answer()

@dp.message_handler(state=AddTariff.title)
async def tariff_add_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await AddTariff.description.set()
    await message.answer('Введите *описание* тарифа/пакета:', parse_mode='Markdown')

@dp.message_handler(state=AddTariff.description)
async def tariff_add_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await AddTariff.price.set()
    await message.answer('Введите *цену* (число):', parse_mode='Markdown')

@dp.message_handler(state=AddTariff.price)
async def tariff_add_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer('Цена должна быть числом. Пример: 1500 или 1500.00')
        return
    data = await state.get_data()
    tid = get_trainer_id_by_chat(message.chat.id)
    cur.execute(
        'INSERT INTO tariffs (trainer_id, title, description, price) VALUES (?, ?, ?, ?)',
        (tid, data['title'], data['description'], price)
    )
    conn.commit()
    await state.finish()
    await message.answer('Тариф добавлен ✅', reply_markup=TRAINER_KB)

class DeleteTariff(StatesGroup):
    tariff_id = State()

@dp.callback_query_handler(lambda c: c.data == 'tariff:delete')
async def tariff_delete_start(call: CallbackQuery, state: FSMContext):
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT id, title FROM tariffs WHERE trainer_id = ? ORDER BY id DESC', (tid,))
    rows = cur.fetchall()
    if not rows:
        await call.message.answer('Удалять нечего — список пуст.')
        await call.answer(); return
    txt = "Введите ID тарифа для удаления:\n" + "\n".join([f"- [{r[0]}] {r[1]}" for r in rows])
    await DeleteTariff.tariff_id.set()
    await call.message.answer(txt)
    await call.answer()

@dp.message_handler(state=DeleteTariff.tariff_id)
async def tariff_delete_confirm(message: types.Message, state: FSMContext):
    try:
        t_id = int(message.text.strip())
    except ValueError:
        await message.answer('Нужно число — ID тарифа.')
        return
    tid = get_trainer_id_by_chat(message.chat.id)
    cur.execute('DELETE FROM tariffs WHERE id = ? AND trainer_id = ?', (t_id, tid))
    conn.commit()
    await state.finish()
    await message.answer('Тариф удалён ✅', reply_markup=TRAINER_KB)

@dp.callback_query_handler(lambda c: c.data.startswith('client:'))
async def cb_client_card(call: CallbackQuery):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT id, name, phone, notes, balance, chat_id, trainer_id, status, tg_id, username FROM clients WHERE id = ?', (cid,))
    r = cur.fetchone()
    if not r:
        await call.answer('Клиент не найден', show_alert=True)
        return
    if r[6] != tid:
        await call.answer('Этот клиент не относится к вам.', show_alert=True)
        return
    text = (
        f"ID: {r[0]}\n"
        f"Имя: {r[1]}\n"
        f"Телефон: {r[2]}\n"
        f"Примечание: {r[3]}\n"
        f"Баланс: {r[4]:.2f}\n"
        f"Chat_id: {r[5]}\n"
        f"TG: @{r[9] or '-'} (id={r[8]})\n"
        f"Статус: {r[7]}"
    )
    await call.message.edit_text(text)
    if r[7] == 'approved':
        await call.message.edit_reply_markup(build_client_card_kb(cid))
    else:
        await call.message.edit_reply_markup(None)
    await call.answer()

# Удаление клиента тренером (с подтверждением)
@dp.callback_query_handler(lambda c: c.data.startswith('delete_client:'))
async def cb_delete_client(call: CallbackQuery):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT name, trainer_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row:
        await call.answer('Клиент не найден', show_alert=True); return
    if row[1] != tid:
        await call.answer('Этот клиент не относится к вам.', show_alert=True); return
    name = row[0]
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton('✅ Да, удалить', callback_data=f"confirm_del_client:{cid}"),
        InlineKeyboardButton('❌ Отмена', callback_data="cancel_del_client")
    )
    await call.message.answer(f'Удалить клиента {name}? Это удалит его тренировки и платежи безвозвратно.', reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == 'cancel_del_client')
async def cb_cancel_del_client(call: CallbackQuery):
    await call.answer('Отменено')
    await call.message.edit_reply_markup(None)

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_del_client:'))
async def cb_confirm_del_client(call: CallbackQuery):
    cid = int(call.data.split(':')[1])
    tid = get_trainer_id_by_chat(call.message.chat.id)
    cur.execute('SELECT trainer_id, chat_id FROM clients WHERE id = ?', (cid,))
    row = cur.fetchone()
    if not row or row[0] != tid:
        await call.answer('Этот клиент не относится к вам.', show_alert=True); return
    client_chat = row[1]
    cur.execute('DELETE FROM sessions WHERE client_id = ?', (cid,))
    cur.execute('DELETE FROM payments WHERE client_id = ?', (cid,))
    cur.execute('DELETE FROM clients WHERE id = ?', (cid,))
    conn.commit()
    await call.answer('Клиент удалён ✅')
    await call.message.edit_reply_markup(None)
    await call.message.answer('Клиент и связанные записи удалены.', reply_markup=TRAINER_KB)
    if client_chat:
        try:
            await bot.send_message(client_chat, 'Ваш профиль у тренера был удалён. Вы можете выбрать другого тренера.', reply_markup=CLIENT_KB)
        except Exception:
            pass

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
    def iso(day, h): return (day + timedelta(hours=h)).isoformat()
    kb = InlineKeyboardMarkup(row_width=3)
    kb.row(
        InlineKeyboardButton('Сегодня 09:00', callback_data=f"slot:{cid}:{iso(today,9)}"),
        InlineKeyboardButton('Сегодня 12:00', callback_data=f"slot:{cid}:{iso(today,12)}"),
        InlineKeyboardButton('Сегодня 18:00', callback_data=f"slot:{cid}:{iso(today,18)}")
    )
    kb.row(
        InlineKeyboardButton('Завтра 09:00', callback_data=f"slot:{cid}:{iso(tomorrow,9)}"),
        InlineKeyboardButton('Завтра 12:00', callback_data=f"slot:{cid}:{iso(tomorrow,12)}"),
        InlineKeyboardButton('Завтра 18:00', callback_data=f"slot:{cid}:{iso(tomorrow,18)}")
    )
    kb.add(InlineKeyboardButton('📝 Ввести вручную', callback_data='slot_manual'))
    await call.message.answer('Выберите слот или введите вручную:', reply_markup=kb)
    await AddSession.when.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('slot:'))
async def cb_pick_slot(call: CallbackQuery, state: FSMContext):
    _, cid, dt_iso = call.data.split(':', 2)
    await state.update_data(client_id=int(cid), when=datetime.fromisoformat(dt_iso))
    await AddSession.comment.set()
    await call.message.answer('Комментарий (или "-" чтобы пропустить):')
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == 'slot_manual')
async def cb_slot_manual(call: CallbackQuery):
    await call.message.answer('Ожидаю ввод даты и времени в формате: ДД.ММ.ГГГГ ЧЧ:ММ (24ч).\nНапример: 12.08.2025 18:00')
    await call.answer()

@dp.message_handler(state=AddSession.when)
async def st_add_session_when(message: types.Message, state: FSMContext):
    try:
        dt = parse_dt(message.text)
    except Exception:
        await message.answer('Не удалось распознать дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ. Пример: 12.08.2025 18:00')
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
    await message.answer(f"Сессия добавлена (id={sid}) на {data['when'].strftime('%d.%m.%Y %H:%М')}", reply_markup=TRAINER_KB)

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
    for sid, _, dt_iso, status, cname in rows[:50]:
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

# --- Заглушки для будущих разделов ---
@dp.message_handler(lambda m: m.text == '📈 Статистика')
async def stats_stub(message: types.Message):
    await message.answer('Статистика появится в следующей версии 🙂', reply_markup=TRAINER_KB)
@dp.message_handler(lambda m: m.text == '💸 Должники')
async def debtors_stub(message: types.Message):
    await message.answer('Список должников появится в следующей версии 🙂', reply_markup=TRAINER_KB)

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
                txt = f"Напоминание: тренировка {dt.strftime('%d.%m.%Y %H:%М')} — {client_name} (id={cid})."
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

# --- Startup ---
async def on_startup(dp):
    asyncio.create_task(reminders_loop())
    logger.info('on_startup finished — reminders loop scheduled.')

if __name__ == '__main__':
    logger.info('Bot is starting...')
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)