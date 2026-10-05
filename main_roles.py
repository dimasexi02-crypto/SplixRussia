"""SPLIX MANAGER — VK, глобальная модерация.
Python 3.10+. Установка: pip install vk-api python-dotenv
.env: VK_TOKEN=
ID сообщества определяется автоматически через groups.getById.

Включите сообщения сообщества, Bot Long Poll / message_new, добавьте
сообщество администратором каждой беседы.
Это самостоятельная версия: старую базу не изменяет.

Владелец беседы: !setup (проверка is_owner через VK).
!создатьсвязь Название — создаёт объединение, возвращает его ID.
!связать ID — владелец другой беседы подаёт заявку.
!заявки и !принять PEER_ID — владелец объединения одобряет заявку.
!связи, !отвязать — просмотр / выход.
Название не является паролем. Просто знать ID недостаточно для подключения.
Глобальные команды доступны только создателю объединения.
/gban ID [дни] [причина], /gunban ID, /gkick ID, /gluck ID (алиас gkick),
/gsnick ID ник, /grnick ID, /gzov текст.
Цель: ID, ссылка VK, упоминание или ответ на сообщение.
Локальные команды: /ban /unban /kick /snick /rnick /nick /banlist.
Массовые команды действуют только на явно подключённые беседы.
Не реализованы игры, VIP, миграция и весь перечень оригинального ТЗ.
"""
import os
import re
import time
import secrets
import sqlite3
import logging
from urllib.parse import urlparse
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('SPLIX MANAGER')
DB = os.getenv('SPLIX_GLOBAL_DB', 'splix_global.sqlite3')
TOKEN = os.getenv('VK_TOKEN', '').strip()


