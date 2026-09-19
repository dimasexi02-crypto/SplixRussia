"""
🎁 Gift Bot — бот-розыгрыш подарков за активность в чате
Библиотека: aiogram 3.x  (pip install aiogram)

Как запустить:
1. Создай бота в @BotFather, возьми токен.
2. Добавь бота в чат и сделай админом (чтобы он видел все сообщения).
   В @BotFather → Bot Settings → Group Privacy → Turn OFF (обязательно!).
3. Заполни настройки ниже (BOT_TOKEN, CHAT_ID, ADMIN_IDS, SPONSOR...).
4. Запусти: python gift_bot.py
5. Напиши боту в личку /gifts — он покажет все подарки с ID и ценами,
   вставь нужный ID в GIFT_ID.
6. Пополни звёзды боту командой /donate (бот выставит счёт, ты его оплатишь).
"""

import asyncio
import json
import logging
import os
import random
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, FSInputFile

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = "8639880014:AAGg6l40uamBex0Fuaz0StJ-7jtIn4McDFM"
CHAT_ID = -1004313069796       # ID твоего чата (узнать: добавь бота и напиши /id)
ADMIN_IDS = {8018644395}           # твой user_id (узнать: /id в личке боту)

GIFT_ID = "5170233102089322756"   # ID подарка (Мишка 🧸 = 15⭐). Проверь через /gifts!
GIFT_NAME = "Мишка 🧸"

DRAW_INTERVAL_MIN = 60            # раз во сколько минут розыгрыш
ACTIVE_WINDOW_MIN = 60            # учитываем сообщения за последние N минут
MIN_MESSAGES = 3                  # минимум сообщений, чтобы попасть в розыгрыш
MSG_COOLDOWN_SEC = 5              # антиспам: одно сообщение в N секунд засчитывается

SPONSOR = "@splixofficial"             # от кого подарок
PHOTO = "win.jpg"                 # картинка для поздравления (файл рядом) или None
AD_TEXT = (
    "⭐ Купить звезды дешево\n"
    "с любым способом оплаты: @позже\n\n"
    "‼️ пишите сообщения\n"
    "в чате, и получайте\n"
    "возможность так же\n"
    "залутать подарки"
)
DATA_FILE = "activity.json"
# ================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("giftbot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# activity: {user_id: {"name": str, "username": str, "msgs": [timestamps], "last": ts}}
activity: dict[int, dict] = {}


# ---------- хранилище ----------
def save():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(activity, f, ensure_ascii=False)


def load():
    global activity
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            activity = {int(k): v for k, v in json.load(f).items()}


def cleanup():
    """Удаляем старые сообщения из окна активности."""
    border = time.time() - ACTIVE_WINDOW_MIN * 60
    for uid in list(activity):
        activity[uid]["msgs"] = [t for t in activity[uid]["msgs"] if t > border]
        if not activity[uid]["msgs"]:
            del activity[uid]


def mention(uid: int, data: dict) -> str:
    if data.get("username"):
        return f"@{data['username']}"
    return f'<a href="tg://user?id={uid}">{data["name"]}</a>'


# ---------- учёт активности ----------
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text | F.sticker | F.photo)
async def count_activity(m: Message):
    if m.chat.id != CHAT_ID or m.from_user.is_bot:
        return
    if m.text and m.text.startswith("/"):
        return
    if m.text and len(m.text.strip()) < 2:      # игнорим точки и односимвольный флуд
        return

    uid = m.from_user.id
    now = time.time()
    user = activity.setdefault(uid, {"name": "", "username": "", "msgs": [], "last": 0})
    if now - user["last"] < MSG_COOLDOWN_SEC:  # антиспам
        return
    user["name"] = m.from_user.full_name
    user["username"] = m.from_user.username or ""
    user["msgs"].append(now)
    user["last"] = now


