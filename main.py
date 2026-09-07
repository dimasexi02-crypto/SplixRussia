"""
Бот конкурсов и розыгрышей (с гарантированной активацией админки)
===================================================================

КОМАНДА ДЛЯ АКТИВАЦИИ АДМИНА:
    /claim  — назначает вас владельцем/админом бота и открывает Админ-панель!

ГЛАВНОЕ МЕНЮ (для админа):
   🎲 Создать конкурс
   💳 Мои конкурсы
   📁 Мои каналы/чаты
   💬 Служба поддержки
   🛠 Админ-панель

ВОЗМОЖНОСТИ АДМИН-ПАНЕЛИ:
   📊 Статистика бота
   📢 Массовая рассылка (текст/фото/видео всем пользователям)

УСТАНОВКА:
    pip install -U aiogram aiosqlite python-dotenv

ЗАПУСК:
    python contest_bot.py
"""

import asyncio
import logging
import random
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "contest_bot.db"

TOKEN_PATTERN = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")


def load_token_from_env() -> str | None:
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def save_token_to_env(token: str) -> None:
    ENV_PATH.write_text(f"BOT_TOKEN={token}\n", encoding="utf-8")


def ask_token_in_console() -> str:
    print("=" * 60)
    print(" Токен бота не найден.")
    print(" Получите его у @BotFather в Telegram и вставьте ниже.")
    print(" Формат: 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxx")
    print("=" * 60)
    while True:
        token = input("Вставьте токен и нажмите Enter: ").strip()
        if TOKEN_PATTERN.match(token):
            return token
        print("⚠️  Токен выглядит некорректно. Попробуйте ещё раз.\n")


BOT_TOKEN = load_token_from_env()
if not BOT_TOKEN:
    BOT_TOKEN = "8922510024:AAGQHPfQkaUcIIWXb_Dmf2S-1607dDiSjIc"
    save_token_to_env(BOT_TOKEN)
    print(f"✅ Токен сохранён в {ENV_PATH}. При следующих запусках спрашивать не буду.\n")

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

db: aiosqlite.Connection | None = None
BOT_USERNAME = ""


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)

    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined_at TEXT,
            is_owner INTEGER DEFAULT 0
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            chat_id INTEGER,
            title TEXT,
            username TEXT,
            added_at TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            target_chat_id INTEGER,
            target_chat_title TEXT,
            message_id INTEGER,
            text TEXT,
            winners_count INTEGER,
            end_type TEXT,
            end_date TEXT,
            end_count INTEGER,
            button_text TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT,
            finished_at TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            contest_id INTEGER,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            joined_at TEXT,
            PRIMARY KEY (contest_id, user_id)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS winners (
            contest_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (contest_id, user_id)
        )
        """
    )
    await db.commit()


async def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    if row is None:
        cur2 = await db.execute("SELECT COUNT(*) FROM users")
        (count,) = await cur2.fetchone()
        is_owner_flag = 1 if count == 0 else 0
        await db.execute(
            "INSERT INTO users (user_id, username, full_name, joined_at, is_owner) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, datetime.utcnow().isoformat(), is_owner_flag),
        )
    else:
        await db.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
    await db.commit()


async def set_owner(user_id: int) -> None:
    await db.execute("UPDATE users SET is_owner = 1 WHERE user_id = ?", (user_id,))
    await db.commit()


async def is_owner(user_id: int) -> bool:
    cur = await db.execute("SELECT is_owner FROM users WHERE user_id = ?", (user_id,))
    row = await cur.fetchone()
    return bool(row and row[0] == 1)


async def get_owner_id() -> int | None:
    cur = await db.execute("SELECT user_id FROM users WHERE is_owner = 1 LIMIT 1")
    row = await cur.fetchone()
    return row[0] if row else None


async def get_all_user_ids() -> list[int]:
    cur = await db.execute("SELECT user_id FROM users")
    rows = await cur.fetchall()
    return [r[0] for r in rows]


# ---- Каналы ----

async def add_channel(owner_id: int, chat_id: int, title: str, username: str | None) -> int:
    cur = await db.execute(
        "INSERT INTO channels (owner_id, chat_id, title, username, added_at) VALUES (?, ?, ?, ?, ?)",
        (owner_id, chat_id, title, username, datetime.utcnow().isoformat()),
    )
    await db.commit()
    return cur.lastrowid


async def get_user_channels(owner_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT id, chat_id, title, username FROM channels WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    )
    rows = await cur.fetchall()
    return [{"id": r[0], "chat_id": r[1], "title": r[2], "username": r[3]} for r in rows]


async def get_channel(channel_id: int) -> dict | None:
    cur = await db.execute(
        "SELECT id, owner_id, chat_id, title, username FROM channels WHERE id = ?", (channel_id,)
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "owner_id": row[1], "chat_id": row[2], "title": row[3], "username": row[4]}


async def delete_channel(channel_id: int, owner_id: int) -> None:
    await db.execute("DELETE FROM channels WHERE id = ? AND owner_id = ?", (channel_id, owner_id))
    await db.commit()


# ---- Конкурсы ----

async def create_contest(
    owner_id: int,
    target_chat_id: int | None,
    target_chat_title: str | None,
    text: str,
    winners_count: int,
    end_type: str,
    end_date: str | None,
    end_count: int | None,
    button_text: str,
) -> int:
    cur = await db.execute(
        """
        INSERT INTO contests
        (owner_id, target_chat_id, target_chat_title, text, winners_count,
         end_type, end_date, end_count, button_text, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            owner_id, target_chat_id, target_chat_title, text, winners_count,
            end_type, end_date, end_count, button_text, datetime.utcnow().isoformat(),
        ),
    )
    await db.commit()
    return cur.lastrowid


