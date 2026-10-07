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
  intake.py --render      — пересобрать все страницы и сводку из data/.
Возраст аккаунта автора — из переменной ACCOUNT_CREATED (ISO-дата от API GitHub).
"""
import datetime
import glob
import hashlib
import json
import os
import re
import sys

DATA_DIR = 'data'
PAGES_DIR = 'providers'
INDEX = 'index.json'
PAGE = 'STRATEGIES.md'
REPO = 'https://github.com/zemidala/nfqws2-strategies'
OWNER = 'zemidala'
MIN_ACCOUNT_DAYS = 7     # заявки от совсем новых аккаунтов не принимаем — защита от спама
MAX_STEPS = 8
MAX_TARGETS = 30
MAX_ISSUES = 20
PROTO = {'tls': 'HTTPS (TLS)', 'http': 'HTTP'}

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


def record(data, s, login, issue, today):
    """Подтверждение или «не сработала» в файле провайдера. Возвращает (запись, новая?, ничего не изменилось?)."""
    key = f"{s['asn']}|{s['proto']}|{' '.join(s['steps'])}"
    sid = hashlib.sha1(key.encode()).hexdigest()[:12]
    who = hashlib.sha256(('nfqws2-strategies:' + login.lower()).encode()).hexdigest()[:12]
    item = next((x for x in data['items'] if x['id'] == sid), None)
    new = item is None
    if new:
        if s['fails']:
            raise Bad('такой стратегии нет в базе — отметка «не сработала» ставится только стратегиям из базы')
        item = {'id': sid, 'asn': s['asn'], 'provider': s['provider'], 'proto': s['proto'], 'steps': s['steps'],
                'targets': [], 'reports': 0, 'users': [], 'fails': 0, 'fail_users': [], 'issues': [],
                'first': today, 'last': today, 'nfqws2': s['nfqws2']}
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
    if not same:
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


def data_path(asn):
    return f'{DATA_DIR}/AS{asn}.json'


def load(asn):
    try:
        return json.load(open(data_path(asn), encoding='utf-8'))
    except FileNotFoundError:
        return {'asn': asn, 'provider': '', 'updated': '', 'items': []}


def providers():
    out = []
    for f in sorted(glob.glob(f'{DATA_DIR}/AS*.json')):
        out.append(json.load(open(f, encoding='utf-8')))
    return out


def write(path, text):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def save(p):
    """Файл провайдера и его страница; пустой провайдер — оба файла удаляются. Затем сводка."""
    if not p['items']:
        for f in (data_path(p['asn']), f'{PAGES_DIR}/AS{p["asn"]}.md'):
            if os.path.exists(f):
                os.remove(f)
    else:
        p['updated'] = now()
        p['provider'] = next((x['provider'] for x in p['items'] if x.get('provider')), p.get('provider', ''))
        p['items'].sort(key=lambda x: (x['proto'], -x['reports'], x['id']))
        write(data_path(p['asn']), json.dumps(p, ensure_ascii=False, indent=1) + '\n')
        write(f'{PAGES_DIR}/AS{p["asn"]}.md', render_provider(p))
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


def render_provider(p):
    a, n = p['asn'], p['provider']
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
              '| Strategy / Стратегия | Sites / Где работала | Works / Подтвердили | Failed / Не сработала | Last confirmed / Последний раз | Issues / Заявки |',
              '|---|---|---|---|---|---|']
        for x in items:
            code = '<br>'.join(f'`{s.removeprefix("--lua-desync=")}`' for s in x['steps'])
            sites = [' · '.join(filter(None, [t.get('host'), f'AS{t["asn"]}' if t.get('asn') else None])) for t in x['targets']]
            where = '<br>'.join(sites[:5]) + (f'<br>и ещё {len(sites) - 5}' if len(sites) > 5 else '') if sites else '—'
            fails = f'{x["fails"]} ({x["last_fail"]})' if x.get('fails') else '—'
            iss = ' '.join(f'[#{i}]({REPO}/issues/{i})' for i in sorted(x['issues']))
            L.append(f'| **{short(x["steps"])}**<br>{code} | {where} | {x["reports"]} | {fails} | {x["last"]} | {iss} |')
    return '\n'.join(L) + '\n'


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
        item, new, same = record(p, s, login, issue['number'], today)
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


def main():
    if sys.argv[1] == '--render':
        for p in providers():
            write(f'{PAGES_DIR}/AS{p["asn"]}.md', render_provider(p))
        write_index()
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
