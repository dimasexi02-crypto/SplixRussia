"""Бот с магазином, ручной проверкой CloudTips, рефералами и розыгрышами.
Python 3.10+; pip install -U aiogram
Укажи BOT_TOKEN и настройки. Бот должен быть администратором чата и каналов.
Обязательные подписки встроены — дополнительных файлов не нужно.
Перед заменой сделай резервную копию bot.sqlite3.
"""
import asyncio
import html
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import urlencode
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

BOT_TOKEN = os.getenv('BOT_TOKEN', '8639880014:AAGg6l40uamBex0Fuaz0StJ-7jtIn4McDFM')
ADMIN_IDS = {8018644395}
CHAT_ID = -1004313069796
CHAT_LINK = 'https://t.me/chat_splix'
CONTACT = 'https://t.me/splixofficial'  # укажи личный контакт продавца
PAY_URL = 'https://pay.cloudtips.ru/p/fa40fb57'
DB_FILE = 'bot.sqlite3'
REF_EVERY, REF_REWARD = 150, 200
PACKAGES = [(50,75),(100,145),(250,350),(500,690),(1000,1350)]
GIFTS = []  # Пример: [{'id':'ID_ИЗ_GIFTS','name':'Мишка 🧸','price':50}]

# ОБЯЗАТЕЛЬНЫЕ ПОДПИСКИ: раскомментируй строки и укажи СВОИ каналы.
# Пустой список отключает проверку. Количество каналов не ограничено пятью.
# Для закрытого канала: числовой id и пригласительная ссылка.
REQUIRED_CHANNELS = [
    {'id': -1004448446792, 'link': 'https://t.me/splixrussiaSTARS'},
]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('bot')
dp = Dispatcher()
bot = None
bot_name = ''

class Form(StatesGroup):
    broadcast = State()
    prize = State()
    description = State()