async def set_contest_message(contest_id: int, message_id: int) -> None:
    await db.execute("UPDATE contests SET message_id = ? WHERE id = ?", (message_id, contest_id))
    await db.commit()


async def get_contest(contest_id: int) -> dict | None:
    cur = await db.execute(
        """
        SELECT id, owner_id, target_chat_id, target_chat_title, message_id, text,
               winners_count, end_type, end_date, end_count, button_text, status
        FROM contests WHERE id = ?
        """,
        (contest_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    keys = [
        "id", "owner_id", "target_chat_id", "target_chat_title", "message_id", "text",
        "winners_count", "end_type", "end_date", "end_count", "button_text", "status",
    ]
    return dict(zip(keys, row))


async def get_user_contests(owner_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT id, text, status, winners_count FROM contests WHERE owner_id = ? ORDER BY id DESC",
        (owner_id,),
    )
    rows = await cur.fetchall()
    return [{"id": r[0], "text": r[1], "status": r[2], "winners_count": r[3]} for r in rows]


async def get_expired_date_contests() -> list[dict]:
    now = datetime.utcnow().isoformat()
    cur = await db.execute(
        "SELECT id FROM contests WHERE status = 'active' AND end_type = 'date' AND end_date <= ?",
        (now,),
    )
    rows = await cur.fetchall()
    result = []
    for (cid,) in rows:
        c = await get_contest(cid)
        if c:
            result.append(c)
    return result


async def delete_contest(contest_id: int, owner_id: int) -> bool:
    cur = await db.execute("SELECT owner_id FROM contests WHERE id = ?", (contest_id,))
    row = await cur.fetchone()
    if not row or row[0] != owner_id:
        return False
    await db.execute("DELETE FROM contests WHERE id = ?", (contest_id,))
    await db.execute("DELETE FROM participants WHERE contest_id = ?", (contest_id,))
    await db.execute("DELETE FROM winners WHERE contest_id = ?", (contest_id,))
    await db.commit()
    return True


async def finish_contest_db(contest_id: int, winner_ids: list[int]) -> None:
    await db.execute(
        "UPDATE contests SET status = 'finished', finished_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), contest_id),
    )
    for uid in winner_ids:
        await db.execute(
            "INSERT OR IGNORE INTO winners (contest_id, user_id) VALUES (?, ?)", (contest_id, uid)
        )
    await db.commit()


# ---- Участники ----

async def add_participant(contest_id: int, user_id: int, username: str | None, full_name: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO participants (contest_id, user_id, username, full_name, joined_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (contest_id, user_id, username, full_name, datetime.utcnow().isoformat()),
    )
    await db.commit()


async def is_participant(contest_id: int, user_id: int) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM participants WHERE contest_id = ? AND user_id = ?", (contest_id, user_id)
    )
    return (await cur.fetchone()) is not None


async def count_participants(contest_id: int) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM participants WHERE contest_id = ?", (contest_id,))
    (count,) = await cur.fetchone()
    return count


async def get_participants(contest_id: int) -> list[dict]:
    cur = await db.execute(
        "SELECT user_id, username, full_name FROM participants WHERE contest_id = ?", (contest_id,)
    )
    rows = await cur.fetchall()
    return [{"user_id": r[0], "username": r[1], "full_name": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

async def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Создать конкурс 🎲"), KeyboardButton(text="Мои конкурсы 💳")],
        [KeyboardButton(text="Мои каналы/чаты 📁"), KeyboardButton(text="Служба поддержки 💬")],
    ]
    if await is_owner(user_id):
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def channel_join_url_kb(bot_username: str, contest_id: int, button_text: str) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=c_{contest_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=url)]]
    )


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
            [InlineKeyboardButton(text="📢 Рассылка всем", callback_data="adm_broadcast")],
        ]
    )


