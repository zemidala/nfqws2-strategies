#!/usr/bin/env python3
"""Стратегия из формы issue → data/AS<номер>.json, страницы и сводка.

Принимается только то, что нельзя использовать во вред: номер AS, протокол, имя сайта
и шаги --lua-desync без путей к файлам. Одна и та же стратегия у того же провайдера —
одна запись; сообщения о ней от других людей — подтверждения («работает») или
отметки «не сработала». У каждого человека учитывается последнее, что он сказал.

Хранение: файл на провайдера data/AS<номер>.json (роутер качает только свой),
index.json — сводка по провайдерам, страницы для чтения — STRATEGIES.md (оглавление)
и providers/AS<номер>.md. Заявка трогает только файлы своего провайдера и сводку.

Запуск:
  intake.py <event.json>  — заявка (issue opened) или удаление (метка remove на заявке);
                            пишет reply.md, файлы базы и выходы шага (changed, label)
                            в $GITHUB_OUTPUT, без него — печатает;
  intake.py --render      — пересобрать все страницы и сводку из data/;
  intake.py --inbox <dir> — отправки с роутеров через приёмник (relay/): файлы <dir>/*.json, по одному на
                            отправку; печатает имена обработанных файлов (их удаляет run.sh).
Возраст аккаунта автора — из переменной ACCOUNT_CREATED (ISO-дата от API GitHub).

Отправки с роутеров (без GitHub): вместо логина — номер установки в виде хеша с солью приёмника («r» + 12
знаков). Новая стратегия от одного роутера ждёт в data/pending/ и в выдачу не попадает; второй роутер её
подтверждает — она переходит в data/. Лимиты в сутки: записей с одной установки и с одного адреса.
stats/<хеш>.json — счётчик для человека: сколько его стратегий в базе и сколько роутеров их подтвердили.
"""
import datetime
import glob
import hashlib
import json
import os
import re
import sys

DATA_DIR = 'data'
PENDING_DIR = 'data/pending'
STATS_DIR = 'stats'
LIMITS = 'state/limits.json'
KNOWN = 'known.txt'
LOG_DIR = 'log'
PAGES_DIR = 'providers'
INDEX = 'index.json'
PAGE = 'STRATEGIES.md'
REPO = 'https://github.com/zemidala/nfqws2-strategies'
OWNER = 'zemidala'
MIN_ACCOUNT_DAYS = 7     # заявки от совсем новых аккаунтов не принимаем — защита от спама
MAX_STEPS = 8
MAX_TARGETS = 30
MAX_ISSUES = 20
MAX_PER_ID = 30          # записей в сутки с одной установки
MAX_PER_IP = 100         # и с одного адреса (за одним адресом бывает много роутеров)
PROTO = {'tls': 'HTTPS (TLS)', 'http': 'HTTP'}
WHO = re.compile(r'^r[0-9a-f]{12}$')
NAME = re.compile(r'^[0-9A-Za-zА-Яа-яЁё _-]{1,24}$')   # подпись: без ссылок, @ и разметки

