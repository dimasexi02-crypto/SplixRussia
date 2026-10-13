# -*- coding: utf-8 -*-
# Бот розыгрышей (аналог Best Random Bot)
# pip install "aiogram>=3.4"
# Запуск: python giveaway_bot.py

import asyncio, random, sqlite3, logging, time, html
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup,
                           InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

# ============================ НАСТРОЙКИ ============================
TOKEN = "8759205415:AAH-L2s9N_98-F6sUatGphN8pLUegvwDERU"
ADMINS = {8018644395}                 # id админов бота (доступ к /admin)
SUPPORT = "@splixofficial"            # куда ведёт "Служба поддержки"
TZ = timezone(timedelta(hours=3))    # часовой пояс для дат (МСК)
# ==================================================================

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
BOT_ID = 0
BOT_USERNAME = ""
pending_captcha: dict[int, tuple[int, int]] = {}   # user_id -> (giveaway_id, answer)

# ============================ БАЗА ============================
db = sqlite3.connect("bot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    joined_at INTEGER, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(
    chat_id INTEGER PRIMARY KEY, owner_id INTEGER, title TEXT, username TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS giveaways(
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, text TEXT, photo TEXT,
    channels TEXT DEFAULT '', winners INTEGER DEFAULT 1, chat_id INTEGER, message_id INTEGER,
    publish_at INTEGER, end_type TEXT, end_value INTEGER, captcha INTEGER DEFAULT 0,
    button_pos TEXT DEFAULT 'under', status TEXT DEFAULT 'scheduled',
    winners_ids TEXT DEFAULT '', created_at INTEGER);
CREATE TABLE IF NOT EXISTS participants(
    giveaway_id INTEGER, user_id INTEGER, joined_at INTEGER,
    PRIMARY KEY(giveaway_id, user_id));
""")
db.commit()

def esc(s): return html.escape(str(s or ""))

def upsert_user(u):
    db.execute("""INSERT INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                  first_name=excluded.first_name, blocked=0""",
               (u.id, u.username, u.first_name, int(time.time())))
    db.commit()

def get_gw(gid: int):
    r = db.execute("SELECT * FROM giveaways WHERE id=?", (gid,)).fetchone()
    if not r: return None
    gw = dict(r)
    gw["channels"] = [int(x) for x in (r["channels"] or "").split(",") if x]
    return gw

def count_participants(gid: int) -> int:
    return db.execute("SELECT COUNT(*) FROM participants WHERE giveaway_id=?", (gid,)).fetchone()[0]

def user_channels(uid: int):
    return db.execute("SELECT * FROM channels WHERE owner_id=? ORDER BY title", (uid,)).fetchall()

def get_channel(chat_id: int):
    return db.execute("SELECT * FROM channels WHERE chat_id=?", (chat_id,)).fetchone()

def ch_html(chat_id: int) -> str:
    c = get_channel(chat_id)
    if not c: return str(chat_id)
    return f'<a href="{c["link"]}">{esc(c["title"])}</a>' if c["link"] else esc(c["title"])

def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")

def parse_dt(s: str):
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%d.%m %H:%M": dt = dt.replace(year=datetime.now(TZ).year)
            return int(dt.replace(tzinfo=TZ).timestamp())
        except ValueError:
            pass
    return None

def mention(uid: int) -> str:
    u = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if u and u["username"]: return f"@{u['username']}"
    name = u["first_name"] if u else "участник"
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'

STATUS = {"scheduled": "⏰ Запланирован", "active": "🟢 Идёт", "finished": "🏁 Завершён"}

# ============================ КЛАВИАТУРЫ ============================
def main_kb():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="Создать конкурс 🎲"), KeyboardButton(text="Мои конкурсы 🗂")],
        [KeyboardButton(text="Мои каналы/чаты 📁"), KeyboardButton(text="Служба поддержки 💬")],
    ])

def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) if not d.startswith("http") else
         InlineKeyboardButton(text=t, url=d) for t, d in row] for row in rows])

# ============================ TELEGRAM-ХЕЛПЕРЫ ============================
async def bot_is_admin(chat_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, BOT_ID)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

async def save_channel(chat, owner_id: int):
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try: link = await bot.export_chat_invite_link(chat.id)
        except Exception: link = None
    db.execute("INSERT OR REPLACE INTO channels(chat_id, owner_id, title, username, link) VALUES(?,?,?,?,?)",
               (chat.id, owner_id, chat.title, chat.username, link))
    db.commit()

async def resolve_chat(text: str):
    """@name / t.me/name / -100id -> Chat или None"""
    t = text.strip()
    if t.lstrip("-").isdigit():
        ref = int(t)
    else:
        t = t.replace("https://", "").replace("http://", "").replace("t.me/", "").lstrip("@").split("/")[0]
        if not t: return None
        ref = "@" + t
    try: return await bot.get_chat(ref)
    except Exception: return None

async def missing_subs(user_id: int, channels: list) -> list:
    missing = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not m.is_member):
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return missing

def build_post(gw: dict, count: int):
    text = gw["text"]
    if gw["channels"]:
        text += "\n\n📣 <b>Условия:</b> подписаться на " + ", ".join(ch_html(c) for c in gw["channels"])
    if gw["winners"] > 1: text += f"\n🏆 Победителей: {gw['winners']}"
    if gw["end_type"] == "time": text += f"\n⏰ Итоги: {fmt_dt(gw['end_value'])}"
    elif gw["end_type"] == "count": text += f"\n👥 Итоги при {gw['end_value']} участниках"
    link = f"https://t.me/{BOT_USERNAME}?start=g{gw['id']}"
    if gw["status"] == "finished":
        text += f"\n\n🏁 <b>Конкурс завершён.</b> Участников: {count}"
        kb = None
    elif gw["button_pos"] == "text":
        text += f"\n\n👉 <a href=\"{link}\">Участвовать</a>\n👥 Участников: {count}"
        kb = None
    else:
        kb = ikb([[(f"🎉 Участвовать ({count})", link)]])
    return text, kb

async def refresh_post(gw: dict):
    if not gw.get("message_id"): return
    text, kb = build_post(gw, count_participants(gw["id"]))
    try:
        if gw["photo"]:
            await bot.edit_message_caption(chat_id=gw["chat_id"], message_id=gw["message_id"], caption=text, reply_markup=kb)
        else:
            await bot.edit_message_text(text, chat_id=gw["chat_id"], message_id=gw["message_id"],
                                        reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass

async def publish(gid: int):
    gw = get_gw(gid)
    if not gw or gw["status"] != "scheduled": return
    text, kb = build_post(gw, 0)
    
    # Пытаемся опубликовать с учетом лимитов Telegram (Flood Control)
    for attempt in range(3):
        try:
            if gw["photo"]:
                sent = await bot.send_photo(gw["chat_id"], gw["photo"], caption=text, reply_markup=kb)
            else:
                sent = await bot.send_message(gw["chat_id"], text, reply_markup=kb, disable_web_page_preview=True)
            
            # Если дошли сюда - успех
            db.execute("UPDATE giveaways SET status='active', message_id=? WHERE id=?", (sent.message_id, gid))
            db.commit()
            try: await bot.send_message(gw["owner_id"], f"✅ Конкурс #{gid} опубликован.")
            except Exception: pass
            return
            
        except TelegramRetryAfter as e:
            # Ошибка Too Many Requests - ждем и повторяем
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            # Другая критическая ошибка
            try: await bot.send_message(gw["owner_id"], f"❌ Не удалось опубликовать конкурс #{gid}: {esc(e)}")
            except Exception: pass
            return

async def finish(gid: int):
    gw = get_gw(gid)
    if not gw or gw["status"] != "active": return
    users = [r[0] for r in db.execute("SELECT user_id FROM participants WHERE giveaway_id=?", (gid,))]
    valid = [u for u in users if not await missing_subs(u, gw["channels"])]
    winners = random.sample(valid, min(gw["winners"], len(valid))) if valid else []
    db.execute("UPDATE giveaways SET status='finished', winners_ids=? WHERE id=?",
               (",".join(map(str, winners)), gid)); db.commit()
    gw["status"] = "finished"
    await refresh_post(gw)
    if winners:
        res = f"🏁 <b>Итоги конкурса</b>\n👥 Участников: {len(users)}\n\n🏆 Победители:\n" + \
              "\n".join(f"{i}. {mention(u)}" for i, u in enumerate(winners, 1))
    else:
        res = "🏁 <b>Конкурс завершён.</b> Подходящих участников не нашлось 😔"
    try:
        await bot.send_message(gw["chat_id"], res, reply_to_message_id=gw["message_id"])
    except Exception:
        try: await bot.send_message(gw["chat_id"], res)
        except Exception: pass
    for u in winners:
        try: await bot.send_message(u, f"🎉 Поздравляем! Ты победил в конкурсе #{gid}!\nСвяжись с организатором: {mention(gw['owner_id'])}")
        except Exception: pass
    try: await bot.send_message(gw["owner_id"], res.replace("Итоги конкурса", f"Итоги конкурса #{gid}"))
    except Exception: pass

# ============================ MIDDLEWARE (учёт юзеров) ============================
@dp.message.outer_middleware()
async def track_msg(handler, event: Message, data):
    if event.from_user and event.chat.type == "private": upsert_user(event.from_user)
    return await handler(event, data)

@dp.callback_query.outer_middleware()
async def track_cb(handler, event: CallbackQuery, data):
    if event.from_user: upsert_user(event.from_user)
    return await handler(event, data)

# ============================ СТАРТ / УЧАСТИЕ ============================
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("g") and arg[1:].isdigit():
        return await try_join(m.from_user.id, int(arg[1:]), m)
    await m.answer("👋 Привет! Я бот для проведения конкурсов и розыгрышей.\n\n"
                   "Создавай конкурсы, добавляй обязательную подписку, капчу — и бот сам выберет победителей.",
                   reply_markup=main_kb())

@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_kb())

async def try_join(user_id: int, gid: int, m: Message):
    gw = get_gw(gid)
    if not gw or gw["status"] != "active":
        return await m.answer("❌ Конкурс не найден или уже завершён.", reply_markup=main_kb())
    if db.execute("SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=?", (gid, user_id)).fetchone():
        return await m.answer(f"✅ Ты уже участвуешь!\n👥 Участников: {count_participants(gid)}", reply_markup=main_kb())
    missing = await missing_subs(user_id, gw["channels"])
    if missing:
        rows = []
        for c in missing:
            ch = get_channel(c)
            if ch and ch["link"]: rows.append([(f"📣 {ch['title']}", ch["link"])])
        rows.append([("🔄 Я подписался", f"check:{gid}")])
        return await m.answer("⚠️ Для участия подпишись на каналы и нажми «Я подписался»:", reply_markup=ikb(rows))
    if gw["captcha"]:
        a, b = random.randint(1, 10), random.randint(1, 10)
        pending_captcha[user_id] = (gid, a + b)
        opts = {a + b}
        while len(opts) < 4: opts.add(random.randint(2, 20))
        opts = list(opts); random.shuffle(opts)
        return await m.answer(f"🤖 Докажи, что ты не бот.\nСколько будет <b>{a} + {b}</b>?",
                              reply_markup=ikb([[(str(o), f"cap:{gid}:{o}") for o in opts]]))
    await finish_join(user_id, gw, m)

async def finish_join(user_id: int, gw: dict, m: Message):
    db.execute("INSERT OR IGNORE INTO participants VALUES(?,?,?)", (gw["id"], user_id, int(time.time()))); db.commit()
    cnt = count_participants(gw["id"])
    await refresh_post(gw)
    await m.answer(f"🎉 Ты участвуешь в конкурсе!\n👥 Участников: {cnt}", reply_markup=main_kb())
    if gw["end_type"] == "count" and cnt >= gw["end_value"]:
        await finish(gw["id"])

@dp.callback_query(F.data.startswith("check:"))
async def recheck(c: CallbackQuery):
    await c.answer()
    try: await c.message.delete()
    except Exception: pass
    await try_join(c.from_user.id, int(c.data.split(":")[1]), c.message)

@dp.callback_query(F.data.startswith("cap:"))
async def captcha_answer(c: CallbackQuery):
    _, gid, val = c.data.split(":")
    gid = int(gid)
    p = pending_captcha.get(c.from_user.id)
    if not p or p[0] != gid:
        return await c.answer("Капча устарела, нажми «Участвовать» снова.", show_alert=True)
    try: await c.message.delete()
    except Exception: pass
    if int(val) != p[1]:
        await c.answer("❌ Неверно, попробуй ещё раз.", show_alert=True)
        return await try_join(c.from_user.id, gid, c.message)
    pending_captcha.pop(c.from_user.id, None)
    gw = get_gw(gid)
    if not gw or gw["status"] != "active":
        return await c.message.answer("❌ Конкурс уже завершён.")
    if await missing_subs(c.from_user.id, gw["channels"]):
        return await try_join(c.from_user.id, gid, c.message)
    await c.answer("✅ Верно!")
    await finish_join(c.from_user.id, gw, c.message)

# ============================ МОИ КАНАЛЫ ============================
class AddChan(StatesGroup):
    wait = State()

@dp.my_chat_member()
async def on_my_chat_member(e: ChatMemberUpdated):
    chat = e.chat
    if chat.type not in ("channel", "supergroup", "group"): return
    st = e.new_chat_member.status
    if st in ("administrator", "creator"):
        await save_channel(chat, e.from_user.id)
        try: await bot.send_message(e.from_user.id, f"✅ Канал <b>{esc(chat.title)}</b> добавлен в «Мои каналы/чаты».")
        except Exception: pass
    else:
        db.execute("DELETE FROM channels WHERE chat_id=?", (chat.id,)); db.commit()

def channels_text_kb(uid: int):
    chs = user_channels(uid)
    text = "📁 <b>Мои каналы/чаты</b>\n\nЧтобы добавить канал — сделай бота администратором канала, он появится здесь автоматически.\n"
    rows = [[(f"❌ {c['title']}", f"chdel:{c['chat_id']}")] for c in chs]
    rows.append([("➕ Добавить по @username / пересылкой", "chadd")])
    if not chs: text += "\nПока пусто."
    return text, ikb(rows)

@dp.message(F.text == "Мои каналы/чаты 📁")
async def my_channels(m: Message, state: FSMContext):
    await state.clear()
    text, kb = channels_text_kb(m.from_user.id)
    await m.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "chadd")
async def ch_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddChan.wait)
    await c.message.answer("Отправь @username канала, ссылку или перешли любой пост из канала.\nБот уже должен быть админом там. /cancel — отмена")
    await c.answer()

@dp.message(AddChan.wait)
async def ch_add_msg(m: Message, state: FSMContext):
    chat = None
    if m.forward_origin and getattr(m.forward_origin, "chat", None):
        chat = m.forward_origin.chat
    elif m.text:
        chat = await resolve_chat(m.text)
    if not chat: return await m.answer("Не удалось найти канал. Попробуй ещё раз или /cancel")
    if not await bot_is_admin(chat.id):
        return await m.answer("⚠️ Бот не админ в этом канале. Добавь бота в администраторы и отправь снова.")
    await save_channel(chat, m.from_user.id)
    await state.clear()
    text, kb = channels_text_kb(m.from_user.id)
    await m.answer(f"✅ Канал <b>{esc(chat.title)}</b> добавлен.")
    await m.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("chdel:"))
async def ch_del(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    db.execute("DELETE FROM channels WHERE chat_id=? AND owner_id=?", (cid, c.from_user.id)); db.commit()
    text, kb = channels_text_kb(c.from_user.id)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer("Удалено")

@dp.message(F.text == "Служба поддержки 💬")
async def support(m: Message):
    await m.answer(f"💬 По всем вопросам: {SUPPORT}")

# ============================ СОЗДАНИЕ КОНКУРСА ============================
class Create(StatesGroup):
    text = State(); channels = State(); winners = State(); publish_chat = State()
    publish_when = State(); publish_time = State(); end_type = State(); end_value = State(); preview = State()

@dp.message(F.text == "Создать конкурс 🎲")
async def create_start(m: Message, state: FSMContext):
    await state.clear()
    if not user_channels(m.from_user.id):
        return await m.answer("⚠️ Сначала добавь канал: сделай бота администратором канала, где будет конкурс.\n"
                              "Потом открой «Мои каналы/чаты 📁».")
    await state.set_state(Create.text)
    await m.answer("📝 Отправь текст конкурса (можно с фото). Форматирование сохранится.\n/cancel — отмена")

@dp.message(Create.text, F.text | F.photo)
async def create_text(m: Message, state: FSMContext):
    photo = m.photo[-1].file_id if m.photo else None
    text = m.html_text or ""
    if not text.strip(): return await m.answer("Нужен текст.")
    await state.update_data(text=text, photo=photo, req=[])
    await state.set_state(Create.channels)
    await m.answer("📣 <b>Обязательная подписка</b>\nВыбери каналы, на которые участник должен быть подписан, "
                   "или отправь @username другого канала (бот должен быть там админом).",
                   reply_markup=req_kb(m.from_user.id, []))

def req_kb(uid: int, selected: list):
    rows = [[(("✅ " if c["chat_id"] in selected else "☐ ") + c["title"], f"req:{c['chat_id']}")] for c in user_channels(uid)]
    rows.append([("Достаточно каналов, идём дальше ➡️", "req_done")])
    return ikb(rows)

@dp.callback_query(Create.channels, F.data.startswith("req:"))
async def create_req_toggle(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    d = await state.get_data(); req = d["req"]
    if cid in req: req.remove(cid)
    else:
        if not await bot_is_admin(cid):
            return await c.answer("Бот не админ в этом канале!", show_alert=True)
        req.append(cid)
    await state.update_data(req=req)
    await c.message.edit_reply_markup(reply_markup=req_kb(c.from_user.id, req))
    await c.answer()

@dp.message(Create.channels, F.text)
async def create_req_text(m: Message, state: FSMContext):
    chat = await resolve_chat(m.text)
    if not chat: return await m.answer("Канал не найден. Отправь @username или нажми кнопку «Достаточно каналов».")
    if not await bot_is_admin(chat.id):
        return await m.answer("⚠️ Бот не админ в этом канале — проверка подписки работать не будет. Добавь бота в админы.")
    await save_channel(chat, m.from_user.id)
    d = await state.get_data(); req = d["req"]
    if chat.id not in req: req.append(chat.id)
    await state.update_data(req=req)
    await m.answer(f"✅ Канал <b>{esc(chat.title)}</b> добавлен. Не забирайте у бота права администратора, иначе проверка подписки работать не будет!",
                   reply_markup=req_kb(m.from_user.id, req))

@dp.callback_query(Create.channels, F.data == "req_done")
async def create_req_done(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await c.message.edit_text(f"✅ Сохранено. Каналов для подписки: {len(d['req'])}")
    await state.set_state(Create.winners)
    await c.message.answer("🧮 Сколько победителей выбрать боту?")
    await c.answer()

@dp.message(Create.winners)
async def create_winners(m: Message, state: FSMContext):
    if not (m.text or "").isdigit() or int(m.text) < 1: return await m.answer("Введи число от 1.")
    await state.update_data(winners=int(m.text))
    await state.set_state(Create.publish_chat)
    rows = [[(c["title"], f"pub:{c['chat_id']}")] for c in user_channels(m.from_user.id)]
    await m.answer(f"✅ Количество победителей сохранено: {m.text}")
    await m.answer("🗓 В каком канале публикуем конкурс?", reply_markup=ikb(rows))

@dp.callback_query(Create.publish_chat, F.data.startswith("pub:"))
async def create_pub(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    if not await bot_is_admin(cid): return await c.answer("Бот не админ в этом канале!", show_alert=True)
    await state.update_data(chat_id=cid)
    await state.set_state(Create.publish_when)
    await c.message.edit_text("✅ Канал выбран")
    await c.message.answer("⏰ Когда нужно опубликовать конкурс?",
                           reply_markup=ikb([[("Прямо сейчас", "when:now")], [("Запланировать публикацию", "when:later")]]))
    await c.answer()

@dp.callback_query(Create.publish_when, F.data.startswith("when:"))
async def create_when(c: CallbackQuery, state: FSMContext):
    await c.answer()
    if c.data == "when:now":
        await state.update_data(publish_at=None)
        await c.message.edit_text("✅ Публикация: прямо сейчас")
        return await ask_end_type(c.message, state)
    await state.set_state(Create.publish_time)
    await c.message.edit_text("🕒 Введи дату и время публикации в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (МСК)\nНапример: 25.12.2025 18:00")

@dp.message(Create.publish_time)
async def create_pub_time(m: Message, state: FSMContext):
    ts = parse_dt(m.text or "")
    if not ts or ts < time.time(): return await m.answer("Неверный формат или дата в прошлом. Пример: 25.12.2025 18:00")
    await state.update_data(publish_at=ts)
    await m.answer(f"✅ Публикация запланирована на {fmt_dt(ts)}")
    await ask_end_type(m, state)

async def ask_end_type(m: Message, state: FSMContext):
    await state.set_state(Create.end_type)
    await m.answer("🗓 Как завершить конкурс?", reply_markup=ikb([[("По времени", "end:time")], [("По кол-ву участников", "end:count")]]))

@dp.callback_query(Create.end_type, F.data.startswith("end:"))
async def create_end_type(c: CallbackQuery, state: FSMContext):
    et = c.data.split(":")[1]
    await state.update_data(end_type=et)
    await state.set_state(Create.end_value)
    if et == "time":
        await c.message.edit_text("🕒 Введи дату и время подведения итогов: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (МСК)")
    else:
        await c.message.edit_text("🏁 Укажи количество участников для проведения конкурса:\n\n"
                                  "<i>❗ Участник — тот, кто нажал кнопку в конкурсе, а не подписчик канала.</i>")
    await c.answer()

@dp.message(Create.end_value)
async def create_end_value(m: Message, state: FSMContext):
    d = await state.get_data()
    if d["end_type"] == "time":
        ts = parse_dt(m.text or "")
        if not ts or ts < time.time() or (d.get("publish_at") and ts <= d["publish_at"]):
            return await m.answer("Неверный формат или дата раньше публикации. Пример: 31.12.2025 20:00")
        await state.update_data(end_value=ts)
        await m.answer(f"✅ Итоги будут подведены {fmt_dt(ts)}")
    else:
        if not (m.text or "").isdigit() or int(m.text) < 1: return await m.answer("Введи число.")
        await state.update_data(end_value=int(m.text))
        await m.answer(f"✅ Количество участников для подведения результатов сохранено: {m.text}")
    await state.update_data(captcha=0, button_pos="under")
    await state.set_state(Create.preview)
    await show_preview(m, state)

def preview_kb(d: dict):
    return ikb([
        [(f"📍 Расположение кнопки: {'под постом' if d['button_pos']=='under' else 'в тексте'}", "pv:pos")],
        [(("✅" if d["captcha"] else "❌") + " Использовать капчу", "pv:cap")],
        [("Сохранить конкурс", "pv:save")],
        [("Отмена", "pv:cancel")],
    ])

async def show_preview(m: Message, state: FSMContext):
    d = await state.get_data()
    fake = dict(id=0, text=d["text"], photo=d["photo"], channels=d["req"], winners=d["winners"],
                end_type=d["end_type"], end_value=d["end_value"], button_pos=d["button_pos"], status="active")
    text, kb = build_post(fake, 0)
    if d["photo"]: await m.answer_photo(d["photo"], caption=text, reply_markup=kb)
    else: await m.answer(text, reply_markup=kb, disable_web_page_preview=True)
    end = f"⏰ Итоги: {fmt_dt(d['end_value'])}" if d["end_type"] == "time" else f"🏁 Конкурс завершится при {d['end_value']} участниках"
    pub = "сразу" if not d.get("publish_at") else fmt_dt(d["publish_at"])
    await m.answer(f"Внимательно перепроверьте конкурс\n\n{end}\n🏆 Победителей: {d['winners']}\n"
                   f"📣 Обязательная подписка: {len(d['req'])} кан.\n🗓 Публикация: {pub}", reply_markup=preview_kb(d))

@dp.callback_query(Create.preview, F.data.startswith("pv:"))
async def create_preview_cb(c: CallbackQuery, state: FSMContext):
    act = c.data.split(":")[1]
    d = await state.get_data()
    if act == "pos":
        d["button_pos"] = "text" if d["button_pos"] == "under" else "under"
        await state.update_data(button_pos=d["button_pos"])
        await c.message.edit_reply_markup(reply_markup=preview_kb(d)); return await c.answer()
    if act == "cap":
        d["captcha"] = 0 if d["captcha"] else 1
        await state.update_data(captcha=d["captcha"])
        await c.message.edit_reply_markup(reply_markup=preview_kb(d)); return await c.answer()
    if act == "cancel":
        await state.clear()
        await c.message.edit_text("❌ Создание конкурса отменено."); return await c.answer()
    # save
    cur = db.execute("""INSERT INTO giveaways(owner_id,text,photo,channels,winners,chat_id,publish_at,end_type,end_value,
                        captcha,button_pos,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'scheduled',?)""",
                     (c.from_user.id, d["text"], d["photo"], ",".join(map(str, d["req"])), d["winners"], d["chat_id"],
                      d.get("publish_at"), d["end_type"], d["end_value"], d["captcha"], d["button_pos"], int(time.time())))
    db.commit(); gid = cur.lastrowid
    await state.clear()
    await c.message.edit_text(f"✅ Конкурс #{gid} сохранён.")
    await c.answer()
    if not d.get("publish_at"):
        await publish(gid)

# ============================ МОИ КОНКУРСЫ ============================
def my_list_kb(uid: int):
    rows = []
    for r in db.execute("SELECT id,text,status FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT 30", (uid,)):
        t = html.unescape(r["text"]).replace("\n", " ")[:25]
        rows.append([(f"#{r['id']} {STATUS[r['status']][:2]} {t}", f"gw:{r['id']}")])
    return ikb(rows) if rows else None

@dp.message(F.text == "Мои конкурсы 🗂")
async def my_giveaways(m: Message, state: FSMContext):
    await state.clear()
    kb = my_list_kb(m.from_user.id)
    await m.answer("🗂 <b>Мои конкурсы</b>" if kb else "У тебя пока нет конкурсов.", reply_markup=kb)

@dp.callback_query(F.data == "gwlist")
async def gw_list_cb(c: CallbackQuery):
    kb = my_list_kb(c.from_user.id)
    await c.message.edit_text("🗂 <b>Мои конкурсы</b>" if kb else "У тебя пока нет конкурсов.", reply_markup=kb)
    await c.answer()

def gw_card(gw: dict):
    cnt = count_participants(gw["id"])
    end = f"⏰ Итоги: {fmt_dt(gw['end_value'])}" if gw["end_type"] == "time" else f"🏁 При {gw['end_value']} участниках"
    chans = ", ".join(ch_html(c) for c in gw["channels"]) or "нет"
    text = (f"🎁 <b>Конкурс #{gw['id']}</b> — {STATUS[gw['status']]}\n\n👥 Участников: <b>{cnt}</b>\n"
            f"🏆 Победителей: {gw['winners']}\n{end}\n📣 Подписка: {chans}\n"
            f"🤖 Капча: {'вкл' if gw['captcha'] else 'выкл'}\n📍 Канал: {ch_html(gw['chat_id'])}")
    if gw["status"] == "scheduled": text += f"\n🗓 Публикация: {fmt_dt(gw['publish_at'])}"
    if gw["status"] == "finished" and gw["winners_ids"]:
        text += "\n\n🏆 Победители: " + ", ".join(mention(int(u)) for u in gw["winners_ids"].split(","))
    rows = []
    if gw["status"] != "finished":
        rows.append([(f"🤖 Капча: {'выключить' if gw['captcha'] else 'включить'}", f"gwcap:{gw['id']}")])
    if gw["status"] == "active":
        rows.append([("🏁 Завершить и выбрать победителей", f"gwend:{gw['id']}")])
    rows.append([("🗑 Удалить", f"gwdel:{gw['id']}"), ("⬅️ Назад", "gwlist")])
    return text, ikb(rows)

@dp.callback_query(F.data.startswith("gw:"))
async def gw_open(c: CallbackQuery):
    gw = get_gw(int(c.data.split(":")[1]))
    if not gw or gw["owner_id"] != c.from_user.id: return await c.answer("Не найдено", show_alert=True)
    text, kb = gw_card(gw)
    await c.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data.startswith("gwcap:"))
async def gw_cap(c: CallbackQuery):
    gw = get_gw(int(c.data.split(":")[1]))
    if not gw or gw["owner_id"] != c.from_user.id: return await c.answer("Не найдено", show_alert=True)
    db.execute("UPDATE giveaways SET captcha=? WHERE id=?", (0 if gw["captcha"] else 1, gw["id"])); db.commit()
    text, kb = gw_card(get_gw(gw["id"]))
    await c.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    await c.answer("Капча " + ("выключена" if gw["captcha"] else "включена"))

@dp.callback_query(F.data.startswith("gwend:"))
async def gw_end(c: CallbackQuery):
    gw = get_gw(int(c.data.split(":")[1]))
    if not gw or gw["owner_id"] != c.from_user.id: return await c.answer("Не найдено", show_alert=True)
    await c.answer("Подводим итоги...")
    await finish(gw["id"])
    text, kb = gw_card(get_gw(gw["id"]))
    await c.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)

@dp.callback_query(F.data.startswith("gwdel:"))
async def gw_del(c: CallbackQuery):
    gid = int(c.data.split(":")[1])
    gw = get_gw(gid)
    if not gw or gw["owner_id"] != c.from_user.id: return await c.answer("Не найдено", show_alert=True)
    db.execute("DELETE FROM giveaways WHERE id=?", (gid,)); db.execute("DELETE FROM participants WHERE giveaway_id=?", (gid,)); db.commit()
    await c.answer("Удалено")
    await gw_list_cb(c)

# ============================ АДМИН-ПАНЕЛЬ ============================
class Cast(StatesGroup):
    msg = State(); confirm = State()

def admin_kb():
    return ikb([[("📊 Статистика", "adm:stats")], [("📢 Рассылка", "adm:cast")]])

@dp.message(Command("admin", "admins"), F.from_user.id.in_(ADMINS))
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())

def stats_text():
    now = int(time.time()); day = now - 86400; week = now - 7 * 86400
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    return (f"📊 <b>Статистика</b>\n\n"
            f"👤 Пользователей: <b>{q('SELECT COUNT(*) FROM users')}</b>\n"
            f"  ├ за 24ч: +{q('SELECT COUNT(*) FROM users WHERE joined_at>?', day)}\n"
            f"  ├ за 7 дней: +{q('SELECT COUNT(*) FROM users WHERE joined_at>?', week)}\n"
            f"  └ заблокировали бота: {q('SELECT COUNT(*) FROM users WHERE blocked=1')}\n\n"
            f"🎁 Конкурсов: <b>{q('SELECT COUNT(*) FROM giveaways')}</b>\n"
            f""" ├ Идут: {q("SELECT COUNT(*) FROM giveaways WHERE status='active'")}\n"""
            f""" ├ запланированы: {q("SELECT COUNT(*) FROM giveaways WHERE status='scheduled'")}\n"""
            f"""  └ завершены: {q("SELECT COUNT(*) FROM giveaways WHERE status='finished'")}\n\n"""
            f"🙋 Участий всего: <b>{q('SELECT COUNT(*) FROM participants')}</b>\n"
            f"📁 Подключено каналов: {q('SELECT COUNT(*) FROM channels')}")

@dp.callback_query(F.data == "adm:stats", F.from_user.id.in_(ADMINS))
async def adm_stats(c: CallbackQuery):
    await c.message.edit_text(stats_text(), reply_markup=ikb([[("🔄 Обновить", "adm:stats")], [("⬅️ Назад", "adm:back")]]))
    await c.answer()

@dp.callback_query(F.data == "adm:back", F.from_user.id.in_(ADMINS))
async def adm_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb()); await c.answer()

@dp.callback_query(F.data == "adm:cast", F.from_user.id.in_(ADMINS))
async def adm_cast(c: CallbackQuery, state: FSMContext):
    await state.set_state(Cast.msg)
    await c.message.edit_text("📢 Отправь сообщение для рассылки (текст, фото, видео, с кнопками — что угодно).\n/cancel — отмена")
    await c.answer()

@dp.message(Cast.msg, F.from_user.id.in_(ADMINS))
async def adm_cast_msg(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, msg_id=m.message_id)
    await state.set_state(Cast.confirm)
    total = db.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
    await m.answer(f"Так будет выглядеть сообщение 👆\n\nОтправить <b>{total}</b> пользователям?",
                   reply_markup=ikb([[("✅ Отправить", "cast:go"), ("❌ Отмена", "cast:no")]]))

@dp.callback_query(Cast.confirm, F.data.startswith("cast:"))
async def adm_cast_confirm(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); await state.clear()
    if c.data == "cast:no":
        await c.message.edit_text("❌ Рассылка отменена."); return await c.answer()
    await c.message.edit_text("🚀 Рассылка запущена...")
    await c.answer()
    asyncio.create_task(run_broadcast(c.from_user.id, d["chat_id"], d["msg_id"]))

async def run_broadcast(admin_id: int, from_chat: int, msg_id: int):
    users = [r[0] for r in db.execute("SELECT user_id FROM users WHERE blocked=0")]
    total = len(users); ok = blocked = err = 0
    status = await bot.send_message(admin_id, f"📢 Рассылка: 0/{total}")
    for i, uid in enumerate(users, 1):
        for attempt in range(2):
            try:
                await bot.copy_message(uid, from_chat, msg_id); ok += 1; break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (TelegramForbiddenError, TelegramBadRequest):
                blocked += 1; db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,)); break
            except Exception as e:
                logging.warning(f"broadcast {uid}: {e}"); err += 1; break
        if i % 50 == 0:
            db.commit()
            try: await status.edit_text(f"📢 Рассылка: {i}/{total}\n✅ {ok}  🚫 {blocked}  ⚠️ {err}")
            except Exception: pass
        await asyncio.sleep(0.05)   # ~20 сообщений/сек — лимит Telegram
    db.commit()
    try: await status.edit_text(f"✅ <b>Рассылка завершена</b>\n\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибок: {err}\nВсего: {total}")
    except Exception: pass

# ============================ ПЛАНИРОВЩИК ============================
async def scheduler():
    while True:
        try:
            now = int(time.time())
            for r in db.execute("SELECT id FROM giveaways WHERE status='scheduled' AND publish_at IS NOT NULL AND publish_at<=?", (now,)).fetchall():
                await publish(r["id"])
            for r in db.execute("SELECT id FROM giveaways WHERE status='active' AND end_type='time' AND end_value<=?", (now,)).fetchall():
                await finish(r["id"])
        except Exception as e:
            logging.exception(e)
        await asyncio.sleep(20)

# ============================ ЗАПУСК ============================
@dp.message(F.chat.type == "private")
async def fallback(m: Message, state: FSMContext):
    if await state.get_state(): return
    await m.answer("Выбери действие в меню 👇", reply_markup=main_kb())

async def main():
    global BOT_ID, BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    me = await bot.get_me()
    BOT_ID, BOT_USERNAME = me.id, me.username
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])

if __name__ == "__main__":
    asyncio.run(main())