# ---------------------------------------------------------------------------
# FSM состояния
# ---------------------------------------------------------------------------

class AddChannel(StatesGroup):
    waiting = State()


class CreateContest(StatesGroup):
    choose_target = State()
    text = State()
    winners_count = State()
    end_type = State()
    end_value = State()
    button_text = State()


class SupportStates(StatesGroup):
    waiting_message = State()


class BroadcastStates(StatesGroup):
    waiting_content = State()
    confirm = State()


# ---------------------------------------------------------------------------
# Логика подведения итогов
# ---------------------------------------------------------------------------

def mention_html(user_id: int, username: str | None, full_name: str | None) -> str:
    name = full_name or username or str(user_id)
    if username:
        return f'<a href="https://t.me/{username}">{name}</a>'
    return f'<a href="tg://user?id={user_id}">{name}</a>'


async def perform_finish(contest: dict) -> None:
    contest_id = contest["id"]
    participants = await get_participants(contest_id)

    winners_count = min(contest["winners_count"], len(participants))
    winners = random.sample(participants, winners_count) if winners_count > 0 else []
    winner_ids = [w["user_id"] for w in winners]

    await finish_contest_db(contest_id, winner_ids)

    if contest["target_chat_id"] and contest["message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=contest["target_chat_id"], message_id=contest["message_id"], reply_markup=None
            )
        except TelegramBadRequest:
            pass

    if winners:
        winners_text = "\n".join(
            f"🏆 {mention_html(w['user_id'], w['username'], w['full_name'])}" for w in winners
        )
        result_text = (
            f"🎉 <b>Конкурс завершён!</b>\n\n"
            f"Победители ({len(winners)}):\n{winners_text}\n\n"
            "Поздравляем! 🥳"
        )
    else:
        result_text = "😔 Конкурс завершён, но участников не было."

    if contest["target_chat_id"]:
        try:
            await bot.send_message(contest["target_chat_id"], result_text)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    else:
        try:
            await bot.send_message(contest["owner_id"], result_text)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass

    for w in winners:
        try:
            await bot.send_message(
                w["user_id"],
                f"🎉🎊 <b>Поздравляем!</b>\n\nВы выиграли в конкурсе!\n\n{contest['text'][:200]}",
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


async def check_and_autofinish_by_count(contest_id: int) -> None:
    contest = await get_contest(contest_id)
    if not contest or contest["status"] != "active" or contest["end_type"] != "count":
        return
    current = await count_participants(contest_id)
    if current >= contest["end_count"]:
        await perform_finish(contest)


# ---------------------------------------------------------------------------
# АКТИВАЦИЯ АДМИНА
# ---------------------------------------------------------------------------

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await set_owner(message.from_user.id)
    kb = await main_menu_kb(message.from_user.id)
    await message.answer(
        "👑 <b>Поздравляем! Вы активировали права Главного Администратора бота.</b>\n\n"
        "Теперь вам доступна кнопка «🛠 Админ-панель» в меню.",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    kb = await main_menu_kb(message.from_user.id)

    parts = message.text.split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""

    if payload.startswith("c_"):
        try:
            contest_id = int(payload[2:])
        except ValueError:
            contest_id = None

        contest = await get_contest(contest_id) if contest_id else None
        if not contest:
            await message.answer("⚠️ Конкурс не найден.", reply_markup=kb)
            return

        if contest["status"] != "active":
            await message.answer(
                f"⛔ Конкурс «<b>{contest['text'][:50]}...</b>» уже завершён.",
                reply_markup=kb,
            )
            return

        already = await is_participant(contest["id"], message.from_user.id)
        if already:
            await message.answer("ℹ️ Вы уже участвуете в этом конкурсе! ✅", reply_markup=kb)
        else:
            await add_participant(
                contest["id"],
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
            )
            count = await count_participants(contest["id"])
            await message.answer(
                f"✅ <b>Вы успешно приняли участие в конкурсе!</b>\n\n"
                f"👥 Всего участников: <b>{count}</b>\n\n"
                f"Желаем удачи! 🎉",
                reply_markup=kb,
            )
            await check_and_autofinish_by_count(contest["id"])
        return

    await message.answer(
        "👋 Добро пожаловать в бота конкурсов и розыгрышей!\n\n"
        "🎲 <b>Создать конкурс</b> — запустите свой конкурс и опубликуйте его в канале\n"
        "💳 <b>Мои конкурсы</b> — управление созданными конкурсами\n"
        "📁 <b>Мои каналы/чаты</b> — подключите канал, чтобы бот мог туда публиковать\n"
        "💬 <b>Служба поддержки</b> — если есть вопросы, напишите нам",
        reply_markup=kb,
    )


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


# ---------------------------------------------------------------------------
# 🛠 Админ-панель
# ---------------------------------------------------------------------------

@dp.message(F.text.startswith("🛠 Админ-панель"))
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_owner(message.from_user.id):
        return await message.answer("⛔ У вас нет админ-прав. Введите /claim чтобы получить их.")
    await message.answer("🛠 <b>Панель администратора</b>", reply_markup=admin_panel_kb())


@dp.callback_query(F.data == "adm_stats")
async def adm_stats(call: CallbackQuery):
    if not await is_owner(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    cur = await db.execute("SELECT COUNT(*) FROM users")
    (total_users,) = await cur.fetchone()
    cur = await db.execute("SELECT COUNT(*) FROM contests")
    (total_contests,) = await cur.fetchone()
    cur = await db.execute("SELECT COUNT(*) FROM contests WHERE status = 'active'")
    (active_contests,) = await cur.fetchone()
    cur = await db.execute("SELECT COUNT(*) FROM channels")
    (total_channels,) = await cur.fetchone()
    cur = await db.execute("SELECT COUNT(*) FROM participants")
    (total_participations,) = await cur.fetchone()

    await call.message.answer(
        "📊 <b>Общая статистика бота</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"🎲 Всего конкурсов: <b>{total_contests}</b>\n"
        f"🟢 Активных конкурсов: <b>{active_contests}</b>\n"
        f"📁 Подключено каналов/чатов: <b>{total_channels}</b>\n"
        f"✍️ Всего участий: <b>{total_participations}</b>"
    )


@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not await is_owner(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await state.set_state(BroadcastStates.waiting_content)
    await call.message.answer(
        "📢 Отправьте сообщение для рассылки (текст, фото, видео).\n"
        "Оно будет отправлено всем пользователям бота.\n\n"
        "Отмена: /cancel"
    )


@dp.message(StateFilter(BroadcastStates.waiting_content))
async def broadcast_content_received(message: Message, state: FSMContext):
    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(BroadcastStates.confirm)
    users = await get_all_user_ids()
    await message.answer(
        f"Разослать сообщение <b>{len(users)}</b> пользователям?\n\n"
        "Отправьте <b>да</b> для отправки или /cancel для отмены."
    )


@dp.message(StateFilter(BroadcastStates.confirm), F.text.lower() == "да")
async def broadcast_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    users = await get_all_user_ids()
    status_msg = await message.answer(f"⏳ Рассылка запущена для {len(users)} пользователей...")

    sent, failed = 0, 0
    for uid in users:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=data["from_chat_id"], message_id=data["message_id"])
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.answer(f"✅ Рассылка завершена!\n\nДоставлено: {sent}\nНе доставлено: {failed}")


# ---------------------------------------------------------------------------
# 📁 Мои каналы/чаты
# ---------------------------------------------------------------------------

def channels_list_kb(channels: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"🗑 {c['title']}", callback_data=f"chdel_{c['id']}")]
        for c in channels
    ]
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал/чат", callback_data="chadd")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text.startswith("Мои каналы/чаты"))
async def my_channels(message: Message):
    channels = await get_user_channels(message.from_user.id)
    if channels:
        text = "📁 <b>Ваши подключённые каналы/чаты:</b>\n\nНажмите на канал, чтобы удалить его из списка."
    else:
        text = "У вас пока нет подключённых каналов/чатов."
    await message.answer(text, reply_markup=channels_list_kb(channels))


@dp.callback_query(F.data == "chadd")
async def channel_add_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AddChannel.waiting)
    await call.message.answer(
        "🛠 <b>Подключение канала/чата</b>\n\n"
        "1️⃣ Добавьте этого бота администратором в ваш канал или группу "
        "с правом <b>публикации сообщений</b>.\n"
        "2️⃣ Перешлите сюда любое сообщение ИЗ этого канала/чата, "
        "либо отправьте его @username.\n\n"
        "Отмена: /cancel"
    )


