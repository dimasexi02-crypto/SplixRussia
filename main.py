"""Stars Referrals, Telegram / Python 3.11+.
Установка: pip install python-telegram-bot==21.6
Токен и ID администратора укажите ниже. .env не нужен.
Запуск: python stars_referrals_v2.py
SQLite совместима с первой stars_referrals.py; перед переносом остановите
старый процесс и скопируйте базу. Только один процесс на токен/базу.
Баланс внутренний; автоматического перевода Telegram Stars нет.
Капча — простая кнопочная проверка, не защита от ферм аккаунтов.
Рассылка: /admin → Рассылка → сообщение → подтверждение.
После перезапуска незавершённая рассылка не возобновляется автоматически.
"""
import asyncio
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.error import TelegramError, RetryAfter, Forbidden

TOKEN = '8759205415:AAFIUltVpc6qxPfKUGtPDMY1q31RJrSrAmg'
ADMIN_IDS = {8018644395}  # Замените на свой числовой Telegram ID.
DB = str(Path(__file__).with_name('stars_referrals.sqlite3'))
NAME = 'Stars Referrals'
logging.basicConfig(level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger(NAME)

@contextmanager
def db():
    c=sqlite3.connect(DB,timeout=30)
    c.row_factory=sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT,
         referrer INTEGER,verified INTEGER DEFAULT 0,rewarded INTEGER DEFAULT 0,
         balance INTEGER DEFAULT 0,earned INTEGER DEFAULT 0,created REAL);
        CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT,
         uid INTEGER,amount INTEGER,status TEXT DEFAULT 'pending',created REAL,
         admin INTEGER,note TEXT DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON withdrawals(uid) WHERE status='pending';
        CREATE TABLE IF NOT EXISTS captcha(uid INTEGER PRIMARY KEY,passed INTEGER DEFAULT 0,
         nonce TEXT,answer INTEGER,expires REAL DEFAULT 0,failures INTEGER DEFAULT 0,blocked REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS delivery(uid INTEGER PRIMARY KEY,optin INTEGER DEFAULT 1);
        ''')
        for k,v in {'reward':'100','minimum':'500','minrefs':'5','enabled':'0','support':''}.items():
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,v))

def one(sql,args=()):
    with db() as c:
        return c.execute(sql,args).fetchone()

def allrows(sql,args=()):
    with db() as c:
        return c.execute(sql,args).fetchall()

def write(sql,args=()):
    with db() as c:
        c.execute(sql,args)

def setting(k):
    return one('SELECT value FROM settings WHERE key=?',(k,))['value']

def private(u):
    return u.effective_chat and u.effective_chat.type=='private'

def admin_ok(u):
    return private(u) and u.effective_user.id in ADMIN_IDS

async def notify(bot,uid,text):
    try:
        await bot.send_message(uid,text)
    except TelegramError:
        log.warning('Не доставлено уведомление ID %s',uid)

def register(user,payload=''):
    with db() as c:
        existing=c.execute('SELECT id FROM users WHERE id=?',(user.id,)).fetchone()
        if existing:
            c.execute('UPDATE users SET username=? WHERE id=?',(user.username or '',user.id))
        else:
            ref=None
            if payload.startswith('ref_') and payload[4:].isdigit():
                candidate=int(payload[4:])
                if candidate!=user.id and c.execute('SELECT id FROM users WHERE id=?',(candidate,)).fetchone():
                    ref=candidate
            c.execute('INSERT INTO users(id,username,referrer,created) VALUES(?,?,?,?)',
                      (user.id,user.username or '',ref,time.time()))
        c.execute('INSERT OR IGNORE INTO captcha(uid) VALUES(?)',(user.id,))
        c.execute('INSERT OR IGNORE INTO delivery(uid) VALUES(?)',(user.id,))

def passed(uid):
    r=one('SELECT passed FROM captcha WHERE uid=?',(uid,))
    return bool(r and r['passed'])

SHAPES=[('⭕','круг'),('🔷','ромб'),('🟦','квадрат'),('⭐','звезда')]

async def challenge(u,ctx):
    uid=u.effective_user.id
    r=one('SELECT * FROM captcha WHERE uid=?',(uid,))
    if r and r['blocked']>time.time():
        return await u.effective_message.reply_text('Слишком много ошибок. Повтори через минуту.')
    answer=secrets.randbelow(4)
    nonce=secrets.token_hex(6)
    write('UPDATE captcha SET nonce=?,answer=?,expires=? WHERE uid=?',(nonce,answer,time.time()+300,uid))
    order=list(range(4)); secrets.SystemRandom().shuffle(order)
    buttons=[[B(f'{SHAPES[i][0]} {SHAPES[i][1]}',callback_data=f'cap:{nonce}:{i}')] for i in order]
    await u.effective_message.reply_text(f'Проверка: Нажми на {SHAPES[answer][1]} {SHAPES[answer][0]}\n\nВыбери один вариант.',reply_markup=K(buttons))

async def missing(bot,uid):
    result=[]
    for ch in allrows('SELECT * FROM channels'):
        cid=int(ch['id']) if ch['id'].lstrip('-').isdigit() else ch['id']
        try:
            member=await bot.get_chat_member(cid,uid)
        except TelegramError:
            raise ValueError('Не удалось проверить подписки. Организатору нужно проверить ID каналов и права бота.')
        active=member.status in ('creator','administrator','member') or (member.status=='restricted' and member.is_member)
        if not active:
            result.append(ch)
    return result

async def check_channels(u,ctx):
    uid=u.effective_user.id
    if not passed(uid):
        return await challenge(u,ctx)
    channels=await missing(ctx.bot,uid)
    if channels:
        buttons=[[B(f'📢 Канал {i+1}',url=r['url'])] for i,r in enumerate(channels)]
        buttons.append([B('Проверить подписки',callback_data='check')])
        return await u.effective_message.reply_text(
            f'Осталось подписаться на каналы.\n\nНужно: {len(channels)}. Открывай по одному и подписывайся, потом нажми проверку.',reply_markup=K(buttons))
    reward_notice=None
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        account=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        enabled=c.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0]=='1'
        c.execute('UPDATE users SET verified=1 WHERE id=?',(uid,))
        if enabled and account['referrer'] and not account['rewarded']:
            amount=int(c.execute("SELECT value FROM settings WHERE key='reward'").fetchone()[0])
            c.execute('UPDATE users SET balance=balance+?,earned=earned+? WHERE id=?',(amount,amount,account['referrer']))
            c.execute('UPDATE users SET rewarded=1 WHERE id=?',(uid,))
            reward_notice=(account['referrer'],amount)
    if reward_notice:
        await notify(ctx.bot,reward_notice[0],f'Пришёл друг: +{reward_notice[1]}★ на внутренний баланс. Подписки проверены.')
    await profile(u,ctx)

async def profile(u,ctx):
    uid=u.effective_user.id
    a=one('SELECT * FROM users WHERE id=?',(uid,))
    count=one('SELECT COUNT(*) n FROM users WHERE referrer=? AND rewarded=1',(uid,))['n']
    pending=one("SELECT * FROM withdrawals WHERE uid=? AND status='pending'",(uid,))
    link=f'https://t.me/{ctx.bot.username}?start=ref_{uid}'
    text=(f'⭐ {NAME}\n\nТвоя ссылка:\n{link}\n'
          f'За каждого нового друга, который зайдёт, пройдёт проверку и подпишется — {setting("reward")}★.\n'
          f'Приглашено: {count} из {setting("minrefs")}\nЗаработано: {a["earned"]}★\n'
          f'В заявке на вывод: {pending["amount"] if pending else 0}★\n\n'
          f'Доступно к выводу: {a["balance"]}★\nМинимум заявки: {setting("minimum")}★.\n\n'
          '★ здесь — внутренние единицы вознаграждения, не баланс Telegram Stars. '
          'Выплата Stars выполняется организатором вручную за его счёт. Бот Stars не переводит.\n'
          'Новости: /stopnews — отключить, /news — включить.')
    if setting('enabled')!='1':
        text+='\nНачисления и новые заявки приостановлены.'
    share='https://t.me/share/url?'+urlencode({'url':link,'text':f'{NAME} — реферальная программа. Условия в боте.'})
    buttons=[[B('Поделиться ссылкой',url=share)],[B('Вывести звёзды',callback_data='withdraw')],
             [B('Обновить',callback_data='profile')],[B('Помощь',callback_data='help')],
             [B('Проверить подписки',callback_data='check')]]
    await u.effective_message.reply_text(text,reply_markup=K(buttons),disable_web_page_preview=True)

async def start(u,ctx):
    if not private(u): return
    register(u.effective_user,ctx.args[0] if ctx.args else '')
    if not passed(u.effective_user.id):
        await u.effective_message.reply_text(f'⭐ {NAME}\n\nРеферальная программа за подписки и приглашённых друзей.\n\nСначала — короткая проверка, что ты человек.',reply_markup=K([[B('Помощь',callback_data='help')]]))
        return await challenge(u,ctx)
    await profile(u,ctx)

async def admin(u,ctx):
    if not admin_ok(u):
        return await u.effective_message.reply_text('Доступ запрещён.')
    await u.effective_message.reply_text(f'{NAME} — админ-панель',reply_markup=K([
        [B('Статистика',callback_data='a:stats'),B('Заявки',callback_data='a:pending:0')],
        [B('Каналы / настройки',callback_data='a:settings')],
        [B('Рассылка',callback_data='a:broadcast')]]))

ADMIN_HELP='''Команды администратора:
/set reward 100
/set minimum 500
/set minrefs 5
/set enabled 1 — включить начисления и заявки
/set enabled 0 — приостановить
/set support @username

/channel_add @publicchannel https://t.me/publicchannel
/channel_add 4448446792 https://t.me/+ПРИГЛАСИТЕЛЬНАЯ_ССЫЛКА
/channel_add -1004448446792 https://t.me/+ПРИГЛАСИТЕЛЬНАЯ_ССЫЛКА
/channel_del ID_из_списка

Бот должен быть администратором каналов. Пригласительная ссылка должна вести в указанный канал; проверьте её сами.
/paid ID_заявки подтверждение — отметить РЕАЛЬНО выполненную выплату
/reject ID_заявки причина — отклонить с возвратом резерва
/cancel — отменить подготовку рассылки

Новые награды применяются к будущим начислениям. Уже начисленное не пересчитывается.
'''

async def admin_callback(u,ctx,data):
    if not admin_ok(u): raise ValueError('Доступ запрещён.')
    if data=='a:settings':
        channels=allrows('SELECT * FROM channels')
        text=ADMIN_HELP+'\n'+'\n'.join(f'{key}: {setting(key)}' for key in ('reward','minimum','minrefs','enabled','support'))
        text+='\nКаналы:\n'+'\n'.join(f'{r["id"]}: {r["url"]}' for r in channels)
        await u.effective_message.reply_text(text,disable_web_page_preview=True)
    elif data=='a:stats':
        r=one('SELECT COUNT(*) n,COALESCE(SUM(balance),0) b FROM users')
        w=one("SELECT COUNT(*) n,COALESCE(SUM(amount),0) a FROM withdrawals WHERE status='pending'")
        await u.effective_message.reply_text(f'Пользователей: {r["n"]}\nСвободные балансы: {r["b"]}\nОжидающих заявок: {w["n"]}\nВ резерве: {w["a"]}')
    elif data.startswith('a:pending:'):
        offset=max(0,int(data.rsplit(':',1)[1]))
        items=allrows("SELECT w.*,u.username FROM withdrawals w JOIN users u ON u.id=w.uid WHERE status='pending' ORDER BY w.id LIMIT 10 OFFSET ?",(offset,))
        text='Заявки:\n'+'\n'.join(f'#{r["id"]}: ID {r["uid"]}, @{r["username"] or "нет"}, {r["amount"]}★' for r in items)
        text+='\nПосле ручной выплаты: /paid ID подтверждение\nОтказ: /reject ID причина'
        buttons=[]
        if offset: buttons.append(B('Назад',callback_data=f'a:pending:{max(0,offset-10)}'))
        if len(items)==10: buttons.append(B('Далее',callback_data=f'a:pending:{offset+10}'))
        await u.effective_message.reply_text(text,reply_markup=K([buttons]) if buttons else None)
    elif data=='a:broadcast':
        if ctx.application.bot_data.get('broadcast_running'):
            raise ValueError('Рассылка уже выполняется.')
        ctx.user_data.pop('draft',None)
        ctx.user_data['await_broadcast']=True
        await u.effective_message.reply_text('Отправьте одно сообщение: текст, фото, видео или документ. Покажу предпросмотр. Альбомы не поддерживаются. /cancel — отмена.')
    elif data.startswith('a:cancel:'):
        ctx.user_data.pop('draft',None)
        ctx.user_data.pop('await_broadcast',None)
        await u.effective_message.reply_text('Черновик отменён.')
    elif data.startswith('a:send:'):
        draft=ctx.user_data.get('draft')
        if not draft or draft['nonce']!=data.rsplit(':',1)[1] or draft['expires']<time.time():
            raise ValueError('Предпросмотр устарел. Подготовьте рассылку заново.')
        if ctx.application.bot_data.get('broadcast_running'):
            raise ValueError('Рассылка уже выполняется.')
        recipients=[r['uid'] for r in allrows('SELECT uid FROM delivery WHERE optin=1')]
        ctx.application.bot_data['broadcast_running']=True
        ctx.user_data.pop('draft',None)
        ctx.application.create_task(broadcast(ctx.application,draft,recipients,u.effective_user.id))
        await u.effective_message.reply_text(f'Рассылка запущена. Получателей: {len(recipients)}. Не перезапускайте бота до отчёта.')

async def broadcast(app,draft,recipients,admin_id):
    sent=failed=skipped=0
    try:
        for uid in recipients:
            r=one('SELECT optin FROM delivery WHERE uid=?',(uid,))
            if not r or not r['optin']:
                skipped+=1; continue
            for attempt in range(3):
                try:
                    await app.bot.copy_message(uid,draft['chat'],draft['message'])
                    sent+=1; break
                except RetryAfter as e:
                    delay=e.retry_after.total_seconds() if hasattr(e.retry_after,'total_seconds') else float(e.retry_after)
                    if attempt==2:
                        failed+=1
                    else:
                        await asyncio.sleep(delay+1)
                except Forbidden:
                    write('UPDATE delivery SET optin=0 WHERE uid=?',(uid,))
                    failed+=1; break
                except TelegramError:
                    failed+=1; break
            await asyncio.sleep(.08)
        await notify(app.bot,admin_id,f'Рассылка завершена. Доставлено: {sent}; ошибок: {failed}; пропущено: {skipped}.')
    finally:
        app.bot_data['broadcast_running']=False

async def draft_message(u,ctx):
    if not admin_ok(u) or not ctx.user_data.get('await_broadcast'): return
    if u.message.media_group_id:
        return await u.message.reply_text('Отправьте одно сообщение без альбома.')
    try:
        await u.message.reply_text('Предпросмотр рассылки:')
        preview=await ctx.bot.copy_message(u.effective_chat.id,u.effective_chat.id,u.message.message_id)
    except TelegramError:
        return await u.message.reply_text('Не получилось скопировать сообщение. Попробуйте текст или одиночное фото.')
    nonce=secrets.token_hex(6)
    ctx.user_data['draft']={'chat':u.effective_chat.id,'message':preview.message_id,'nonce':nonce,'expires':time.time()+600}
    ctx.user_data['await_broadcast']=False
    count=one('SELECT COUNT(*) n FROM delivery WHERE optin=1')['n']
    await u.message.reply_text(f'Отправить подписанным на новости пользователям ({count})? Подтверждение действует 10 минут.',reply_markup=K([
        [B('Отправить',callback_data=f'a:send:{nonce}'),B('Отмена',callback_data=f'a:cancel:{nonce}')]]))

async def click(u,ctx):
    await u.callback_query.answer()
    if not private(u): return
    uid=u.effective_user.id
    register(u.effective_user)
    data=u.callback_query.data
    try:
        if data.startswith('a:'): return await admin_callback(u,ctx,data)
        if data=='help':
            return await u.effective_message.reply_text('Пригласи новых друзей своей ссылкой. Друг проходит капчу и проверку подписок. За один Telegram ID награда даётся один раз; самоприглашение запрещено.\nБаланс внутренний. Заявки на Stars рассматривает и оплачивает организатор вручную; бот не переводит Stars автоматически.\nНовости: /stopnews — отключить.\nПоддержка: '+(setting('support') or 'контакт пока не указан'))
        if data.startswith('cap:'):
            _,nonce,answer=data.split(':')
            ok=False
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute('SELECT * FROM captcha WHERE uid=?',(uid,)).fetchone()
                if r['passed']:
                    ok=True
                elif r['blocked']>time.time():
                    raise ValueError('Повтори проверку через минуту.')
                elif r['nonce']!=nonce or r['expires']<time.time():
                    raise ValueError('Проверка устарела. Отправь /start.')
                elif str(r['answer'])==answer:
                    c.execute('UPDATE captcha SET passed=1,nonce=NULL,failures=0 WHERE uid=?',(uid,))
                    ok=True
                else:
                    failures=r['failures']+1
                    c.execute('UPDATE captcha SET nonce=NULL,failures=?,blocked=? WHERE uid=?',
                              (0 if failures>=3 else failures,time.time()+60 if failures>=3 else 0,uid))
            try: await u.callback_query.edit_message_reply_markup(reply_markup=None)
            except TelegramError: pass
            if not ok:
                await u.effective_message.reply_text('Неверный вариант. Попробуй ещё раз.')
                return await challenge(u,ctx)
            await u.effective_message.reply_text('Готово, проверка пройдена.')
            return await check_channels(u,ctx)
        if not passed(uid): return await challenge(u,ctx)
        if data=='profile': return await profile(u,ctx)
        if data=='check': return await check_channels(u,ctx)
        if data=='withdraw':
            if await missing(ctx.bot,uid):
                raise ValueError('Сначала проверь подписки кнопкой «Проверить подписки».')
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                existing=c.execute("SELECT * FROM withdrawals WHERE uid=? AND status='pending'",(uid,)).fetchone()
                if existing:
                    raise ValueError(f'Заявка #{existing["id"]} на {existing["amount"]}★ уже на рассмотрении.\n\nДождёмся решения.')
                cfg={r['key']:r['value'] for r in c.execute('SELECT * FROM settings')}
                if cfg['enabled']!='1': raise ValueError('Приём заявок приостановлен.')
                a=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
                refs=c.execute('SELECT COUNT(*) FROM users WHERE referrer=? AND rewarded=1',(uid,)).fetchone()[0]
                if a['balance']<int(cfg['minimum']) or refs<int(cfg['minrefs']):
                    raise ValueError(f'Для заявки нужно минимум {cfg["minimum"]}★ и {cfg["minrefs"]} зачтённых друзей.')
                amount=a['balance']
                wid=c.execute('INSERT INTO withdrawals(uid,amount,created) VALUES(?,?,?)',(uid,amount,time.time())).lastrowid
                c.execute('UPDATE users SET balance=balance-? WHERE id=?',(amount,uid))
            await u.effective_message.reply_text(f'Заявка #{wid} на {amount}★ на рассмотрении.\n\nСумма зарезервирована. Stars пока не переведены.')
            for aid in ADMIN_IDS:
                await notify(ctx.bot,aid,f'Новая заявка #{wid}: ID {uid}, {amount}★. /admin')
    except ValueError as e:
        await u.effective_message.reply_text(str(e))

def normalize_channel(value):
    if value.startswith('@'): return value
    if value.startswith('https://t.me/') and '+' not in value:
        return '@'+value.rstrip('/').rsplit('/',1)[1]
    if not value.lstrip('-').isdigit():
        raise ValueError('Нужен @username или числовой ID. Пригласительную ссылку укажите вторым аргументом.')
    if value.startswith('-100'): return int(value)
    if value.isdigit() and int(value)>0: return int('-100'+value)
    raise ValueError('ID канала должен быть -100… или положительная короткая часть ID.')

async def admin_cmd(u,ctx):
    if not admin_ok(u): return await u.effective_message.reply_text('Доступ запрещён.')
    cmd=u.message.text.split()[0].split('@')[0]
    args=ctx.args
    try:
        if cmd=='/cancel':
            ctx.user_data.pop('draft',None);ctx.user_data.pop('await_broadcast',None)
            return await u.message.reply_text('Подготовка отменена. Уже запущенная рассылка продолжится.')
        if cmd=='/set':
            if len(args)<2: raise ValueError('/set reward 100; остальные настройки в /admin.')
            k=args[0]; v=' '.join(args[1:])
            if k not in ('reward','minimum','minrefs','enabled','support'): raise ValueError('Неизвестная настройка.')
            if k!='support':
                if not v.isdigit(): raise ValueError('Нужно целое число.')
                n=int(v)
                if k=='enabled' and n not in (0,1): raise ValueError('enabled: 0 или 1.')
                if k in ('minimum','reward') and not 1<=n<=1000000: raise ValueError('Сумма 1–1000000.')
                if k=='minrefs' and n>100000: raise ValueError('Слишком большое значение.')
                v=str(n)
            elif len(v)>200: raise ValueError('Контакт до 200 символов.')
            write('UPDATE settings SET value=? WHERE key=?',(v,k))
            return await u.message.reply_text('Настройка сохранена.')
        if cmd=='/channel_add':
            if len(args)!=2 or not args[1].startswith('https://t.me/'):
                raise ValueError('/channel_add @канал https://t.me/канал\nили /channel_add 4448446792 https://t.me/+ссылка')
            chat=await ctx.bot.get_chat(normalize_channel(args[0]))
            if chat.type not in ('channel','supergroup'): raise ValueError('Нужен канал или супергруппа.')
            member=await ctx.bot.get_chat_member(chat.id,ctx.bot.id)
            if member.status not in ('administrator','creator'): raise ValueError('Назначьте бота администратором канала.')
            with db() as c:
                count=c.execute('SELECT COUNT(*) FROM channels').fetchone()[0]
                exists=c.execute('SELECT id FROM channels WHERE id=?',(str(chat.id),)).fetchone()
                if count>=20 and not exists: raise ValueError('Максимум 20 каналов.')
                c.execute('INSERT OR REPLACE INTO channels VALUES(?,?)',(str(chat.id),args[1]))
            return await u.message.reply_text(f'Добавлен {chat.title}, ID {chat.id}. Проверьте правильность пригласительной ссылки.')
        if cmd=='/channel_del':
            if len(args)!=1: raise ValueError('/channel_del ID_из_списка')
            cid=normalize_channel(args[0])
            if isinstance(cid,str): cid=(await ctx.bot.get_chat(cid)).id
            with db() as c:
                changed=c.execute('DELETE FROM channels WHERE id=?',(str(cid),)).rowcount
            return await u.message.reply_text('Удалён.' if changed else 'Канал не найден.')
        if cmd in ('/paid','/reject'):
            if len(args)<2 or not args[0].isdigit(): raise ValueError(f'{cmd} ID_заявки подтверждение_или_причина')
            wid=int(args[0]); note=' '.join(args[1:])[:1000]
            status='paid' if cmd=='/paid' else 'rejected'
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                w=c.execute('SELECT * FROM withdrawals WHERE id=?',(wid,)).fetchone()
                if not w or w['status']!='pending': raise ValueError('Заявка отсутствует или уже обработана.')
                c.execute('UPDATE withdrawals SET status=?,admin=?,note=? WHERE id=?',(status,u.effective_user.id,note,wid))
                if status=='rejected':
                    c.execute('UPDATE users SET balance=balance+? WHERE id=?',(w['amount'],w['uid']))
            await u.message.reply_text('Статус сохранён.')
            text=f'Организатор отметил заявку #{wid} как выплаченную вручную.' if status=='paid' else f'Заявка #{wid} отклонена. {w["amount"]}★ возвращено на внутренний баланс.'
            return await notify(ctx.bot,w['uid'],text+'\nКомментарий: '+note)
    except (ValueError,TelegramError) as e:
        await u.message.reply_text('Не выполнено: '+str(e))

async def news(u,ctx):
    if not private(u): return
    register(u.effective_user)
    enabled=not u.message.text.startswith('/stopnews')
    write('UPDATE delivery SET optin=? WHERE uid=?',(int(enabled),u.effective_user.id))
    await u.message.reply_text('Рассылка включена.' if enabled else 'Рассылка отключена. Уведомления по заявкам сохраняются.')

async def errors(u,ctx):
    log.error('Ошибка обработчика',exc_info=(type(ctx.error),ctx.error,ctx.error.__traceback__))
    if u and getattr(u,'effective_message',None):
        try: await u.effective_message.reply_text('Ошибка обработки. Попробуйте позже; администратору нужно проверить журнал.')
        except TelegramError: pass

async def post_init(app):
    await app.bot.set_my_commands([('start','Главное меню'),('admin','Админ-панель'),('stopnews','Отключить рассылку')])
    log.info('Stars Referrals запущен: @%s',app.bot.username)

def main():
    if 'ВСТАВЬТЕ' in TOKEN or not TOKEN:
        raise SystemExit('Вставьте НОВЫЙ токен в TOKEN в начале файла.')
    if ADMIN_IDS=={123456789}:
        raise SystemExit('Укажите свой числовой Telegram ID в ADMIN_IDS.')
    init()
    app=Application.builder().token(TOKEN).concurrent_updates(False).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('admin',admin))
    app.add_handler(CommandHandler(['set','channel_add','channel_del','paid','reject','cancel'],admin_cmd))
    app.add_handler(CommandHandler(['news','stopnews'],news))
    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND,draft_message))
    app.add_error_handler(errors)
    app.run_polling(allowed_updates=['message','callback_query'])

if __name__=='__main__':
    main()