# ---------- розыгрыш ----------
async def do_draw(forced_by: Message | None = None):
    cleanup()
    candidates = {uid: d for uid, d in activity.items() if len(d["msgs"]) >= MIN_MESSAGES}
    if not candidates:
        log.info("Розыгрыш пропущен — нет активных участников")
        if forced_by:
            await forced_by.reply("😴 Некого награждать — в чате тихо.")
        return

    # шанс победить пропорционален количеству сообщений
    uids = list(candidates)
    weights = [len(candidates[u]["msgs"]) for u in uids]
    winner_id = random.choices(uids, weights=weights, k=1)[0]
    winner = candidates[winner_id]

    # отправляем подарок
    try:
        await bot.send_gift(
            user_id=winner_id,
            gift_id=GIFT_ID,
            text=f"🎉 Подарок за активность в чате от {SPONSOR}!",
        )
        gift_status = "🎁 Подарок отправлен."
    except Exception as e:
        log.error("Не удалось отправить подарок: %s", e)
        gift_status = f"⚠️ Не смог отправить подарок автоматически ({e.__class__.__name__}). {SPONSOR}, выдай вручную!"

    text = (
        f"🥳 Поздравляю!\n"
        f"🎁 Ты выиграл <b>{GIFT_NAME}</b> от {SPONSOR}\n"
        f"{gift_status}\n\n"
        f"👤 Победитель: {mention(winner_id, winner)}\n"
        f"💬 Сообщений за час: {len(winner['msgs'])}\n\n"
        f"{AD_TEXT}"
    )

    if PHOTO and os.path.exists(PHOTO):
        await bot.send_photo(CHAT_ID, FSInputFile(PHOTO), caption=text, parse_mode="HTML")
    else:
        await bot.send_message(CHAT_ID, text, parse_mode="HTML")

    # сбрасываем счётчик победителя, чтобы не выигрывал подряд
    activity.pop(winner_id, None)
    save()


async def draw_loop():
    while True:
        await asyncio.sleep(DRAW_INTERVAL_MIN * 60)
        try:
            await do_draw()
        except Exception as e:
            log.exception("Ошибка в розыгрыше: %s", e)


# ---------- команды ----------
def is_admin(m: Message) -> bool:
    return m.from_user.id in ADMIN_IDS


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.reply(f"chat_id: <code>{m.chat.id}</code>\nuser_id: <code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("draw"))
async def cmd_draw(m: Message):
    """Принудительный розыгрыш (только админ)."""
    if not is_admin(m):
        return
    await do_draw(forced_by=m)


@dp.message(Command("top"))
async def cmd_top(m: Message):
    cleanup()
    if not activity:
        await m.reply("Пока никто не активничал 😴")
        return
    rows = sorted(activity.items(), key=lambda x: len(x[1]["msgs"]), reverse=True)[:10]
    text = "🏆 <b>Топ активных за час:</b>\n\n"
    for i, (uid, d) in enumerate(rows, 1):
        text += f"{i}. {mention(uid, d)} — {len(d['msgs'])} сообщ.\n"
    text += f"\n🎲 Следующий розыгрыш каждые {DRAW_INTERVAL_MIN} мин. Минимум {MIN_MESSAGES} сообщ."
    await m.reply(text, parse_mode="HTML")


@dp.message(Command("gifts"))
async def cmd_gifts(m: Message):
    """Список доступных подарков с ID (только админ, в личке)."""
    if not is_admin(m):
        return
    gifts = await bot.get_available_gifts()
    text = "🎁 <b>Доступные подарки:</b>\n\n"
    for g in gifts.gifts:
        text += f"{g.sticker.emoji} {g.star_count}⭐ — <code>{g.id}</code>\n"
    await m.reply(text, parse_mode="HTML")


@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    if not is_admin(m):
        return
    bal = await bot.get_my_star_balance()
    await m.reply(f"⭐ Баланс бота: {bal.amount} звёзд")


@dp.message(Command("donate"))
async def cmd_donate(m: Message):
    """Пополнить звёзды боту: /donate 100"""
    parts = m.text.split()
    amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
    await bot.send_invoice(
        chat_id=m.chat.id,
        title="Пополнение баланса подарков",
        description=f"Звёзды пойдут на подарки участникам чата 🎁",
        payload="donate",
        currency="XTR",
        prices=[LabeledPrice(label="Звёзды", amount=amount)],
    )


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def paid(m: Message):
    await m.reply(f"✅ Спасибо! Получено {m.successful_payment.total_amount}⭐")


# ---------- запуск ----------
async def main():
    load()
    asyncio.create_task(draw_loop())
    log.info("Бот запущен. Розыгрыш каждые %s мин.", DRAW_INTERVAL_MIN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