@dp.callback_query(F.data.startswith("chdel_"))
async def channel_delete(call: CallbackQuery):
    channel_id = int(call.data.split("_", 1)[1])
    await delete_channel(channel_id, call.from_user.id)
    await call.answer("Канал удалён ✅")
    channels = await get_user_channels(call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=channels_list_kb(channels))
    except TelegramBadRequest:
        pass


@dp.message(StateFilter(AddChannel.waiting))
async def channel_add_process(message: Message, state: FSMContext):
    chat_ref = None

    if message.forward_from_chat:
        chat_ref = message.forward_from_chat.id
    elif message.text:
        text = message.text.strip()
        chat_ref = text

    if not chat_ref:
        return await message.answer(
            "Не понял. Перешлите сообщение из канала/чата, либо отправьте его @username."
        )

    try:
        chat = await bot.get_chat(chat_ref)
        member = await bot.get_chat_member(chat_id=chat.id, user_id=(await bot.get_me()).id)
    except TelegramBadRequest as e:
        return await message.answer(
            f"⚠️ Не удалось найти канал/чат или бот туда не добавлен.\nОшибка: {e}"
        )

    if member.status not in ("administrator", "creator"):
        return await message.answer(
            "⚠️ Бот не является администратором этого канала/чата. "
            "Добавьте его в администраторы и попробуйте снова."
        )

    can_post = getattr(member, "can_post_messages", True)
    if chat.type == ChatType.CHANNEL and can_post is False:
        return await message.answer("⚠️ У бота нет права публиковать сообщения в этом канале.")

    await add_channel(message.from_user.id, chat.id, chat.title or "Без названия", chat.username)
    await state.clear()
    await message.answer(f"✅ Канал/чат «{chat.title}» успешно подключён!")