# шаг стратегии: функция и параметры через «:»; без пробелов, кавычек, «@» (блоб из файла) и «/» (пути)
STEP = re.compile(r'^--lua-desync=[a-z][a-z0-9_]{0,40}(:[A-Za-z0-9_.,=+%-]{1,200}){0,40}$')
HOST = re.compile(r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$')
LABELS = {
    'outcome': 'Outcome', 'asn': 'Provider AS', 'provider': 'Provider', 'proto': 'Protocol', 'target_host': 'Site',
    'target_asn': 'Site AS', 'strategy': 'Strategy', 'result': 'Result', 'nfqws2': 'nfqws2 version', 'ui': 'nfqws2-ui version',
}


class Bad(Exception):
    pass


def fields(body):
    """Ответы формы: «### Подпись / Перевод» и текст под ней."""
    out = {}
    parts = re.split(r'^### +(.+?)\s*$', body or '', flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].split(' / ')[0].strip()
        val = parts[i + 1].strip()
        val = re.sub(r'^```[a-z]*\n?|\n?```$', '', val).strip()
        if val == '_No response_':
            val = ''
        for key, name in LABELS.items():
            if label == name:
                out[key] = val
    return out


def num(v, what, required=False):
    v = (v or '').strip().upper().removeprefix('AS')
    if not v:
        if required:
            raise Bad(f'не указан {what}')
        return None
    if not re.fullmatch(r'\d{1,10}', v) or not 0 < int(v) < 4294967296:
        raise Bad(f'{what}: «{v[:20]}» — не номер AS')
    return int(v)


def check(f):
    fails = (f.get('outcome') or '').strip().lower() in ('fails', 'fail', 'не сработала')
    asn = num(f.get('asn'), 'AS провайдера', True)
    proto = (f.get('proto') or '').strip().lower()
    proto = {'https': 'tls', 'tls': 'tls', 'http': 'http'}.get(proto)
    if not proto:
        raise Bad('протокол — tls или http')
    steps = [s.strip() for s in (f.get('strategy') or '').replace('\r', '').split('\n') if s.strip()]
    steps = [t for s in steps for t in s.split()]
    if not steps:
        raise Bad('нет стратегии')
    if len(steps) > MAX_STEPS:
        raise Bad(f'слишком много шагов ({len(steps)}, не больше {MAX_STEPS})')
    for s in steps:
        if not STEP.fullmatch(s):
            raise Bad(f'шаг не подходит: {s[:120]} — нужен --lua-desync=функция:параметры, без путей к файлам и блобов «@файл»')
    m = re.fullmatch(r'\s*(\d{1,2})\s*/\s*(\d{1,2})\s*', f.get('result') or '')
    if not m or int(m[2]) < 1 or int(m[1]) > int(m[2]):
        raise Bad('результат — «открылся N из M», например 3/3')
    if not fails and int(m[1]) != int(m[2]):
        raise Bad('результат — «N/N»: принимаются стратегии, открывшие сайт каждый раз')
    if fails and int(m[1]) == int(m[2]):
        raise Bad('отметка «не сработала», а результат — открылся каждый раз')
    host = (f.get('target_host') or '').strip().lower().rstrip('.')
    host = re.sub(r'^[a-z]+://', '', host).split('/')[0]
    if host and not HOST.fullmatch(host):
        raise Bad(f'имя сайта «{host[:60]}» не похоже на домен')
    tasn = num(f.get('target_asn'), 'AS сайта')
    provider = re.sub(r'[\x00-\x1f<>`|]', '', f.get('provider') or '').strip()[:80]
    ver = lambda v: v.strip() if re.fullmatch(r'[0-9][0-9A-Za-z.+~-]{0,30}', (v or '').strip()) else ''
    return {'fails': fails, 'asn': asn, 'provider': provider, 'proto': proto, 'steps': steps, 'host': host, 'target_asn': tasn,
            'nfqws2': ver(f.get('nfqws2')), 'ui': ver(f.get('ui'))}


def login_hash(login):
    return hashlib.sha256(('nfqws2-strategies:' + login.lower()).encode()).hexdigest()[:12]


def strategy_id(s):
    return hashlib.sha1(f"{s['asn']}|{s['proto']}|{' '.join(s['steps'])}".encode()).hexdigest()[:12]


def record(data, s, who, issue, today, name=None):
    """Подтверждение или «не сработала» в файле провайдера. who — хеш логина GitHub или номера установки,
    issue — номер заявки (None у отправки с роутера). Возвращает (запись, новая?, ничего не изменилось?)."""
    sid = strategy_id(s)
    item = next((x for x in data['items'] if x['id'] == sid), None)
    new = item is None
    if new:
        if s['fails']:
            raise Bad('такой стратегии нет в базе — отметка «не сработала» ставится только стратегиям из базы')
        item = {'id': sid, 'asn': s['asn'], 'provider': s['provider'], 'proto': s['proto'], 'steps': s['steps'],
                'targets': [], 'reports': 0, 'users': [], 'fails': 0, 'fail_users': [], 'issues': [],
                'first': today, 'last': today, 'nfqws2': s['nfqws2'], 'by': who}
        if name:
            item['author'] = name
        data['items'].append(item)
    item.setdefault('fails', 0)
    item.setdefault('fail_users', [])
    before = json.dumps(item, sort_keys=True)
    if s['provider'] and not item.get('provider'):
        item['provider'] = s['provider']
    # у каждого человека — последнее, что он сказал: сработала или нет
    mine, other = ('fail_users', 'users') if s['fails'] else ('users', 'fail_users')
    if who not in item[mine]:
        item[mine].append(who)
    if who in item[other]:
        item[other].remove(who)
    item['reports'] = len(item['users'])
    item['fails'] = len(item['fail_users'])
    if s['fails']:
        item['last_fail'] = today
    else:
        target = {k: v for k, v in (('host', s['host']), ('asn', s['target_asn'])) if v}
        if target and target not in item['targets']:
            item['targets'] = ([target] + [t for t in item['targets'] if not (target.get('host') and t.get('host') == target['host'])])[:MAX_TARGETS]
        if s['nfqws2']:
            item['nfqws2'] = s['nfqws2']
        item['last'] = today   # повторное «работает» в другой день — тоже новость: стратегия жива
    same = json.dumps(item, sort_keys=True) == before
    if not same and issue:
        item['issues'] = ([issue] + [i for i in item['issues'] if i != issue])[:MAX_ISSUES]
    return item, new, same


def short(steps):
    """Короткое имя: «hostfakesplit ×16 + multisplit»."""
    out = []
    for s in steps:
        parts = s.removeprefix('--lua-desync=').split(':')
        rep = next((p.split('=', 1)[1] for p in parts[1:] if p.startswith('repeats=')), None)
        out.append(parts[0] + (f' ×{rep}' if rep and rep != '1' else ''))
    return ' + '.join(out)


# ----- хранение: файл на провайдера + сводка -----

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def when(t):
    return (t or '—').replace('T', ' ')[:16] + (' UTC' if t else '')


def data_path(asn, pend=False):
    return f'{PENDING_DIR if pend else DATA_DIR}/AS{asn}.json'


def load(asn, pend=False):
    try:
        return json.load(open(data_path(asn, pend), encoding='utf-8'))
    except FileNotFoundError:
        return {'asn': asn, 'provider': '', 'updated': '', 'items': []}


def save_pending(p):
    """Стратегии, ждущие второго роутера: свой файл (роутеры его не качают); страница провайдера — заново."""
    path = data_path(p['asn'], True)
    if p['items']:
        p['updated'] = now()
        write(path, json.dumps(p, ensure_ascii=False, indent=1) + '\n')
    elif os.path.exists(path):
        os.remove(path)


def providers():
    out = []
    for f in sorted(glob.glob(f'{DATA_DIR}/AS*.json')):
        out.append(json.load(open(f, encoding='utf-8')))
    return out


def write(path, text):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def save(p, index=True):
    """Файл провайдера и его страница; пустой провайдер — файл удаляется, страница остаётся, только если есть
    ждущие второго роутера. Затем сводка."""
    pend = load(p['asn'], True)['items']
    page = f'{PAGES_DIR}/AS{p["asn"]}.md'
    if not p['items']:
        if os.path.exists(data_path(p['asn'])):
            os.remove(data_path(p['asn']))
    else:
        p['updated'] = now()
        p['provider'] = next((x['provider'] for x in p['items'] if x.get('provider')), p.get('provider', ''))
        p['items'].sort(key=lambda x: (x['proto'], -x['reports'], x['id']))
        write(data_path(p['asn']), json.dumps(p, ensure_ascii=False, indent=1) + '\n')
    if p['items'] or pend:
        write(page, render_provider(p, pend))
    elif os.path.exists(page):
        os.remove(page)
    if index:
        write_index()


def write_index():
    ps = providers()
    idx = {'version': 2, 'updated': now(), 'total': sum(len(p['items']) for p in ps), 'providers': [
        {'asn': p['asn'], 'provider': p['provider'], 'count': len(p['items']), 'reports': sum(x['reports'] for x in p['items']), 'updated': p['updated']}
        for p in ps]}
    idx['providers'].sort(key=lambda x: (-x['reports'], -x['count'], x['asn']))
    write(INDEX, json.dumps(idx, ensure_ascii=False, indent=1) + '\n')
    write(PAGE, render_index(idx))


# ----- страницы для чтения на GitHub -----

def render_index(idx):
    n, total = len(idx['providers']), idx['total']
    L = ['# Strategies by provider / Стратегии по провайдерам', '',
         f'Strategies: {total} · providers: {n} · updated {when(idx["updated"])}. Find your provider (AS number) and open its page. '
         'Generated after every submission — do not edit by hand.', '',
         f'Стратегий: {total}, провайдеров: {n}, обновлено {when(idx["updated"])}. Найдите своего провайдера (номер AS) и откройте его страницу — '
         'эти стратегии nfqws2-ui проверяет при подборе первыми. Номер AS вашего провайдера показан в nfqws2-ui: «Проверка сайта → Стратегии сообщества». '
         'Файл собирается сам после каждой заявки.', '']
    if not n:
        return '\n'.join(L + ['_Пока пусто — станьте первым: кнопка «Поделиться» в nfqws2-ui._']) + '\n'
    L += ['| AS | Provider / Провайдер | Strategies / Стратегий | Confirmed / Подтверждений | Updated / Обновлено |', '|---|---|---|---|---|']
    for p in idx['providers']:
        name = p['provider'].replace('|', '/') or '—'
        L.append(f'| [AS{p["asn"]}]({PAGES_DIR}/AS{p["asn"]}.md) | {name} | {p["count"]} | {p["reports"]} | {p["updated"][:10]} |')
    return '\n'.join(L) + '\n'


def render_provider(p, pend=()):
    a, n = p['asn'], p['provider'] or next((x.get('provider') for x in pend if x.get('provider')), '')
    L = [f'# AS{a}' + (f' · {n}' if n else ''), '',
         f'[← all providers / все провайдеры](../{PAGE}) · data: [`{data_path(a)}`](../{data_path(a)}) · updated {when(p["updated"])}', '',
         'Order: confirmed minus «did not work», then newest. Who sent and who confirmed — in the linked issues.', '',
         'Порядок: «подтвердили» за вычетом «не сработала», затем свежие. «Не сработала» — сколько человек проверили стратегию у себя и она не открыла сайт; '
         'такие роутер пробует последними. Кто прислал и кто подтвердил — в заявках по ссылкам.']
    for proto in ('tls', 'http'):
        items = sorted((x for x in p['items'] if x['proto'] == proto), key=lambda x: x['last'], reverse=True)
        items.sort(key=lambda x: x.get('fails', 0) - x['reports'])
        if not items:
            continue
        L += ['', f'## {PROTO[proto]}', '',
              '| Strategy / Стратегия | Sites / Где работала | Works / Подтвердили | Failed / Не сработала | Last confirmed / Последний раз | Found by / Нашёл | Issues / Заявки |',
              '|---|---|---|---|---|---|---|']
        for x in items:
            fails = f'{x["fails"]} ({x["last_fail"]})' if x.get('fails') else '—'
            iss = ' '.join(f'[#{i}]({REPO}/issues/{i})' for i in sorted(x['issues'])) or '—'
            L.append(f'| {cell(x)} | {where(x)} | {x["reports"]} | {fails} | {x["last"]} | {x.get("author") or "—"} | {iss} |')
    if pend:
        L += ['', '## Waiting for a second router / Ждут второго подтверждения', '',
              'Sent from one router; routers start trying them once a second one confirms. / '
              'Прислал один роутер; в подбор у других они попадут, когда стратегию подтвердит второй.', '',
              '| Strategy / Стратегия | Protocol / Протокол | Sites / Где работала | Sent / Прислана | Found by / Нашёл |', '|---|---|---|---|---|']
        for x in sorted(pend, key=lambda x: x['first'], reverse=True):
            L.append(f'| {cell(x)} | {x["proto"]} | {where(x)} | {x["first"]} | {x.get("author") or "—"} |')
    return '\n'.join(L) + '\n'


def cell(x):
    return f'**{short(x["steps"])}**<br>' + '<br>'.join(f'`{s.removeprefix("--lua-desync=")}`' for s in x['steps'])


def where(x):
    sites = [' · '.join(filter(None, [t.get('host'), f'AS{t["asn"]}' if t.get('asn') else None])) for t in x['targets']]
    return '<br>'.join(sites[:5]) + (f'<br>и ещё {len(sites) - 5}' if len(sites) > 5 else '') if sites else '—'


# ----- заявка, удаление -----

def too_new(login):
    created = os.environ.get('ACCOUNT_CREATED', '')
    if login.lower() == OWNER or not created:
        return False
    try:
        t = datetime.datetime.fromisoformat(created.replace('Z', '+00:00'))
    except ValueError:
        return False
    return datetime.datetime.now(datetime.timezone.utc) - t < datetime.timedelta(days=MIN_ACCOUNT_DAYS)


def intake(issue):
    """Возвращает (изменилось?, метка, ответ)."""
    login = issue['user']['login']
    today = datetime.date.today().isoformat()
    try:
        if too_new(login):
            raise Bad(f'аккаунту GitHub меньше {MIN_ACCOUNT_DAYS} дней — это защита от спама; пришлите заявку позже или напишите в чат t.me/nfqws2_ui')
        s = check(fields(issue.get('body')))
        p = load(s['asn'])
        item, new, same = record(p, s, login_hash(login), issue['number'], today)
    except Bad as e:
        return False, 'rejected', (f"Не принято: {e}.\n\nNot accepted — the strategy must be `--lua-desync=…` steps without file paths, "
                                   f"protocol tls or http. Use the «Поделиться» button in nfqws2-ui, it fills the form correctly.")
    link = f"\n\n`{item['id']}` · [все стратегии AS{s['asn']} / all strategies]({REPO}/blob/main/{PAGES_DIR}/AS{s['asn']}.md)"
    n, f = item['reports'], item['fails']
    if same:
        return False, 'failed' if s['fails'] else 'confirmed', f"Это от вас уже учтено: подтвердили {n}, не сработала у {f}.\n\nAlready counted.{link}"
    save(p)
    if s['fails']:
        return True, 'failed', (f"Спасибо, учтено: стратегия не сработала у вас. Теперь подтвердили {n}, не сработала у {f} — "
                                f"роутеры будут пробовать её позже других.\n\nThanks, counted as not working ({n} works / {f} failed).{link}")
    if new:
        return True, 'accepted', (f"Спасибо! Стратегия добавлена для AS{s['asn']} ({s['proto']}). nfqws2-ui у абонентов этого провайдера "
                                  f"будет пробовать её при подборе.\n\nThanks! Added for AS{s['asn']} ({s['proto']}).{link}")
    return True, 'confirmed', f"Спасибо! Учтено: подтвердили {n}" + (f", не сработала у {f}" if f else '') + f".\n\nThanks! Counted: {n} works.{link}"


def remove(issue):
    """Метка remove на заявке: убрать запись, к которой она относится (ищем во всех провайдерах)."""
    gone = []
    for p in providers():
        hit = [x for x in p['items'] if issue in x['issues']]
        if hit:
            p['items'] = [x for x in p['items'] if issue not in x['issues']]
            save(p)
            gone += hit
    return gone


# ----- отправки с роутеров через приёмник -----

def known():
    try:
        return [l.strip().lower() for l in open(KNOWN, encoding='utf-8') if l.strip() and not l.startswith('#')]
    except FileNotFoundError:
        return []


def check_relay(it, names):
    """Запись отправки с роутера → то же, что check() даёт для формы. Имя сайта — только из known.txt."""
    if not isinstance(it, dict):
        raise Bad('запись не объект')
    kind = it.get('kind')
    if kind not in ('share', 'works', 'fails'):
        raise Bad('неизвестный вид записи')
    asn = num(str(it.get('asn') or ''), 'AS провайдера', True)
    proto = it.get('proto')
    if proto not in PROTO:
        raise Bad('протокол — tls или http')
    steps = it.get('steps')
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise Bad('нет стратегии или слишком много шагов')
    for s in steps:
        if not isinstance(s, str) or not STEP.fullmatch(s):
            raise Bad('шаг не подходит')
    host = str(it.get('host') or '').strip().lower().rstrip('.')
    if host and not (HOST.fullmatch(host) and any(host == k or host.endswith('.' + k) for k in names)):
        host = ''   # not a known site: the router should not have sent it, it is dropped, not stored
    net = num(str(it.get('net') or ''), 'AS сайта') if it.get('net') else None
    provider = re.sub(r'[\x00-\x1f<>`|]', '', str(it.get('provider') or '')).strip()[:80]
    ver = lambda v: v.strip() if re.fullmatch(r'[0-9][0-9A-Za-z.+~-]{0,30}', str(v or '').strip()) else ''
    return {'kind': kind, 'fails': kind == 'fails', 'asn': asn, 'provider': provider, 'proto': proto, 'steps': steps,
            'host': host, 'target_asn': net, 'nfqws2': ver(it.get('nfqws2')), 'ui': ver(it.get('ui'))}


def relay_one(s, who, name, today, act, pend):
    """Одна запись: подтверждение уже принятой, второй роутер у ждущей (переходит в выдачу), новая — в ожидание.
    act, pend — кеши файлов провайдеров. Возвращает итог для журнала."""
    a = s['asn']
    act.setdefault(a, load(a))
    pend.setdefault(a, load(a, True))
    sid = strategy_id(s)
    if any(x['id'] == sid for x in act[a]['items']) or s['fails']:
        _, new, same = record(act[a], s, who, None, today)
        return 'same' if same else ('failed' if s['fails'] else 'confirmed')
    waiting = next((x for x in pend[a]['items'] if x['id'] == sid), None)
    if waiting and who not in waiting['users']:
        record(pend[a], s, who, None, today)
        pend[a]['items'] = [x for x in pend[a]['items'] if x['id'] != sid]
        act[a]['items'].append(waiting)
        return 'promoted'
    if waiting:
        _, _, same = record(pend[a], s, who, None, today)
        return 'same' if same else 'waiting'
    record(pend[a], s, who, None, today, name)
    return 'pending'


def inbox(folder):
    """Все отправки из папки: лимиты, разбор, файлы провайдеров, журнал, сводка, счётчики. Возвращает
    обработанные файлы (и те, что не приняты: повторять их незачем)."""
    files = sorted(glob.glob(f'{folder}/*.json'))
    today = datetime.date.today().isoformat()
    try:
        lim = json.load(open(LIMITS, encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        lim = {}
    if lim.get('day') != today:
        lim = {'day': today, 'id': {}, 'ip': {}}
    names = known()
    act, pend, log = {}, {}, []
    for f in files:
        try:
            sub = json.load(open(f, encoding='utf-8'))
        except ValueError:
            continue
        who, ip = str(sub.get('who', '')), str(sub.get('ip', ''))[:12]
        name = str(sub.get('name') or '').strip()
        name = name if NAME.fullmatch(name) else None
        if not WHO.fullmatch(who):
            continue
        for it in (sub.get('items') or [])[:MAX_PER_ID]:
            row = {'t': sub.get('t') or now(), 'who': who, 'ip': ip, 'kind': it.get('kind') if isinstance(it, dict) else None}
            if lim['id'].get(who, 0) >= MAX_PER_ID or (ip and lim['ip'].get(ip, 0) >= MAX_PER_IP):
                log.append({**row, 'result': 'limit'})
                continue
            lim['id'][who] = lim['id'].get(who, 0) + 1
            if ip:
                lim['ip'][ip] = lim['ip'].get(ip, 0) + 1
            try:
                s = check_relay(it, names)
                res = relay_one(s, who, name, today, act, pend)
                log.append({**row, 'asn': s['asn'], 'id': strategy_id(s), 'result': res})
            except Bad as e:
                log.append({**row, 'result': 'rejected', 'why': str(e)})
    # only what changed is written: «updated» of an untouched provider must not move
    touched = False
    for a in set(act) | set(pend):
        pch = json.dumps(pend[a]['items'], sort_keys=True) != json.dumps(load(a, True)['items'], sort_keys=True)
        ach = json.dumps(act[a]['items'], sort_keys=True) != json.dumps(load(a)['items'], sort_keys=True)
        if pch:
            save_pending(pend[a])
        if ach or pch:
            save(act[a], index=False) if ach else write(f'{PAGES_DIR}/AS{a}.md', render_provider(act[a], pend[a]['items']))
            touched = True
    if touched:
        write_index()
        write_stats()
    write(LIMITS, json.dumps(lim, sort_keys=True) + '\n')
    if log:
        path = f'{LOG_DIR}/{today[:7]}.jsonl'
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, 'a', encoding='utf-8', newline='\n') as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in log)
    return files


def write_stats():
    """stats/<хеш>.json — для каждого, кто присылал с роутера: сколько его стратегий в выдаче и ждёт, сколько
    роутеров их подтвердили (кроме него самого), у скольких провайдеров. Роутер знает свой хеш из ответа приёмника."""
    st = {}
    for pend, ps in ((False, providers()), (True, [json.load(open(f, encoding='utf-8')) for f in glob.glob(f'{PENDING_DIR}/AS*.json')])):
        for p in ps:
            for x in p['items']:
                by = x.get('by') or ''
                if not WHO.fullmatch(by):
                    continue
                s = st.setdefault(by, {'live': 0, 'waiting': 0, 'confirmed': 0, 'asns': set()})
                s['waiting' if pend else 'live'] += 1
                s['asns'].add(p['asn'])
                if not pend:
                    s['confirmed'] += max(0, x['reports'] - (1 if by in x['users'] else 0))
    os.makedirs(STATS_DIR, exist_ok=True)
    for f in glob.glob(f'{STATS_DIR}/r*.json'):
        if os.path.basename(f)[:-5] not in st:
            os.remove(f)
    for by, s in st.items():
        out = {'live': s['live'], 'waiting': s['waiting'], 'confirmed': s['confirmed'], 'providers': len(s['asns']), 'updated': now()}
        path = f'{STATS_DIR}/{by}.json'
        try:
            old = json.load(open(path, encoding='utf-8'))
            if {k: v for k, v in old.items() if k != 'updated'} == {k: v for k, v in out.items() if k != 'updated'}:
                continue   # unchanged: no new commit just for the time
        except (FileNotFoundError, ValueError):
            pass
        write(path, json.dumps(out) + '\n')


def main():
    if sys.argv[1] == '--render':
        asns = {p['asn'] for p in providers()} | {int(os.path.basename(f)[2:-5]) for f in glob.glob(f'{PENDING_DIR}/AS*.json')}
        for a in sorted(asns):
            write(f'{PAGES_DIR}/AS{a}.md', render_provider(load(a), load(a, True)['items']))
        write_index()
        write_stats()
        return
    if sys.argv[1] == '--inbox':
        for f in inbox(sys.argv[2]):
            print(f)
        return
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    issue = ev['issue']
    if ev.get('action') == 'labeled' and (ev.get('label') or {}).get('name') == 'remove':
        gone = remove(issue['number'])
        changed, label = bool(gone), 'removed'
        text = (f"Удалено из базы: {', '.join('`' + x['id'] + '` ' + short(x['steps']) for x in gone)}.\n\nRemoved."
                if gone else 'В базе нет записи, связанной с этой заявкой.\n\nNothing to remove.')
    else:
        changed, label, text = intake(issue)
    write('reply.md', text + '\n')
    out = f"changed={'true' if changed else 'false'}\nlabel={label}\n"
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as fh:
            fh.write(out)
    else:
        print(out + text)


if __name__ == '__main__':
    main()