@contextmanager
def db():
    c = sqlite3.connect(DB_FILE, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()

def rows(sql,args=()):
    with db() as c: return c.execute(sql,args).fetchall()
def one(sql,args=()):
    with db() as c: return c.execute(sql,args).fetchone()
def run(sql,args=()):
    with db() as c: return c.execute(sql,args).rowcount

def init_db():
    with db() as c:
        c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,username TEXT DEFAULT '',ref_by INTEGER,ref_confirmed INTEGER DEFAULT 0,referrals INTEGER DEFAULT 0,rewards INTEGER DEFAULT 0,pending_stars INTEGER DEFAULT 0,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,stars INTEGER NOT NULL,price INTEGER NOT NULL,status TEXT DEFAULT 'new',created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS raffles(id INTEGER PRIMARY KEY AUTOINCREMENT,prize TEXT NOT NULL,description TEXT DEFAULT '',status TEXT DEFAULT 'active',winner_id INTEGER,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS raffle_entries(raffle_id INTEGER NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(raffle_id,user_id));
CREATE TABLE IF NOT EXISTS payout_log(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,amount INTEGER,admin_id INTEGER,created_at INTEGER);
''')
        cols={x['name'] for x in c.execute('PRAGMA table_info(orders)')}
        for name,spec in [('kind',"TEXT NOT NULL DEFAULT 'stars'"),('gift_id',"TEXT NOT NULL DEFAULT ''"),('label',"TEXT NOT NULL DEFAULT ''"),('error',"TEXT NOT NULL DEFAULT ''"),('admin_id','INTEGER')]:
            if name not in cols: c.execute(f'ALTER TABLE orders ADD COLUMN {name} {spec}')
        c.execute("UPDATE orders SET status='uncertain',error='Перезапуск во время выдачи: проверь отправку вручную' WHERE status='processing'")

def esc(s): return html.escape(str(s))
def kb(items): return K(inline_keyboard=[[B(text=t,callback_data=d) for t,d in row] for row in items])
def back(): return kb([[('◀️ Главное меню','home')]])
def home(uid):
    k=kb([[('⭐ Магазин','shop')],[('👥 Рефералы','ref')],[('🎁 Розыгрыши','raffles:0')],[('📊 Мой профиль','profile')]])
    k.inline_keyboard.append([B(text='💬 Перейти в чат',url=CHAT_LINK)])
    if uid in ADMIN_IDS: k.inline_keyboard.append([B(text='⚙️ Администратор',callback_data='admin')])
    return k

def admin_kb():
    return kb([[('📊 Статистика','stats')],[('🛒 Заказы','orders:0')],[('💸 Выплаты','payouts:0')],[('📨 Рассылка','broadcast')],[('🎁 Создать розыгрыш','newraffle')],[('🏆 Провести розыгрыш','raffles:0')],[('Главное меню','home')]])

def register(u,ref=None):
    with db() as c:
        if ref==u.id or not c.execute('SELECT id FROM users WHERE id=?',(ref,)).fetchone(): ref=None
        c.execute('INSERT OR IGNORE INTO users(id,name,username,ref_by,created_at) VALUES(?,?,?,?,?)',(u.id,u.full_name,u.username or '',ref,int(time.time())))
        c.execute('UPDATE users SET name=?,username=? WHERE id=?',(u.full_name,u.username or '',u.id))

def ref_argument(text):
    parts=(text or '').split(maxsplit=1)
    if parts and parts[0].split('@')[0]=='/start' and len(parts)==2 and parts[1].startswith('ref_'):
        try: return int(parts[1][4:])
        except ValueError: pass
    return None

async def notify(uid,text,keyboard=None):
    try:
        await bot.send_message(uid,text,parse_mode='HTML',reply_markup=keyboard)
        return True
    except Exception as e:
        log.warning('Уведомление %s: %s',uid,type(e).__name__)
        return False
async def admins(text,keyboard=None):
    for uid in ADMIN_IDS: await notify(uid,text,keyboard)
async def show(c,text,keyboard=None):
    try: await c.message.edit_text(text,parse_mode='HTML',reply_markup=keyboard)
    except TelegramBadRequest as e:
        if 'message is not modified' not in str(e): await c.message.answer(text,parse_mode='HTML',reply_markup=keyboard)

async def member(chat,uid):
    try:
        m=await bot.get_chat_member(chat,uid)
        return m.status in ('member','administrator','creator') or (m.status=='restricted' and m.is_member)
    except Exception as e:
        log.warning('Проверка подписки %s: %s',chat,type(e).__name__)
        return None

# ================= ВСТРОЕННЫЕ ОБЯЗАТЕЛЬНЫЕ ПОДПИСКИ =================
async def missing_channels(uid):
    missing=[]; failed=False
    for channel in REQUIRED_CHANNELS:
        status=await member(channel['id'],uid)
        if status is not True: missing.append(channel)
        if status is None: failed=True
    return missing,failed

def subscriptions_kb(channels):
    k=K(inline_keyboard=[[B(text=f'📢 Подписаться на канал {i}',url=channel['link'])] for i,channel in enumerate(channels,1)])
    k.inline_keyboard.append([B(text='✅ Проверить подписку',callback_data='subs:check')])
    return k

class SubscriptionGate(BaseMiddleware):
    async def __call__(self,handler,event,data):
        if not isinstance(event,(Message,CallbackQuery)) or not event.from_user or event.from_user.is_bot:
            return await handler(event,data)
        uid=event.from_user.id
        check=isinstance(event,CallbackQuery) and event.data=='subs:check'
        private=(event.chat.type=='private') if isinstance(event,Message) else bool(event.message and event.message.chat.type=='private')
        if isinstance(event,Message) and not private:
            return await handler(event,data)
        # Сохраняем пригласившего ДО экрана подписок.
        if isinstance(event,Message) and private and (event.text or '').split(maxsplit=1)[0:1] and (event.text or '').split()[0].split('@')[0]=='/start':
            register(event.from_user,ref_argument(event.text))
        if uid in ADMIN_IDS or not REQUIRED_CHANNELS:
            missing,failed=[],False
        else:
            missing,failed=await missing_channels(uid)
        if missing:
            text='📢 <b>Обязательная подписка</b>\n\nПодпишись на все каналы ниже, затем нажми «✅ Проверить подписку».'
            if failed: text+='\n\n⚠️ Часть подписок не удалось проверить. Если ты уже подписан, попробуй позже или сообщи администратору.'
            if isinstance(event,CallbackQuery):
                if not private:
                    await event.answer('Для участия нужны подписки. Открой бота в личке через /start — там список каналов.',show_alert=True)
                    return
                await event.answer('Подписки ещё не подтверждены' if check else 'Нужно подписаться на каналы')
                await event.message.answer(text,parse_mode='HTML',reply_markup=subscriptions_kb(missing))
            else:
                await event.answer(text,parse_mode='HTML',reply_markup=subscriptions_kb(missing))
            return
        if check:
            await event.answer('✅ Подписки подтверждены!')
            if private:
                register(event.from_user)
                await confirm_ref(uid)
                await event.message.answer('✅ Доступ открыт! Выбирай раздел 👇',reply_markup=home(uid))
            return
        return await handler(event,data)
# ====================================================================

async def confirm_ref(uid):
    u=one('SELECT * FROM users WHERE id=?',(uid,))
    if not u or not u['ref_by']: return 'Ты не зарегистрирован по приглашению друга.'
    if u['ref_confirmed']: return 'Твоё приглашение уже засчитано другу.'
    status=await member(CHAT_ID,uid)
    if status is None: return 'Не удалось проверить вступление. Попробуй позже.'
    if not status: return 'Вступи в чат и нажми «Проверить моё вступление».'
    with db() as c:
        changed=c.execute('UPDATE users SET ref_confirmed=1 WHERE id=? AND ref_confirmed=0',(uid,)).rowcount
        if not changed: return 'Приглашение уже засчитано.'
        c.execute('UPDATE users SET referrals=referrals+1 WHERE id=?',(u['ref_by'],))
        inv=c.execute('SELECT * FROM users WHERE id=?',(u['ref_by'],)).fetchone()
        earned=inv['referrals']//REF_EVERY
        amount=max(0,earned-inv['rewards'])*REF_REWARD
        c.execute('UPDATE users SET rewards=?,pending_stars=pending_stars+? WHERE id=?',(earned,amount,u['ref_by']))
    if amount:
        await notify(u['ref_by'],f'🏆 Начислено {amount}⭐ к ручной выплате за приглашённых!')
        await admins(f'💸 Реферальная награда {amount}⭐ для ID {u["ref_by"]}.',kb([[('Выплаты','payouts:0')]]))
    return '✅ Твоё приглашение засчитано другу!'

STATUS={'new':'ожидает оплаты','review':'оплата на проверке','approved':'оплата подтверждена, Stars нужно выдать','processing':'отправка подарка','done':'выполнен','rejected':'отклонён','failed':'подарок не отправлен','uncertain':'исход отправки неизвестен'}
def order_text(o):
    u=one('SELECT * FROM users WHERE id=?',(o['user_id'],))
    name=(u['name'] if u else str(o['user_id']))
    t=f'🛒 <b>Заказ #{o["id"]}</b>\nТовар: {esc(o["label"] or str(o["stars"])+"⭐")}\nЦена: {o["price"]} ₽\nСтатус: {esc(STATUS.get(o["status"],o["status"]))}\nПокупатель: <a href="tg://user?id={o["user_id"]}">{esc(name)}</a>\nID: <code>{o["user_id"]}</code>'
    if u and u['username']: t+='\n@'+esc(u['username'])
    if o['error']: t+='\nПримечание: '+esc(o['error'][:300])
    return t

def order_kb(o):
    oid=o['id']; s=o['status']; r=[]
    if s in ('new','review'): r=[[('✅ Подтвердить оплату',f'ask:approve:{oid}')],[('❌ Отказать',f'ask:reject:{oid}')]]
    if s=='approved': r=[[('⭐ Я выдал звёзды',f'ask:complete:{oid}')]]
    if s=='failed': r=[[('🔄 Повторить отправку',f'ask:retry:{oid}')],[('✅ Выдал вручную',f'ask:complete:{oid}')]]
    if s=='uncertain': r=[[('✅ Проверил: подарок выдан',f'ask:complete:{oid}')]]
    return kb(r+[[('◀️ Заказы','orders:0')]])

async def order_view(c,oid):
    o=one('SELECT * FROM orders WHERE id=?',(oid,))
    await show(c,order_text(o) if o else 'Заказ не найден.',order_kb(o) if o else admin_kb())

async def approve(oid,aid,retry=False):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o or o['status'] not in (('failed',) if retry else ('new','review')): return
        status='processing' if o['kind']=='gift' else 'approved'
        c.execute('UPDATE orders SET status=?,admin_id=? WHERE id=?',(status,aid,oid))
    if status=='approved':
        await notify(o['user_id'],f'✅ Оплата заказа #{oid} подтверждена. Ожидайте выдачу {o["stars"]}⭐ администратором.')
        return
    try:
        ok=await bot.send_gift(user_id=o['user_id'],gift_id=o['gift_id'],text=f'Подарок по заказу #{oid}')
        if not ok: raise RuntimeError('Отправка не подтверждена')
    except (TelegramBadRequest,TelegramForbiddenError) as e:
        run("UPDATE orders SET status='failed',error=? WHERE id=?",(str(e)[:500],oid))
        await notify(o['user_id'],f'Оплата #{oid} подтверждена. Отправка подарка не удалась, администратор проверит выдачу.')
    except Exception as e:
        run("UPDATE orders SET status='uncertain',error=? WHERE id=?",(type(e).__name__+': проверь выдачу вручную',oid))
        await notify(o['user_id'],f'Оплата #{oid} подтверждена. Результат отправки подарка уточняется.')
    else:
        run("UPDATE orders SET status='done',error='' WHERE id=?",(oid,))
        await notify(o['user_id'],f'🎁 Подарок по заказу #{oid} отправлен!')
    o=one('SELECT * FROM orders WHERE id=?',(oid,))
    await admins(order_text(o),order_kb(o))

def stats():
    n=one('SELECT COUNT(*) n FROM users')['n']
    r=one('SELECT COALESCE(SUM(referrals),0) r,COALESCE(SUM(pending_stars),0) p FROM users')
    counts=rows('SELECT status,COUNT(*) n FROM orders GROUP BY status')
    active=one("SELECT COUNT(*) n FROM raffles WHERE status='active'")['n']
    return f'📊 Пользователей: {n}\nРефералов: {r["r"]}\nОжидает выплаты: {r["p"]}⭐\nАктивных розыгрышей: {active}\n\nЗаказы:\n'+'\n'.join(f'{esc(STATUS.get(x["status"],x["status"]))}: {x["n"]}' for x in counts)

@dp.message(Command('start','cancel'))
async def start(m:Message,state:FSMContext):
    if m.chat.type!='private':
        await m.answer(f'Открой бота в личке: https://t.me/{bot_name}'); return
    await state.clear()
    register(m.from_user,ref_argument(m.text))
    await m.answer(f'👋 Привет, {esc(m.from_user.first_name)}!\n\n⭐ Магазин, 🎁 розыгрыши и 👥 приглашения друзей.\nЗа каждые {REF_EVERY} подтверждённых приглашений — {REF_REWARD}⭐ к ручной выплате.',parse_mode='HTML',reply_markup=home(m.from_user.id))
    if ref_argument(m.text): await m.answer(await confirm_ref(m.from_user.id),reply_markup=kb([[('✅ Проверить моё вступление','check')]]))

@dp.message(Command('stats','r','done','gifts','balance','id'))
async def commands(m:Message,command:CommandObject,state:FSMContext):
    if m.chat.type!='private' or m.from_user.id not in ADMIN_IDS: return
    await state.clear()
    cmd=command.command
    if cmd=='stats': await m.answer(stats(),parse_mode='HTML',reply_markup=admin_kb())
    elif cmd=='r':
        if command.args:
            await state.update_data(text=command.args)
            await m.answer(command.args)
            await m.answer('Отправить всем?',reply_markup=kb([[('📨 Отправить','sendbroadcast')],[('Отмена','admin')]]))
        else:
            await state.set_state(Form.broadcast)
            await m.answer('Пришли сообщение для рассылки.',reply_markup=kb([[('Отмена','admin')]]))
    elif cmd=='done':
        o=one('SELECT * FROM orders WHERE id=?',(int(command.args),)) if command.args and command.args.isdigit() else None
        await m.answer(order_text(o) if o else 'Выбери заказ в панели.',parse_mode='HTML',reply_markup=order_kb(o) if o else kb([[('Заказы','orders:0')]]))
    elif cmd=='gifts':
        gifts=await bot.get_available_gifts()
        lines=[f'{g.sticker.emoji or "🎁"} {g.star_count}⭐ — {g.id}' for g in gifts.gifts]
        for i in range(0,len(lines),30): await m.answer('\n'.join(lines[i:i+30]))
    elif cmd=='balance': await m.answer(f'Баланс бота: {(await bot.get_my_star_balance()).amount}⭐')
    else: await m.answer(f'ID пользователя: {m.from_user.id}\nID чата: {m.chat.id}')

@dp.callback_query()
async def callbacks(c:CallbackQuery,state:FSMContext):
    d=c.data or ''; uid=c.from_user.id
    if d.startswith('join:'):
        rid=int(d.split(':')[1])
        if await member(CHAT_ID,uid) is not True:
            await c.answer('Для участия нужно состоять в чате.',show_alert=True); return
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            r=con.execute("SELECT id FROM raffles WHERE id=? AND status='active'",(rid,)).fetchone()
            changed=con.execute('INSERT OR IGNORE INTO raffle_entries VALUES(?,?)',(rid,uid)).rowcount if r else None
        await c.answer('Розыгрыш завершён.' if changed is None else ('Ты участвуешь!' if changed else 'Ты уже участвуешь.'),show_alert=True)
        return
    if not c.message or c.message.chat.type!='private':
        await c.answer('Открой бота в личке.',show_alert=True); return
    if d not in ('home','shop','ref','profile','check') and not d.startswith(('buy:','paid:','raffles:')) and uid not in ADMIN_IDS:
        await c.answer('Нет доступа',show_alert=True); return
    await c.answer(); register(c.from_user)
    if d in ('home','admin'):
        await state.clear()
        await show(c,'🎁 Главное меню' if d=='home' else '⚙️ Администратор',home(uid) if d=='home' else admin_kb())
    elif d=='shop':
        r=[[(f'{s}⭐ — {p} ₽',f'buy:stars:{i}')] for i,(s,p) in enumerate(PACKAGES)]
        r += [[(f'{g["name"]} — {g["price"]} ₽',f'buy:gift:{i}')] for i,g in enumerate(GIFTS)]
        await show(c,'⭐ Магазин\nCloudTips проверяется вручную. Stars выдаёт администратор.',kb(r+[[('Главное меню','home')]]))
    elif d.startswith('buy:'):
        _,kind,index=d.split(':'); i=int(index)
        if kind=='stars' and 0<=i<len(PACKAGES):
            stars,price=PACKAGES[i]; label=f'{stars}⭐'; gid=''
        elif kind=='gift' and 0<=i<len(GIFTS):
            g=GIFTS[i]; stars,price,label,gid=0,g['price'],g['name'],g['id']
        else: await show(c,'Товар недоступен.',back()); return
        with db() as con:
            o=con.execute("SELECT * FROM orders WHERE user_id=? AND status='new' AND kind=? AND stars=? AND price=? AND gift_id=? ORDER BY id DESC LIMIT 1",(uid,kind,stars,price,gid)).fetchone()
            oid=o['id'] if o else con.execute('INSERT INTO orders(user_id,stars,price,created_at,kind,label,gift_id) VALUES(?,?,?,?,?,?,?)',(uid,stars,price,int(time.time()),kind,label,gid)).lastrowid
        k=kb([[('✅ Я оплатил',f'paid:{oid}')],[('Главное меню','home')]])
        k.inline_keyboard.insert(0,[B(text='💳 Оплатить',url=PAY_URL)])
        k.inline_keyboard.append([B(text='Связаться с продавцом',url=CONTACT)])
        await show(c,f'🛒 <b>Заказ #{oid}</b>\n{esc(label)} — <b>{price} ₽</b>\n\nНа странице оплаты введи точную сумму {price} ₽. Если есть комментарий, укажи «Заказ #{oid}, ID {uid}». Сохрани чек.\nПосле оплаты нажми «Я оплатил».',k)
    elif d.startswith('paid:'):
        oid=int(d.split(':')[1])
        changed=run("UPDATE orders SET status='review' WHERE id=? AND user_id=? AND status='new'",(oid,uid))
        o=one('SELECT * FROM orders WHERE id=? AND user_id=?',(oid,uid))
        if not o: await show(c,'Заказ не найден.',back()); return
        if changed: await admins('🔔 <b>Покупатель нажал «Я оплатил».</b>\nПоступление ещё НЕ проверено. Сверь сумму и чек в CloudTips.\n\n'+order_text(o),order_kb(o))
        await show(c,('⏳ Ожидайте, оплата проверяется.\n\n' if o['status']=='review' else '')+order_text(o),back())
    elif d in ('ref','profile'):
        u=one('SELECT * FROM users WHERE id=?',(uid,)); link=f'https://t.me/{bot_name}?start=ref_{uid}'
        k=kb([[('✅ Проверить моё вступление','check')],[('Главное меню','home')]])
        k.inline_keyboard.insert(0,[B(text='📤 Поделиться',url='https://t.me/share/url?'+urlencode({'url':link,'text':'Заходи в наш чат 🎁'}))])
        k.inline_keyboard.insert(1,[B(text='Вступить в чат',url=CHAT_LINK)])
        await show(c,f'👤 {esc(u["name"])}\nID: <code>{uid}</code>\nПриглашено: {u["referrals"]}\nНачислено наград: {u["rewards"]}\nК выдаче: {u["pending_stars"]}⭐\nДо следующей награды: {REF_EVERY-u["referrals"]%REF_EVERY}\n\nЗа {REF_EVERY} приглашённых — {REF_REWARD}⭐. Новый пользователь должен впервые запустить бота по ссылке и подтвердить вступление в чат.\n\n<code>{link}</code>',k)
    elif d=='check': await c.message.answer(await confirm_ref(uid),reply_markup=back())
    elif d=='stats': await show(c,stats(),admin_kb())
    elif d.startswith('orders:'):
        page=max(0,int(d.split(':')[1])); rr=rows("SELECT * FROM orders WHERE status NOT IN ('done','rejected') ORDER BY id DESC LIMIT 9 OFFSET ?",(page*8,))
        k=[[(f'#{o["id"]} · {o["price"]} ₽ · {STATUS.get(o["status"],o["status"])}',f'order:{o["id"]}')] for o in rr[:8]]
        if page: k.append([('⬅️',f'orders:{page-1}')])
        if len(rr)>8: k.append([('➡️',f'orders:{page+1}')])
        await show(c,'🛒 Незавершённые заказы. /done НОМЕР открывает любой заказ.',kb(k+[[('Администратор','admin')]]))
    elif d.startswith('order:'): await order_view(c,int(d.split(':')[1]))
    elif d.startswith('ask:'):
        _,action,oid=d.split(':'); o=one('SELECT * FROM orders WHERE id=?',(int(oid),))
        if not o: return
        notes={'approve':'Ты проверил реальное поступление денег? Подарок отправится автоматически, Stars нужно выдать вручную.','retry':'Причина отказа Telegram устранена? Повторить отправку подарка?','reject':'Отклонить заказ? Эта кнопка НЕ возвращает деньги через CloudTips.','complete':'Ты действительно выдал товар или проверил успешную отправку? Кнопка только отмечает выдачу.'}
        if action in notes: await show(c,order_text(o)+'\n\n'+notes[action],kb([[('Да, подтверждаю',f'{action}:{oid}')],[('Назад',f'order:{oid}')]]))
    elif d.startswith(('approve:','retry:')):
        action,oid=d.split(':'); await approve(int(oid),uid,action=='retry'); await order_view(c,int(oid))
    elif d.startswith('reject:'):
        oid=int(d.split(':')[1]); changed=run("UPDATE orders SET status='rejected',admin_id=? WHERE id=? AND status IN ('new','review')",(uid,oid))
        if changed:
            o=one('SELECT * FROM orders WHERE id=?',(oid,))
            await notify(o['user_id'],f'❌ Заказ #{oid} отклонён. Если деньги списались, пришли продавцу чек и номер заказа. Автоматического возврата нет.',K(inline_keyboard=[[B(text='Связаться с продавцом',url=CONTACT)]]))
        await order_view(c,oid)
    elif d.startswith('complete:'):
        oid=int(d.split(':')[1]); changed=run("UPDATE orders SET status='done',admin_id=? WHERE id=? AND status IN ('approved','failed','uncertain')",(uid,oid))
        if changed:
            o=one('SELECT * FROM orders WHERE id=?',(oid,)); await notify(o['user_id'],f'✅ Администратор подтвердил выдачу заказа #{oid}. Спасибо за покупку!')
        await order_view(c,oid)
    elif d.startswith('payouts:'):
        page=max(0,int(d.split(':')[1])); rr=rows('SELECT * FROM users WHERE pending_stars>0 ORDER BY id LIMIT 9 OFFSET ?',(page*8,))
        k=[[(f'{u["id"]}: {u["pending_stars"]}⭐',f'payoutask:{u["id"]}:{u["pending_stars"]}')] for u in rr[:8]]
        if page: k.append([('⬅️',f'payouts:{page-1}')])
        if len(rr)>8: k.append([('➡️',f'payouts:{page+1}')])
        await show(c,'💸 Сначала выдай награду вручную, затем отметь выплату.',kb(k+[[('Администратор','admin')]]))
    elif d.startswith('payoutask:'):
        _,who,amount=d.split(':'); await show(c,f'Уже выдал {amount}⭐ пользователю {who}?',kb([[('Да, выдал',f'payout:{who}:{amount}')],[('Назад','payouts:0')]]))
    elif d.startswith('payout:'):
        _,who,amount=d.split(':'); who=int(who); amount=int(amount)
        with db() as con:
            changed=con.execute('UPDATE users SET pending_stars=0 WHERE id=? AND pending_stars=? AND pending_stars>0',(who,amount)).rowcount
            if changed: con.execute('INSERT INTO payout_log(user_id,amount,admin_id,created_at) VALUES(?,?,?,?)',(who,amount,uid,int(time.time())))
        if changed: await notify(who,f'✅ Администратор отметил выплату реферальной награды {amount}⭐.')
        await show(c,'Выплата отмечена.' if changed else 'Сумма изменилась или уже выплачена. Обнови список.',admin_kb())
    elif d=='broadcast':
        await state.clear(); await state.set_state(Form.broadcast)
        await show(c,'Пришли сообщение для рассылки. Затем будет предпросмотр.',kb([[('Отмена','admin')]]))
    elif d=='sendbroadcast':
        draft=await state.get_data()
        if not draft.get('source') and not draft.get('text'): await show(c,'Черновик отсутствует или уже отправлен.',admin_kb()); return
        await state.clear(); await show(c,'📨 Рассылка выполняется…')
        sent=0; users=rows('SELECT id FROM users')
        for u in users:
            for attempt in range(2):
                try:
                    if draft.get('source'): await bot.copy_message(u['id'],draft['chat'],draft['source'])
                    else: await bot.send_message(u['id'],draft['text'])
                    sent+=1; break
                except TelegramRetryAfter as e:
                    if attempt==0: await asyncio.sleep(e.retry_after)
                except Exception: break
            await asyncio.sleep(0.06)
        await c.message.answer(f'Доставлено: {sent}/{len(users)}.',reply_markup=admin_kb())
    elif d=='newraffle':
        await state.clear(); await state.set_state(Form.prize)
        await show(c,'Напиши название приза (до 150 символов). Приз розыгрыша выдаётся вручную.',kb([[('Отмена','admin')]]))
    elif d=='publishraffle':
        draft=await state.get_data()
        if not draft.get('prize') or 'description' not in draft: return
        await state.clear()
        with db() as con: rid=con.execute("INSERT INTO raffles(prize,description,status,created_at) VALUES(?,?,'draft',?)",(draft['prize'],draft['description'],int(time.time()))).lastrowid
        try: await bot.send_message(CHAT_ID,f'🎁 <b>Розыгрыш #{rid}</b>\nПриз: {esc(draft["prize"])}\n{esc(draft["description"])}\n\nОдин победитель. Окончание по решению администратора. Приз выдаётся вручную.',parse_mode='HTML',reply_markup=kb([[('🎉 Участвовать',f'join:{rid}')]]))
        except Exception:
            await show(c,'Публикация не подтверждена. Проверь чат и права бота. Розыгрыш остался неактивным.',admin_kb()); return
        run("UPDATE raffles SET status='active' WHERE id=?",(rid,))
        await show(c,f'Розыгрыш #{rid} опубликован.',admin_kb())
    elif d.startswith('raffles:'):
        page=max(0,int(d.split(':')[1])); rr=rows("SELECT * FROM raffles WHERE status='active' ORDER BY id DESC LIMIT 6 OFFSET ?",(page*5,)); text='🎁 <b>Активные розыгрыши</b>\n'; k=[]
        for r in rr[:5]:
            count=one('SELECT COUNT(*) n FROM raffle_entries WHERE raffle_id=?',(r['id'],))['n']
            text+=f'\n#{r["id"]}: {esc(r["prize"][:150])} · участников {count}\n'
            k.append([(f'Участвовать #{r["id"]}',f'join:{r["id"]}')])
            if uid in ADMIN_IDS: k.append([(f'🏆 Завершить #{r["id"]}',f'drawask:{r["id"]}')])
        if not rr: text+='Нет активных розыгрышей.'
        if page: k.append([('⬅️',f'raffles:{page-1}')])
        if len(rr)>5: k.append([('➡️',f'raffles:{page+1}')])
        await show(c,text,kb(k+[[('Главное меню','home')]]))
    elif d.startswith('drawask:'):
        rid=int(d.split(':')[1]); await show(c,f'Завершить #{rid} и выбрать одного победителя?',kb([[('Да, провести',f'draw:{rid}')],[('Назад','raffles:0')]]))
    elif d.startswith('draw:'):
        rid=int(d.split(':')[1])
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            r=con.execute("SELECT * FROM raffles WHERE id=? AND status='active'",(rid,)).fetchone()
            entries=con.execute('SELECT user_id FROM raffle_entries WHERE raffle_id=?',(rid,)).fetchall()
            winner=secrets.choice(entries)['user_id'] if r and entries else None
            if winner: con.execute("UPDATE raffles SET status='finished',winner_id=? WHERE id=?",(winner,rid))
        if not winner: await show(c,'Нет участников или розыгрыш завершён.',admin_kb()); return
        ok=await notify(CHAT_ID,f'🏆 Победитель #{rid}: <a href="tg://user?id={winner}">участник</a>! Приз: {esc(r["prize"])}. Выдача администратором.')
        await notify(winner,f'🎉 Ты победил в розыгрыше #{rid}! Свяжись с администратором.')
        await show(c,f'Победитель: {winner}. Результат сохранён. '+('Опубликован в чате.' if ok else 'Не удалось опубликовать — сообщи вручную.'),admin_kb())

@dp.message(Form.broadcast)
async def broadcast_draft(m:Message,state:FSMContext):
    if m.chat.type!='private' or m.from_user.id not in ADMIN_IDS: return
    try: await m.copy_to(m.chat.id)
    except TelegramBadRequest:
        await m.answer('Это сообщение нельзя скопировать. Пришли обычный текст или фото.'); return
    await state.set_state(None); await state.update_data(source=m.message_id,chat=m.chat.id)
    await m.answer('Отправить это сообщение всем?',reply_markup=kb([[('📨 Отправить','sendbroadcast')],[('Отмена','admin')]]))

@dp.message(Form.prize)
async def prize(m:Message,state:FSMContext):
    if m.chat.type!='private' or m.from_user.id not in ADMIN_IDS: return
    if not m.text or len(m.text)>150: await m.answer('Нужно текстовое название до 150 символов.'); return
    await state.update_data(prize=m.text); await state.set_state(Form.description)
    await m.answer('Описание до 1500 символов; «-» — без описания.',reply_markup=kb([[('Отмена','admin')]]))

@dp.message(Form.description)
async def description(m:Message,state:FSMContext):
    if m.chat.type!='private' or m.from_user.id not in ADMIN_IDS: return
    if not m.text or len(m.text)>1500: await m.answer('Нужно текстовое описание до 1500 символов.'); return
    await state.update_data(description='' if m.text=='-' else m.text); await state.set_state(None)
    draft=await state.get_data()
    await m.answer(f'Приз: {draft["prize"]}\n{draft["description"]}\n\nОпубликовать?',reply_markup=kb([[('Опубликовать','publishraffle')],[('Отмена','admin')]]))

async def main():
    global bot,bot_name
    if BOT_TOKEN=='ВСТАВЬ_ТОКЕН': raise RuntimeError('Укажи BOT_TOKEN в настройках или окружении')
    for channel in REQUIRED_CHANNELS:
        if not isinstance(channel.get('id'),(str,int)) or not str(channel.get('link','')).startswith('https://t.me/'):
            raise ValueError('В REQUIRED_CHANNELS нужны id и link вида https://t.me/...')
    init_db()
    bot=Bot(BOT_TOKEN)
    gate=SubscriptionGate()
    dp.message.outer_middleware(gate)
    dp.callback_query.outer_middleware(gate)
    async with bot:
        bot_name=(await bot.get_me()).username
        await dp.start_polling(bot,close_bot_session=False)

if __name__=='__main__': asyncio.run(main())