# ---------------------------------------------------------------------------
# 🎲 Создание конкурса
# ---------------------------------------------------------------------------

async def target_choice_kb(owner_id: int) -> InlineKeyboardMarkup:
    channels = await get_user_channels(owner_id)
    buttons = [
        [InlineKeyboardButton(text=f"📢 {c['title']}", callback_data=f"ctgt_{c['id']}")]
        for c in channels
    ]
    buttons.append([InlineKeyboardButton(text="🤖 Только в боте (по ссылке)", callback_data="ctgt_bot")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text.startswith("Создать конкурс"))
async def create_contest_start(message: Message, state: FSMContext):
    await state.set_state(CreateContest.choose_target)
    kb = await target_choice_kb(message.from_user.id)
    await message.answer(
        "🎲 <b>Создание конкурса</b>\n\n"
        "Куда опубликовать конкурс?\n\n"
        "Для отмены — /cancel",
        reply_markup=kb,
    )


@dp.callback_query(StateFilter(CreateContest.choose_target), F.data.startswith("ctgt_"))
async def create_contest_target_chosen(call: CallbackQuery, state: FSMContext):
    await call.answer()
    value = call.data.split("_", 1)[1]

    if value == "bot":
        await state.update_data(target_chat_id=None, target_chat_title=None)
    else:
        channel = await get_channel(int(value))
        if not channel or channel["owner_id"] != call.from_user.id:
            return await call.message.answer("⚠️ Канал не найден.")
        await state.update_data(target_chat_id=channel["chat_id"], target_chat_title=channel["title"])

    await state.set_state(CreateContest.text)
    await call.message.answer(
        "✍️ Введите текст конкурса (описание, призы, условия — всё, что увидят участники):"
    )


@dp.message(Command("cancel"))
async def cancel_any(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    kb = await main_menu_kb(message.from_user.id)
    await message.answer("❌ Действие отменено.", reply_markup=kb)


@dp.message(StateFilter(CreateContest.text))
async def create_contest_text(message: Message, state: FSMContext):
    await state.update_data(text=message.html_text or message.text)
    await state.set_state(CreateContest.winners_count)
    await message.answer("🏆 Сколько будет победителей? (введите число)")


@dp.message(StateFilter(CreateContest.winners_count))
async def create_contest_winners(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1:
        return await message.answer("Введите положительное число, например 1")
    await state.update_data(winners_count=int(text))
    await state.set_state(CreateContest.end_type)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 По дате и времени", callback_data="et_date")],
            [InlineKeyboardButton(text="🔢 По числу участников", callback_data="et_count")],
        ]
    )
    await message.answer("⏳ Как определить момент завершения конкурса?", reply_markup=kb)


@dp.callback_query(StateFilter(CreateContest.end_type), F.data.in_(["et_date", "et_count"]))
async def create_contest_end_type(call: CallbackQuery, state: FSMContext):
    await call.answer()
    end_type = "date" if call.data == "et_date" else "count"
    await state.update_data(end_type=end_type)
    await state.set_state(CreateContest.end_value)
    if end_type == "date":
        await call.message.answer(
            "Введите дату и время завершения в формате:\n<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
            "Например: <code>25.12.2026 18:00</code>\n\n(время указывайте по UTC)"
        )
    else:
        await call.message.answer(
            "Введите нужное количество участников (число), при достижении — розыгрыш подведётся автоматически:"
        )


@dp.message(StateFilter(CreateContest.end_value))
async def create_contest_end_value(message: Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()

    if data["end_type"] == "date":
        try:
            dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
        except ValueError:
            return await message.answer("⚠️ Неверный формат. Пример: 25.12.2026 18:00")
        await state.update_data(end_date=dt.isoformat(), end_count=None)
    else:
        if not text.isdigit() or int(text) < 1:
            return await message.answer("Введите положительное число.")
        await state.update_data(end_count=int(text), end_date=None)

    await state.set_state(CreateContest.button_text)
    await message.answer("🔘 Введите текст кнопки участия (или «-» для стандартного «🎉 Участвовать»):")


@dp.message(StateFilter(CreateContest.button_text))
async def create_contest_button_text(message: Message, state: FSMContext):
    text = message.text.strip()
    button_text = "🎉 Участвовать" if text == "-" else text
    data = await state.get_data()
    await state.clear()

    contest_id = await create_contest(
        owner_id=message.from_user.id,
        target_chat_id=data.get("target_chat_id"),
        target_chat_title=data.get("target_chat_title"),
        text=data["text"],
        winners_count=data["winners_count"],
        end_type=data["end_type"],
        end_date=data.get("end_date"),
        end_count=data.get("end_count"),
        button_text=button_text,
    )

    kb = await main_menu_kb(message.from_user.id)

    if data.get("target_chat_id"):
        try:
            sent = await bot.send_message(
                data["target_chat_id"],
                data["text"],
                reply_markup=channel_join_url_kb(BOT_USERNAME, contest_id, button_text),
            )
            await set_contest_message(contest_id, sent.message_id)
            await message.answer(
                f"✅ Конкурс опубликован в «{data['target_chat_title']}»!",
                reply_markup=kb,
            )
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            await message.answer(
                f"⚠️ Конкурс создан (ID {contest_id}), но опубликовать в канал не удалось: {e}\n"
                "Проверьте права бота в канале.",
                reply_markup=kb,
            )
    else:
        link = f"https://t.me/{BOT_USERNAME}?start=c_{contest_id}"
        await message.answer(
            f"✅ Конкурс создан!\n\n🔗 Ссылка для приглашения участников:\n{link}",
            reply_markup=kb,
        )


# ---------------------------------------------------------------------------
# 📇 Мои конкурсы
# ---------------------------------------------------------------------------

def my_contests_kb(contests: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for c in contests:
        emoji = "🟢" if c["status"] == "active" else "⚪️"
        title_preview = re.sub("<[^<]+?>", "", c["text"])[:30]
        buttons.append(
            [InlineKeyboardButton(text=f"{emoji} {title_preview}...", callback_data=f"cview_{c['id']}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text.startswith("Мои конкурсы"))
async def my_contests(message: Message):
    contests = await get_user_contests(message.from_user.id)
    if not contests:
        return await message.answer("У вас пока нет созданных конкурсов.")
    await message.answer("💳 <b>Ваши конкурсы:</b>", reply_markup=my_contests_kb(contests))


@dp.callback_query(F.data.startswith("cview_"))
async def contest_view(call: CallbackQuery):
    contest_id = int(call.data.split("_", 1)[1])
    contest = await get_contest(contest_id)
    if not contest or contest["owner_id"] != call.from_user.id:
        return await call.answer("Конкурс не найден", show_alert=True)

    await call.answer()
    count = await count_participants(contest_id)
    status_line = "🟢 Активен" if contest["status"] == "active" else "⚪️ Завершён"

    text = (
        f"{status_line}\n👥 Участников: {count}\n🏆 Победителей: {contest['winners_count']}\n\n"
        f"{contest['text'][:500]}"
    )

    buttons = []
    if contest["status"] == "active":
        buttons.append([InlineKeyboardButton(text="🏁 Завершить сейчас", callback_data=f"cfin_{contest_id}")])
    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"cdel_{contest_id}")])

    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("cfin_"))
async def contest_finish_manual(call: CallbackQuery):
    contest_id = int(call.data.split("_", 1)[1])
    contest = await get_contest(contest_id)
    if not contest or contest["owner_id"] != call.from_user.id:
        return await call.answer("Нет доступа", show_alert=True)
    if contest["status"] != "active":
        return await call.answer("Уже завершён", show_alert=True)

    await call.answer("Подвожу итоги...")
    await perform_finish(contest)
    await call.message.answer("✅ Конкурс завершён, итоги подведены и опубликованы.")


@dp.callback_query(F.data.startswith("cdel_"))
async def contest_delete_cb(call: CallbackQuery):
    contest_id = int(call.data.split("_", 1)[1])
    ok = await delete_contest(contest_id, call.from_user.id)
    if ok:
        await call.answer("Конкурс удалён ✅", show_alert=True)
    else:
        await call.answer("Не удалось удалить", show_alert=True)


# ---------------------------------------------------------------------------
# 💬 Служба поддержки
# ---------------------------------------------------------------------------

@dp.message(F.text.startswith("Служба поддержки"))
async def support_start(message: Message, state: FSMContext):
    await state.set_state(SupportStates.waiting_message)
    await message.answer(
        "💬 Опишите ваш вопрос одним сообщением — мы передадим его в поддержку и ответим прямо сюда.\n\n"
        "Отмена: /cancel"
    )


@dp.message(StateFilter(SupportStates.waiting_message))
async def support_forward(message: Message, state: FSMContext):
    await state.clear()
    owner_id = await get_owner_id()
    kb = await main_menu_kb(message.from_user.id)
    if owner_id:
        try:
            await bot.send_message(
                owner_id,
                f"📩 <b>Вопрос от пользователя</b>\n"
                f"ID: <code>{message.from_user.id}</code>\n"
                f"Имя: {message.from_user.full_name}\n\n"
                f"Ответить: <code>/reply {message.from_user.id} текст</code>",
            )
            await bot.copy_message(owner_id, message.chat.id, message.message_id)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    await message.answer("✅ Ваше сообщение отправлено в поддержку. Ожидайте ответа!", reply_markup=kb)


@dp.message(Command("reply"))
async def owner_reply(message: Message):
    if not await is_owner(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await message.answer("Использование: /reply ID текст")
    target_id, text = int(parts[1]), parts[2]
    try:
        await bot.send_message(target_id, f"💬 <b>Ответ от поддержки:</b>\n\n{text}")
        await message.answer("✅ Отправлено.")
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        await message.answer(f"⚠️ Не удалось отправить: {e}")


# ---------------------------------------------------------------------------
# Фоновая проверка
# ---------------------------------------------------------------------------

async def background_checker():
    while True:
        try:
            expired = await get_expired_date_contests()
            for contest in expired:
                await perform_finish(contest)
        except Exception as e:
            logging.exception("Ошибка в фоновой проверке: %s", e)
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main():
    global BOT_USERNAME
    await init_db()
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        print(f"✅ Успешно подключились к Telegram как @{me.username}")
    except TelegramUnauthorizedError:
        print("❌ Токен неверный (Telegram отклонил его).")
        print(f"   Удалите файл {ENV_PATH} и запустите скрипт заново.")
        sys.exit(1)

    asyncio.create_task(background_checker())

    print("🚀 Бот запущен. Напишите ему /start в Telegram.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