def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS networks(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, owner INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS chats(
          peer INTEGER PRIMARY KEY, owner INTEGER NOT NULL,
          network TEXT REFERENCES networks(id));
        CREATE TABLE IF NOT EXISTS requests(
          network TEXT REFERENCES networks(id), peer INTEGER REFERENCES chats(peer),
          applicant INTEGER NOT NULL, PRIMARY KEY(network,peer));
        CREATE TABLE IF NOT EXISTS nicks(
          peer INTEGER, uid INTEGER, nick TEXT NOT NULL, PRIMARY KEY(peer,uid));
        CREATE TABLE IF NOT EXISTS bans(
          peer INTEGER, uid INTEGER, until REAL NOT NULL, reason TEXT NOT NULL,
          PRIMARY KEY(peer,uid));
        ''')


def row(sql, args=()):
    with connect() as c:
        return c.execute(sql, args).fetchone()


def rows(sql, args=()):
    with connect() as c:
        return c.execute(sql, args).fetchall()


def write(sql, args=()):
    with connect() as c:
        c.execute(sql, args)


def send(peer, text):
    # VK ограничивает размер сообщения; длинные отчёты разбиваются.
    for start in range(0, len(text), 3500):
        vk.messages.send(peer_id=peer, random_id=secrets.randbelow(2147483646)+1,
                         message=text[start:start+3500], disable_mentions=1)
        time.sleep(0.35)


def members(peer):
    return vk.messages.getConversationMembers(peer_id=peer)['items']


def owner(peer):
    for item in members(peer):
        if item.get('is_owner'):
            return int(item['member_id'])
    raise ValueError('VK не вернул владельца беседы. Проверьте права бота.')


def check_owner(peer, uid):
    actual = owner(peer)
    if actual != uid:
        raise ValueError('Команда доступна только владельцу беседы ВК.')
    write('INSERT INTO chats(peer,owner) VALUES(?,?) ON CONFLICT(peer) DO UPDATE SET owner=excluded.owner',
          (peer, actual))


def target(message, text):
    reply = message.get('reply_message') or {}
    if reply.get('from_id'):
        uid, rest = int(reply['from_id']), text.strip()
    else:
        match = re.match(r'\[id(\d+)\|[^\]]*\](.*)', text, re.S)
        if match:
            uid, rest = int(match[1]), match[2].strip()
        else:
            parts = text.split(maxsplit=1)
            if not parts:
                raise ValueError('Укажите пользователя или ответьте на его сообщение.')
            value, rest = parts[0], parts[1] if len(parts)>1 else ''
            if '://' in value or value.startswith(('vk.com/', 'vk.ru/')):
                parsed = urlparse(value if '://' in value else 'https://'+value)
                if parsed.netloc.lower() not in ('vk.com','www.vk.com','m.vk.com','vk.ru','www.vk.ru'):
                    raise ValueError('Нужна ссылка ВК.')
                value = parsed.path.strip('/')
            if re.fullmatch(r'(?:id)?\d+', value):
                uid = int(value.removeprefix('id'))
            else:
                resolved = vk.utils.resolveScreenName(screen_name=value.lstrip('@'))
                if not resolved or resolved.get('type') != 'user':
                    raise ValueError('Пользователь не найден.')
                uid = int(resolved['object_id'])
    if uid <= 0:
        raise ValueError('Нужен ID пользователя, не сообщества.')
    return uid, rest


def active_ban(peer, uid):
    b = row('SELECT * FROM bans WHERE peer=? AND uid=?', (peer,uid))
    if b and b['until'] and b['until'] <= time.time():
        write('DELETE FROM bans WHERE peer=? AND uid=?', (peer,uid))
        return None
    return b


def delete_message(message):
    cmid = message.get('conversation_message_id')
    if cmid:
        vk.messages.delete(peer_id=message['peer_id'], cmids=str(cmid), delete_for_all=1)


def kick(peer, uid):
    vk.messages.removeChatUser(chat_id=peer-2000000000, member_id=uid)


def protected(peer, uid, actor):
    if uid == actor:
        raise ValueError('Нельзя применить это действие к себе.')
    for m in members(peer):
        if int(m['member_id']) == uid and (m.get('is_owner') or m.get('is_admin')):
            raise ValueError('Администратор/владелец ВК защищён.')


HELP = '''SPLIX MANAGER · VK
!setup — регистрация владельцем беседы
!создатьсвязь Название — создать объединение
!связать ID — заявка на подключение
!заявки / !принять PEER_ID — управление заявками
!связи / !отвязать

Глобальные команды (создатель объединения):
/gban ID [дни] [причина] — бан во всех связанных беседах
/gunban ID — снять бан
/gkick ID — исключить (/gluck — алиас)
/gsnick ID ник — установить ник
/grnick ID — убрать ник
/gzov текст — оповестить связанные беседы

Локальные команды (владелец беседы):
/ban /unban /kick /snick /rnick /banlist
/nick ID — посмотреть ник
!пинг / !команды

Работают ! и /, регистр не важен, пробел после префикса допустим.
При ответе на сообщение ID не нужен. Бан: 0 дней = бессрочно.
/gzov отправляет текст, без массового упоминания участников.'''

ALIASES = {'пинг':'ping','команды':'help','начать':'setup',
 'создатьсвязь':'createunity','связать':'addunity','связи':'unity',
 'заявки':'requests','принять':'accept','отвязать':'removeunity',
 'gluck':'gkick','гбан':'gban','гунбан':'gunban','гкик':'gkick',
 'гсник':'gsnick','грник':'grnick','гвызов':'gzov',
 'бан':'ban','унбан':'unban','кик':'kick','сник':'snick','рник':'rnick','ник':'nick'}


def network_for(peer, actor):
    n = row('SELECT n.* FROM networks n JOIN chats c ON c.network=n.id WHERE c.peer=?', (peer,))
    if not n:
        raise ValueError('Беседа не подключена к объединению.')
    if n['owner'] != actor:
        raise ValueError('Глобальные команды доступны создателю объединения.')
    # Потеря прав владельца текущей беседы прекращает доступ из неё.
    check_owner(peer, actor)
    return n


def operate(cmd, peer, uid, rest, actor):
    if cmd in ('ban','kick','snick','rnick','unban'):
        protected(peer, uid, actor)
    if cmd == 'ban':
        parts = rest.split(maxsplit=1)
        days = 0
        reason = rest or 'Без причины'
        if parts and re.fullmatch(r'-?\d+', parts[0]):
            days = int(parts[0])
            if not 0 <= days <= 36500:
                raise ValueError('Дни: от 0 до 36500.')
            reason = parts[1] if len(parts)>1 else 'Без причины'
        write('INSERT OR REPLACE INTO bans VALUES(?,?,?,?)',
              (peer,uid,time.time()+days*86400 if days else 0,reason))
        try:
            kick(peer,uid)
            return 'бан сохранён, участник исключён'
        except vk_api.exceptions.ApiError as e:
            return f'бан сохранён, исключить не удалось (VK {e.code})'
    if cmd == 'unban':
        write('DELETE FROM bans WHERE peer=? AND uid=?',(peer,uid))
        return 'бан снят; приглашение обратно не отправляется'
    if cmd == 'kick':
        kick(peer,uid)
        return 'исключён'
    if cmd == 'snick':
        if not 1 <= len(rest.strip()) <= 50:
            raise ValueError('Ник: от 1 до 50 символов.')
        write('INSERT OR REPLACE INTO nicks VALUES(?,?,?)',(peer,uid,rest.strip()))
        return 'ник сохранён в базе бота (имя аккаунта VK не меняется)'
    if cmd == 'rnick':
        write('DELETE FROM nicks WHERE peer=? AND uid=?',(peer,uid))
        return 'ник удалён'
    raise ValueError('Неизвестное действие.')


def command(message):
    peer, actor = message['peer_id'], message['from_id']
    parsed = re.match(r'^[!/]\s*(\S+)(?:\s+(.*))?$', message.get('text','').strip(), re.S)
    if not parsed:
        return
    cmd = parsed[1].lower()
    cmd = ALIASES.get(cmd,cmd)
    text = (parsed[2] or '').strip()
    if cmd == 'ping':
        return send(peer,'SPLIX MANAGER на связи!')
    if cmd == 'help':
        return send(peer,HELP)
    if cmd == 'setup':
        check_owner(peer,actor)
        return send(peer,'Беседа зарегистрирована. Владелец подтверждён через VK.')
    if cmd == 'nick':
        uid,_ = target(message,text)
        n = row('SELECT nick FROM nicks WHERE peer=? AND uid=?',(peer,uid))
        return send(peer,n['nick'] if n else 'Ник не установлен.')
    check_owner(peer,actor)
    chat = row('SELECT * FROM chats WHERE peer=?',(peer,))
    if cmd == 'createunity':
        if chat['network']:
            raise ValueError('Сначала выйдите из текущего объединения.')
        if not 1 <= len(text) <= 60:
            raise ValueError('Укажите название длиной 1–60 символов.')
        nid = secrets.token_hex(6)
        with connect() as c:
            c.execute('INSERT INTO networks VALUES(?,?,?)',(nid,text,actor))
            c.execute('UPDATE chats SET network=? WHERE peer=?',(nid,peer))
        return send(peer,f'Объединение «{text}» создано. ID: {nid}\nВ другой беседе: !связать {nid}')
    if cmd == 'addunity':
        if chat['network']:
            raise ValueError('Беседа уже связана. Используйте !отвязать.')
        n = row('SELECT * FROM networks WHERE id=?',(text,))
        if not n:
            raise ValueError('Объединение не найдено. Нужен ID из !создатьсвязь.')
        write('INSERT OR REPLACE INTO requests VALUES(?,?,?)',(text,peer,actor))
        return send(peer,f'Заявка отправлена. Создатель объединения должен выполнить !принять {peer} в своей связанной беседе.')
    if cmd == 'requests':
        n = network_for(peer,actor)
        req = rows('SELECT peer,applicant FROM requests WHERE network=?',(n['id'],))
        return send(peer,'Заявки:\n'+'\n'.join(f"{r['peer']} — владелец {r['applicant']}" for r in req) if req else 'Заявок нет.')
    if cmd == 'accept':
        n = network_for(peer,actor)
        target_peer = int(text)
        req = row('SELECT * FROM requests WHERE network=? AND peer=?',(n['id'],target_peer))
        if not req:
            raise ValueError('Нет заявки от этой беседы.')
        if owner(target_peer) != req['applicant']:
            raise ValueError('Владелец беседы изменился. Нужна новая заявка.')
        with connect() as c:
            current = c.execute('SELECT network FROM chats WHERE peer=?',(target_peer,)).fetchone()
            if current['network']:
                raise ValueError('Беседа уже подключена к объединению.')
            c.execute('UPDATE chats SET network=? WHERE peer=?',(n['id'],target_peer))
            c.execute('DELETE FROM requests WHERE peer=?',(target_peer,))
        send(peer,'Беседа подключена.')
        return send(target_peer,f"Подключение к «{n['name']}» подтверждено. Создатель объединения {n['owner']} может выполнять глобальные команды. !отвязать — выход.")
    if cmd == 'unity':
        if not chat['network']:
            return send(peer,'Беседа не связана.')
        n = row('SELECT * FROM networks WHERE id=?',(chat['network'],))
        linked = rows('SELECT peer FROM chats WHERE network=?',(n['id'],))
        return send(peer,f"{n['name']} · {n['id']}\nСоздатель: {n['owner']}\nБеседы:\n"+'\n'.join(str(c['peer']) for c in linked))
    if cmd == 'removeunity':
        write('UPDATE chats SET network=NULL WHERE peer=?',(peer,))
        write('DELETE FROM requests WHERE peer=?',(peer,))
        return send(peer,'Беседа отключена. Ранее выданные баны и ники сохранены.')
    if cmd == 'banlist':
        bans = rows('SELECT * FROM bans WHERE peer=?',(peer,))
        return send(peer,'Баны:\n'+'\n'.join(f"{b['uid']}: {b['reason']}" for b in bans if active_ban(peer,b['uid'])))
    global_actions = {'gban':'ban','gunban':'unban','gkick':'kick','gsnick':'snick','grnick':'rnick'}
    if cmd in global_actions or cmd == 'gzov':
        n = network_for(peer,actor)
        targets = rows('SELECT peer,owner FROM chats WHERE network=?',(n['id'],))
        if cmd == 'gzov':
            if not text:
                raise ValueError('Укажите текст оповещения.')
        else:
            uid,rest = target(message,text)
        report = []
        for dest in targets:
            p = dest['peer']
            try:
                # Смена владельца автоматически приостанавливает доверие объединению.
                if owner(p) != dest['owner']:
                    raise ValueError('владелец сменился; переподключите беседу')
                if cmd == 'gzov':
                    send(p,f"SPLIX MANAGER · {n['name']}\n{text}")
                    result = 'отправлено'
                else:
                    result = operate(global_actions[cmd],p,uid,rest,actor)
                report.append(f'{p}: {result}')
            except Exception as e:
                report.append(f'{p}: ошибка — {e}')
            time.sleep(0.4)
        return send(peer,'Результат по беседам:\n'+'\n'.join(report))
    if cmd in ('ban','unban','kick','snick','rnick'):
        uid,rest = target(message,text)
        return send(peer,operate(cmd,peer,uid,rest,actor))
    send(peer,'Неизвестная команда. !команды')


def handle(message):
    peer = int(message.get('peer_id',0))
    actor = int(message.get('from_id',0))
    if peer < 2000000000 or actor <= 0 or message.get('out'):
        return
    action = message.get('action') or {}
    if action.get('type') in ('chat_invite_user','chat_invite_user_by_link'):
        uid = int(action.get('member_id') or actor)
        if active_ban(peer,uid):
            try:
                protected(peer,uid,0)
                kick(peer,uid)
            except Exception:
                log.exception('Не удалось исключить забаненного участника')
    # Проверка до разбора команд: префикс ! не позволяет обойти бан.
    if active_ban(peer,actor):
        try:
            protected(peer,actor,0)
            delete_message(message)
            kick(peer,actor)
            return
        except ValueError:
            pass  # Владельца/администратора VK нельзя блокировать ботом.
        except Exception:
            log.exception('Не удалось применить бан')
            return
    try:
        command(message)
    except (ValueError, vk_api.exceptions.ApiError) as e:
        send(peer,f'Не выполнено: {e}')


def main():
    global vk
    if not TOKEN:
        raise SystemExit('Укажите VK_TOKEN в .env. VK_GROUP_ID не нужен.')
    init_db()
    session = vk_api.VkApi(token=TOKEN, api_version='5.199')
    vk = session.get_api()
    result = vk.groups.getById()
    groups = result.get('groups',[]) if isinstance(result,dict) else result
    if not groups:
        raise SystemExit('Не удалось определить сообщество по токену.')
    group_id = int(groups[0]['id'])
    log.info('SPLIX MANAGER: сообщество %s',group_id)
    while True:
        try:
            longpoll = VkBotLongPoll(session,group_id)
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    try:
                        handle(event.object['message'])
                    except Exception:
                        log.exception('Ошибка обработки события')
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception('Long Poll: повтор подключения через 5 секунд')
            time.sleep(5)



# ========== SPLIX MANAGER: дополнительные модули ==========
import json
from pathlib import Path

# Токен можно хранить как в .env, так и в VK_TOKEN.env рядом со скриптом.
load_dotenv(Path(__file__).with_name('VK_TOKEN.env'))
load_dotenv(Path(__file__).with_name('.env'))
TOKEN = os.getenv('VK_TOKEN', '').strip()
_base_init = init_db
_base_command = command
_base_handle = handle
_base_protected = protected

def init_db():
    _base_init()
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sx_roles(peer INTEGER, level INTEGER, name TEXT,
          PRIMARY KEY(peer,level), UNIQUE(peer,name));
        CREATE TABLE IF NOT EXISTS sx_users(peer INTEGER, uid INTEGER, level INTEGER DEFAULT 0,
          count INTEGER DEFAULT 0, last REAL DEFAULT 0, muted REAL DEFAULT 0,
          immunity INTEGER DEFAULT 0, nomention INTEGER DEFAULT 0,
          PRIMARY KEY(peer,uid));
        CREATE TABLE IF NOT EXISTS sx_settings(peer INTEGER, key TEXT, value TEXT,
          PRIMARY KEY(peer,key));
        CREATE TABLE IF NOT EXISTS sx_warns(id INTEGER PRIMARY KEY, peer INTEGER,
          uid INTEGER, actor INTEGER, reason TEXT, date REAL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sx_reports(id INTEGER PRIMARY KEY, peer INTEGER,
          uid INTEGER, text TEXT, date REAL, status TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS sx_money(peer INTEGER, uid INTEGER, balance INTEGER DEFAULT 0,
          bonus REAL DEFAULT 0, PRIMARY KEY(peer,uid));
        """)

def sx_user(p,u):
    write('INSERT OR IGNORE INTO sx_users(peer,uid) VALUES(?,?)',(p,u))
    return row('SELECT * FROM sx_users WHERE peer=? AND uid=?',(p,u))

def sx_setting(p,k,default=''):
    r = row('SELECT value FROM sx_settings WHERE peer=? AND key=?',(p,k))
    return r['value'] if r else default

def sx_set(p,k,v):
    write('INSERT OR REPLACE INTO sx_settings VALUES(?,?,?)',(p,k,str(v)))

def sx_level(p,u):
    if owner(p) == u:
        return 100
    return sx_user(p,u)['level']

def sx_need(p,u,level):
    if sx_level(p,u) < level:
        raise ValueError('Недостаточно прав.')

def protected(p,u,a):
    _base_protected(p,u,a)
    if a and sx_level(p,a) <= sx_level(p,u):
        raise ValueError('Нельзя воздействовать на равную или старшую роль.')
    if sx_user(p,u)['immunity']:
        raise ValueError('У пользователя иммунитет. Сначала снимите его.')

def sx_roles(p):
    for level,name in [(0,'Пользователь'),(20,'ГС ГОСС/ОПГ'),(40,'ЗГА'),
                       (60,'Специальный Администратор'),(80,'Команда Проекта'),(100,'Владелец')]:
        write('INSERT OR IGNORE INTO sx_roles VALUES(?,?,?)',(p,level,name))

def sx_target(m,t):
    return target(m,t)

SX_ALIASES = {
 'status':'ping','статус':'ping','test':'ping','тест':'ping','cmds':'help','помощь':'help',
 'start':'setup','правила':'rules','попытка':'try','рандом':'roll',
 'стата':'stats','статистика':'stats','statistic':'stats','чатинфо':'chatinfo','очате':'chatinfo','инфо':'chatinfo',
 'админы':'admins','staff':'admins','онлайн':'online','репорт':'report',
 'мут':'mute','заткнуть':'mute','затычка':'mute','унмут':'unmute','разоткнуть':'unmute',
 'пред':'warn','предупреждение':'warn','варн':'warn','снятьпред':'unwarn','унварн':'unwarn',
 'предупреждения':'warns','getwarns':'warns','getwarn':'warns','историяварнов':'warnhistory',
 'преды':'warnlist','warnmans':'warnlist','гетбан':'getban','baninfo':'getban',
 'gnick':'nick','getnick':'nick','гник':'nick','setnick':'snick','removenick':'rnick',
 'нлист':'nlist','nicklist':'nlist','nicknames':'nlist','безников':'nonames',
 'checknick':'checknicks','syncnicks':'checknicks','syncnick':'checknicks',
 'понику':'getbynick','неупоминать':'nomention','nomentions':'nomention',
 'упоминать':'mention','вызов':'zov','зов':'zov',
 'роль':'role','giverole':'role','setrole':'role','роли':'roles','rolelist':'roles','listrole':'roles',
 'снятьроль':'removerole','снять':'removerole','rr':'removerole',
 'помощник':'helper','хелпер':'helper','addhelper':'helper',
 'модер':'moder','модератор':'moder','админ':'admin','addadmin':'admin',
 'spec':'chief','glav':'chief','gladmin':'chief','glavadmin':'chief','гладмин':'chief','глав':'chief',
 'deleterole':'delrole','drole':'delrole','тишина':'silence','установитьправила':'setrules','srules':'setrules',
 'удалить':'delete','del':'delete','закрепить':'pin','открепить':'unpin',
 'gamemode':'gm','гм':'gm','неуязвимость':'gm','иммунитет':'gm','гмс':'gms','gamemodes':'gms',
 'профиль':'profile','бонус':'bonus','перевод':'transfer','топ':'top','казино':'casino',
 'уведы':'mentions','синхронизация':'sync','вайп':'wipe','владелец':'owner',
 'unblock':'unban','унблок':'unban','блок':'ban','исключить':'kick',
 'gblock':'gban','gunblock':'gunban','gsetnick':'gsnick','gremovenick':'grnick',
 'гзов':'gzov','runity':'removeunity'}
ALIASES.update(SX_ALIASES)

SX_HELP = """

Дополнение SPLIX MANAGER:
!roles; !role ID приоритет; !removerole ID
!helper / !moder / !admin / !chief ID
!newrole приоритет название; !delrole приоритет
!mute ID минуты [причина]; !unmute ID
!warn ID причина; !unwarn ID; !warns ID; !warnhistory ID; !warnlist
!stats [ID]; !chatinfo; !admins; !online
!rules; !setrules текст; !welcome текст; !settings; !silence
!nlist; !nonames; !checknicks; !getbynick часть; !getban ID
!report текст; !getreport; !reports (руководители)
!zov текст; !nomention; !mention
!gm ID (переключить); !gms
!pin / !delete (ответом); !unpin
!games; !profile; !bonus; !transfer ID сумма; !casino ставка; !top
!try действие; !roll
!sync; !wipe warns/bans/roles/nicks ПОДТВЕРЖДАЮ

Игровые деньги виртуальные, покупки/вывода нет.
Мут — удаление новых сообщений, не запрет отправки средствами VK.
Глобальные команды из исходника сохранены; доступны создателю объединения.
Не реализованы: VIP, страны, браки, миграция чужих ботов, настройка прав
editcmd/geditcmd, массовый inactive/clear, глобальные роли, дата регистрации,
просмотр чужих стикеров. Сексуальные RP-команды не добавлены.
"""
HELP += SX_HELP

SX_LEVELS = {'mute':20,'unmute':20,'warn':20,'unwarn':20,'warns':20,'warnhistory':20,
 'warnlist':20,'stats':20,'chatinfo':20,'nlist':20,'nonames':20,'checknicks':20,
 'getban':20,'getbynick':40,'role':40,'removerole':40,'helper':40,'moder':60,
 'admin':80,'chief':100,'newrole':80,'delrole':80,'silence':40,'setrules':80,
 'welcome':80,'settings':100,'delete':60,'pin':80,'unpin':80,'gm':60,'gms':60,
 'games':100,'reports':20,'zov':20,'sync':100,'wipe':100,'mentions':100,'owner':100}

def sx_amount(s):
    s=s.lower().replace('к','k')
    m=re.fullmatch(r'(\d+)(k{0,2})',s)
    if not m:
        raise ValueError('Сумма: целое число, 5к или 5кк.')
    amount=int(m[1])*1000**len(m[2])
    if not 1 <= amount <= 10**12:
        raise ValueError('Сумма вне допустимого диапазона.')
    return amount

def sx_game(p,a,cmd,t,m):
    if sx_setting(p,'games','0')!='1':
        raise ValueError('Игры отключены. Владелец может включить !games.')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT OR IGNORE INTO sx_money(peer,uid) VALUES(?,?)',(p,a))
        account=c.execute('SELECT * FROM sx_money WHERE peer=? AND uid=?',(p,a)).fetchone()
        if cmd=='profile':
            result=f"Баланс: {account['balance']} виртуальных монет."
        elif cmd=='bonus':
            remaining=86400-(time.time()-account['bonus'])
            if remaining>0:
                raise ValueError(f'Бонус через {int(remaining//60)+1} мин.')
            c.execute('UPDATE sx_money SET balance=balance+1000,bonus=? WHERE peer=? AND uid=?',(time.time(),p,a))
            result='Начислено 1000 виртуальных монет.'
        elif cmd=='top':
            leaders=c.execute('SELECT uid,balance FROM sx_money WHERE peer=? ORDER BY balance DESC LIMIT 20',(p,)).fetchall()
            result='Топ:\n'+'\n'.join(f"{r['uid']}: {r['balance']}" for r in leaders)
        else:
            if cmd=='transfer':
                uid,rest=target(m,t)
                if uid==a:
                    raise ValueError('Нельзя перевести себе.')
                stake=sx_amount(rest)
            else:
                stake=sx_amount(t)
            if account['balance']<stake:
                raise ValueError('Недостаточно монет.')
            c.execute('UPDATE sx_money SET balance=balance-? WHERE peer=? AND uid=?',(stake,p,a))
            if cmd=='transfer':
                c.execute('INSERT OR IGNORE INTO sx_money(peer,uid) VALUES(?,?)',(p,uid))
                c.execute('UPDATE sx_money SET balance=balance+? WHERE peer=? AND uid=?',(stake,p,uid))
                result=f'Переведено {stake} монет пользователю {uid}.'
            else:
                won=secrets.randbelow(100)<18
                if won:
                    c.execute('UPDATE sx_money SET balance=balance+? WHERE peer=? AND uid=?',(stake*5,p,a))
                result=f'Ставка списана. Выплата: {stake*5 if won else 0}. Шанс выигрыша 18%, выплата x5.'
    return send(p,result)

def command(m):
    p,a=m['peer_id'],m['from_id']
    parsed=re.match(r'^[!/]\s*(\S+)(?:\s+(.*))?$',m.get('text','').strip(),re.S)
    if not parsed:
        return
    cmd=ALIASES.get(parsed[1].lower(),parsed[1].lower())
    t=(parsed[2] or '').strip()
    sx_roles(p)
    sx_user(p,a)
    if cmd in SX_LEVELS:
        sx_need(p,a,SX_LEVELS[cmd])
    if cmd in ('profile','bonus','transfer','casino','top'):
        return sx_game(p,a,cmd,t,m)
    if cmd=='try':
        return send(p,f"{a} пытается {t or 'совершить действие'}: {'удачно' if secrets.randbelow(2) else 'неудачно'}.")
    if cmd=='roll':
        return send(p,str(secrets.randbelow(100)+1))
    if cmd=='owner':
        return send(p,f'Владелец определяется через VK: {owner(p)}. Передача владельца через бота отключена.')
    if cmd=='rules':
        return send(p,sx_setting(p,'rules','Правила не установлены.'))
    if cmd in ('setrules','welcome'):
        if cmd=='setrules' and not t:
            raise ValueError('Укажите текст правил.')
        sx_set(p,'rules' if cmd=='setrules' else 'welcome',t)
        return send(p,'Сохранено.')
    if cmd in ('games','silence','mentions'):
        value='0' if sx_setting(p,cmd,'0')=='1' else '1'
        sx_set(p,cmd,value)
        return send(p,f'{cmd}: {value}.')
    if cmd=='settings':
        return send(p,'\n'.join(f'{k}: {sx_setting(p,k,"0")}' for k in ('games','silence','mentions'))+'\nНастройка: !games, !silence, !welcome текст, !setrules текст. Рассылки обновлений не подключены.')
    if cmd=='chatinfo':
        return send(p,f'Peer ID: {p}\nВладелец: {owner(p)}\nУчастников: {len(members(p))}')
    if cmd=='roles':
        return send(p,'\n'.join(f"{r['level']}: {r['name']}" for r in rows('SELECT * FROM sx_roles WHERE peer=? ORDER BY level DESC',(p,))))
    if cmd in ('newrole','delrole'):
        parts=t.split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            raise ValueError('Нужен числовой приоритет.')
        level=int(parts[0])
        if level in (0,20,40,60,80,100) or not 0<level<sx_level(p,a):
            raise ValueError('Базовые роли неизменны. Новая роль должна быть ниже вашей.')
        if cmd=='newrole':
            if len(parts)<2 or len(parts[1])>50:
                raise ValueError('Название: 1–50 символов.')
            with connect() as c:
                c.execute('INSERT INTO sx_roles VALUES(?,?,?) ON CONFLICT(peer,level) DO UPDATE SET name=excluded.name',(p,level,parts[1]))
        else:
            with connect() as c:
                c.execute('DELETE FROM sx_roles WHERE peer=? AND level=?',(p,level))
                c.execute('UPDATE sx_users SET level=0 WHERE peer=? AND level=?',(p,level))
        return send(p,'Роли обновлены.')
    if cmd in ('role','removerole','helper','moder','admin','chief'):
        uid,rest=target(m,t)
        if uid==a or sx_level(p,uid)>=sx_level(p,a):
            raise ValueError('Нельзя изменить свою или старшую роль.')
        fixed={'removerole':0,'helper':20,'moder':40,'admin':60,'chief':80}
        if cmd in fixed:
            level=fixed[cmd]
        else:
            r=row('SELECT level FROM sx_roles WHERE peer=? AND (name=? OR level=?)',(p,rest,int(rest) if rest.isdigit() else -1))
            if not r:
                raise ValueError('Роль не найдена.')
            level=r['level']
        if level>=sx_level(p,a):
            raise ValueError('Выдаваемая роль должна быть ниже вашей.')
        sx_user(p,uid)
        write('UPDATE sx_users SET level=? WHERE peer=? AND uid=?',(level,p,uid))
        return send(p,f'{uid}: приоритет {level}.')
    if cmd=='admins':
        return send(p,f'Владелец VK: {owner(p)}\n'+'\n'.join(f"{r['uid']}: {r['level']}" for r in rows('SELECT uid,level FROM sx_users WHERE peer=? AND level>0 ORDER BY level DESC',(p,))))
    if cmd in ('mute','unmute','warn','unwarn','gm'):
        uid,rest=target(m,t)
        if cmd=='gm':
            if uid==a or sx_level(p,uid)>=sx_level(p,a):
                raise ValueError('Нельзя изменить иммунитет своей или старшей роли.')
            value=1-sx_user(p,uid)['immunity']
            write('UPDATE sx_users SET immunity=? WHERE peer=? AND uid=?',(value,p,uid))
            return send(p,f'Иммунитет {uid}: {value}.')
        protected(p,uid,a)
        sx_user(p,uid)
        if cmd=='mute':
            parts=rest.split(maxsplit=1)
            if not parts or not parts[0].isdigit() or not 1<=int(parts[0])<=525600:
                raise ValueError('Укажите длительность 1–525600 минут.')
            write('UPDATE sx_users SET muted=? WHERE peer=? AND uid=?',(time.time()+int(parts[0])*60,p,uid))
        elif cmd=='unmute':
            write('UPDATE sx_users SET muted=0 WHERE peer=? AND uid=?',(p,uid))
        elif cmd=='warn':
            write('INSERT INTO sx_warns(peer,uid,actor,reason,date) VALUES(?,?,?,?,?)',(p,uid,a,rest or 'Без причины',time.time()))
        else:
            write('UPDATE sx_warns SET active=0 WHERE id=(SELECT id FROM sx_warns WHERE peer=? AND uid=? AND active=1 ORDER BY id DESC LIMIT 1)',(p,uid))
        return send(p,f'{cmd}: выполнено для {uid}.')
    if cmd in ('warns','warnhistory','stats','getban'):
        uid,_=target(m,t) if t or m.get('reply_message') else (a,'')
        if cmd=='stats':
            u=sx_user(p,uid)
            return send(p,f"ID: {uid}\nПриоритет: {sx_level(p,uid)}\nСообщений с момента установки дополнения: {u['count']}")
        if cmd=='getban':
            b=active_ban(p,uid)
            return send(p,str(dict(b)) if b else 'Бана нет.')
        items=rows('SELECT * FROM sx_warns WHERE peer=? AND uid=? ORDER BY id DESC LIMIT 50',(p,uid))
        return send(p,'\n'.join(f"#{r['id']}: {r['reason']} (активно: {r['active']})" for r in items if cmd=='warnhistory' or r['active']) or 'Предупреждений нет.')
    if cmd in ('warnlist','gms'):
        items=rows('SELECT uid,COUNT(*) AS n FROM sx_warns WHERE peer=? AND active=1 GROUP BY uid',(p,)) if cmd=='warnlist' else rows('SELECT uid,immunity AS n FROM sx_users WHERE peer=? AND immunity=1',(p,))
        return send(p,'\n'.join(f"{r['uid']}: {r['n']}" for r in items) or 'Список пуст.')
    if cmd in ('nlist','getbynick','nonames','checknicks','sync'):
        items=rows('SELECT * FROM nicks WHERE peer=?',(p,))
        if cmd in ('checknicks','sync','nonames'):
            ids={int(x['member_id']) for x in members(p) if int(x['member_id'])>0}
            if cmd=='nonames':
                named={x['uid'] for x in items}
                return send(p,'Без ников: '+', '.join(map(str,sorted(ids-named))))
            for r in items:
                if r['uid'] not in ids:
                    write('DELETE FROM nicks WHERE peer=? AND uid=?',(p,r['uid']))
            if cmd=='sync':
                for r in rows('SELECT uid FROM sx_users WHERE peer=?',(p,)):
                    if r['uid'] not in ids:
                        write('UPDATE sx_users SET level=0 WHERE peer=? AND uid=?',(p,r['uid']))
            return send(p,'Ники синхронизированы. При sync сняты роли вышедших. Баны сохранены.')
        return send(p,'\n'.join(f"{r['uid']}: {r['nick']}" for r in items if cmd=='nlist' or t.lower() in r['nick'].lower()) or 'Нет совпадений.')
    if cmd in ('nomention','mention'):
        write('UPDATE sx_users SET nomention=? WHERE peer=? AND uid=?',(int(cmd=='nomention'),p,a))
        return send(p,'Предпочтение упоминаний сохранено.')
    if cmd in ('online','zov'):
        ids=[int(x['member_id']) for x in members(p) if int(x['member_id'])>0]
        if cmd=='online':
            online=[]
            for start in range(0,len(ids),500):
                online.extend(u['id'] for u in vk.users.get(user_ids=','.join(map(str,ids[start:start+500])),fields='online') if u.get('online'))
            return send(p,'Онлайн по данным VK: '+', '.join(map(str,online)))
        last=float(sx_setting(p,'last_zov','0'))
        if time.time()-last<300:
            raise ValueError('Общий вызов доступен раз в 5 минут.')
        sx_set(p,'last_zov',time.time())
        selected=[u for u in ids if not sx_user(p,u)['nomention']]
        for start in range(0,len(selected),30):
            mentions=' '.join(f'[id{u}|Участник]' for u in selected[start:start+30])
            vk.messages.send(peer_id=p,random_id=secrets.randbelow(2147483646)+1,message=(t[:500]+'\n'+mentions),disable_mentions=0)
            time.sleep(.4)
        return
    if cmd in ('report','getreport','reports'):
        if cmd=='report':
            if not t or len(t)>3000:
                raise ValueError('Текст репорта: 1–3000 символов.')
            with connect() as c:
                cur=c.execute('INSERT INTO sx_reports(peer,uid,text,date) VALUES(?,?,?,?)',(p,a,t,time.time()))
                result=f'Репорт #{cur.lastrowid} сохранён. Руководители: !reports.'
        else:
            items=rows('SELECT * FROM sx_reports WHERE peer=? ORDER BY id DESC LIMIT 20',(p,)) if cmd=='reports' else rows('SELECT * FROM sx_reports WHERE peer=? AND uid=? ORDER BY id DESC LIMIT 5',(p,a))
            result='\n'.join(f"#{r['id']} ({r['status']}): {r['text']}" for r in items) or 'Репортов нет.'
        return send(p,result)
    if cmd in ('delete','pin','unpin'):
        if cmd=='unpin':
            vk.messages.unpin(peer_id=p)
        else:
            reply=m.get('reply_message') or {}
            cmid=reply.get('conversation_message_id')
            if not cmid or (reply.get('peer_id') and reply['peer_id']!=p):
                raise ValueError('Ответьте на сообщение из этой беседы.')
            if cmd=='pin':
                vk.messages.pin(peer_id=p,conversation_message_id=cmid)
            else:
                vk.messages.delete(peer_id=p,cmids=str(cmid),delete_for_all=1)
        return send(p,'Запрос выполнен VK.')
    if cmd=='wipe':
        parts=t.split()
        if len(parts)!=2 or parts[1]!='ПОДТВЕРЖДАЮ' or parts[0] not in ('warns','bans','roles','nicks'):
            raise ValueError('!wipe warns/bans/roles/nicks ПОДТВЕРЖДАЮ. Сделайте резервную копию.')
        if parts[0]=='roles':
            write('UPDATE sx_users SET level=0 WHERE peer=?',(p,))
        else:
            table={'warns':'sx_warns','bans':'bans','nicks':'nicks'}[parts[0]]
            write(f'DELETE FROM {table} WHERE peer=?',(p,))
        return send(p,'Список очищен.')
    local={'ban':40,'unban':40,'kick':20,'snick':20,'rnick':20,'banlist':40}
    if cmd in local:
        sx_need(p,a,local[cmd])
        if cmd=='banlist':
            items=rows('SELECT * FROM bans WHERE peer=?',(p,))
            return send(p,'\n'.join(f"{r['uid']}: {r['reason']}" for r in items if active_ban(p,r['uid'])) or 'Банов нет.')
        uid,rest=target(m,t)
        return send(p,operate(cmd,p,uid,rest,a))
    # Исходные объединения, заявки, глобальные команды и setup.
    _base_command(m)

def handle(m):
    p=int(m.get('peer_id',0)); a=int(m.get('from_id',0))
    if p<2000000000 or a<=0 or m.get('out'):
        return
    sx_user(p,a)
    write('UPDATE sx_users SET count=count+1,last=? WHERE peer=? AND uid=?',(time.time(),p,a))
    try:
        # Бан проверяется исходным обработчиком; мут/тишина до команд.
        u=sx_user(p,a)
        if (u['muted']>time.time() or (sx_setting(p,'silence','0')=='1' and sx_level(p,a)==0)) and owner(p)!=a:
            delete_message(m)
            return
        action=m.get('action') or {}
        if action.get('type') in ('chat_invite_user','chat_invite_user_by_link'):
            uid=int(action.get('member_id') or a)
            welcome=sx_setting(p,'welcome','')
            if uid>0 and welcome and not active_ban(p,uid):
                send(p,welcome.replace('{id}',str(uid)))
        _base_handle(m)
    except (ValueError,vk_api.exceptions.ApiError,sqlite3.IntegrityError) as e:
        send(p,f'Не выполнено: {e}')
# ========== конец дополнения ==========




# SPLIX_BASE_ROLE_RENAME_V1
_role_edit_original_command = command

def command(message):
    parsed = re.match(r'^[!/]\s*(\S+)(?:\s+(.*))?$',
                      message.get('text', '').strip(), re.S)
    if not parsed:
        return _role_edit_original_command(message)
    raw = parsed[1].lower()
    cmd = ALIASES.get(raw, raw)
    text = (parsed[2] or '').strip()
    parts = text.split(maxsplit=1)
    basic = {0, 20, 40, 60, 80, 100}
    rename = raw in ('renamerole', 'переименоватьроль')
    basic_newrole = (cmd == 'newrole' and parts and
                     parts[0].isdigit() and int(parts[0]) in basic)
    if not (rename or basic_newrole):
        return _role_edit_original_command(message)
    peer, actor = message['peer_id'], message['from_id']
    check_owner(peer, actor)
    if len(parts) != 2 or not parts[0].isdigit():
        raise ValueError('Использование: !renamerole [приоритет] [новое название]')
    level = int(parts[0])
    name = parts[1].strip()
    if not 1 <= len(name) <= 50 or any(ord(ch) < 32 for ch in name):
        raise ValueError('Название: 1–50 символов, без переносов строк.')
    sx_roles(peer)
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute(
            'SELECT name FROM sx_roles WHERE peer=? AND level=?',
            (peer, level)).fetchone()
        if not existing:
            raise ValueError('Такой роли нет. Сначала создайте её через !newrole.')
        occupied = c.execute(
            'SELECT level FROM sx_roles WHERE peer=? AND name=? AND level<>?',
            (peer, name, level)).fetchone()
        if occupied:
            raise ValueError('Это название уже используется другой ролью.')
        c.execute('UPDATE sx_roles SET name=? WHERE peer=? AND level=?',
                  (name, peer, level))
    send(peer, f'Роль {level} теперь называется «{name}». Приоритет и права сохранены.')

HELP += '\nПереименование ролей (владелец VK): !renamerole 20 Новое название.\nБазовые роли также можно переименовать через !newrole 20 Новое название.'

if __name__ == "__main__":
    main()
