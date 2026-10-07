#!/usr/bin/env python3
"""Стратегия из формы issue → strategies.json.

Принимается только то, что нельзя использовать во вред: номер AS, протокол, имя сайта
и шаги --lua-desync без путей к файлам. Одна и та же стратегия у того же провайдера —
одна запись; новые сообщения о ней от других людей считаются подтверждениями.

Запуск: intake.py <event.json> — пишет reply.md, strategies.json и выходы шага
(changed, label) в $GITHUB_OUTPUT. Без GITHUB_OUTPUT печатает их.
"""
import datetime
import hashlib
import json
import os
import re
import sys

DATA = 'strategies.json'
MAX_STEPS = 8
MAX_TARGETS = 30
MAX_ISSUES = 20

# шаг стратегии: функция и параметры через «:»; без пробелов, кавычек, «@» (блоб из файла) и «/» (пути)
STEP = re.compile(r'^--lua-desync=[a-z][a-z0-9_]{0,40}(:[A-Za-z0-9_.,=+%-]{1,200}){0,40}$')
HOST = re.compile(r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$')
LABELS = {
    'asn': 'Provider AS', 'provider': 'Provider', 'proto': 'Protocol', 'target_host': 'Site',
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
    if not m or int(m[1]) < 1 or int(m[1]) != int(m[2]):
        raise Bad('результат — «N/N»: принимаются стратегии, открывшие сайт каждый раз')
    host = (f.get('target_host') or '').strip().lower().rstrip('.')
    host = re.sub(r'^[a-z]+://', '', host).split('/')[0]
    if host and not HOST.fullmatch(host):
        raise Bad(f'имя сайта «{host[:60]}» не похоже на домен')
    tasn = num(f.get('target_asn'), 'AS сайта')
    provider = re.sub(r'[\x00-\x1f<>`]', '', f.get('provider') or '').strip()[:80]
    ver = lambda v: v.strip() if re.fullmatch(r'[0-9][0-9A-Za-z.+~-]{0,30}', (v or '').strip()) else ''
    return {'asn': asn, 'provider': provider, 'proto': proto, 'steps': steps, 'host': host, 'target_asn': tasn,
            'nfqws2': ver(f.get('nfqws2')), 'ui': ver(f.get('ui'))}


def record(data, s, login, issue, today):
    """Добавить или подтвердить. Возвращает (запись, новая?, этот человек уже присылал?, добавлен новый сайт?)."""
    key = f"{s['asn']}|{s['proto']}|{' '.join(s['steps'])}"
    sid = hashlib.sha1(key.encode()).hexdigest()[:12]
    who = hashlib.sha256(('nfqws2-strategies:' + login.lower()).encode()).hexdigest()[:12]
    item = next((x for x in data['items'] if x['id'] == sid), None)
    new = item is None
    if new:
        item = {'id': sid, 'asn': s['asn'], 'provider': s['provider'], 'proto': s['proto'], 'steps': s['steps'],
                'targets': [], 'reports': 0, 'users': [], 'issues': [], 'first': today, 'last': today, 'nfqws2': s['nfqws2']}
        data['items'].append(item)
    if s['provider'] and not item.get('provider'):
        item['provider'] = s['provider']
    target = {k: v for k, v in (('host', s['host']), ('asn', s['target_asn'])) if v}
    fresh = bool(target) and target not in item['targets']
    if fresh:
        item['targets'] = [target] + [t for t in item['targets'] if t != target and not (target.get('host') and t.get('host') == target['host'])]
        item['targets'] = item['targets'][:MAX_TARGETS]
    seen = who in item['users']
    if not seen:
        item['users'].append(who)
        item['reports'] = len(item['users'])
    item['last'] = today
    if s['nfqws2']:
        item['nfqws2'] = s['nfqws2']
    item['issues'] = ([issue] + [i for i in item['issues'] if i != issue])[:MAX_ISSUES]
    return item, new, seen, fresh


REPO = 'https://github.com/zemidala/nfqws2-strategies'
PAGE = 'STRATEGIES.md'
PROTO = {'tls': 'HTTPS (TLS)', 'http': 'HTTP'}


def short(steps):
    """Короткое имя: «hostfakesplit ×16 + multisplit»."""
    out = []
    for s in steps:
        parts = s.removeprefix('--lua-desync=').split(':')
        rep = next((p.split('=', 1)[1] for p in parts[1:] if p.startswith('repeats=')), None)
        out.append(parts[0] + (f' ×{rep}' if rep and rep != '1' else ''))
    return ' + '.join(out)


def render(data):
    """STRATEGIES.md — та же база, что strategies.json, но для чтения на GitHub."""
    groups = {}
    for x in data['items']:
        groups.setdefault(x['asn'], []).append(x)
    order = sorted(groups, key=lambda a: (-sum(x['reports'] for x in groups[a]), -len(groups[a]), a))
    name = lambda a: next((x['provider'] for x in groups[a] if x.get('provider')), '')
    total = len(data['items'])
    upd = (data.get('updated') or '—').replace('T', ' ').replace(':00Z', '').rstrip('Z')[:16] + (' UTC' if data.get('updated') else '')
    L = ['# Strategies by provider / Стратегии по провайдерам', '',
         f'Strategies: {total} · providers: {len(groups)} · updated {upd}. '
         f'Generated from [`strategies.json`](strategies.json) after every submission — do not edit by hand.', '',
         f'Стратегий: {total}, провайдеров: {len(groups)}, обновлено {upd}. Файл собирается сам из `strategies.json` после каждой заявки. '
         'Найдите свой провайдер (номер AS) — эти стратегии nfqws2-ui проверяет при подборе первыми. '
         'Кто прислал и кто подтвердил — в заявках по ссылкам.', '']
    if not total:
        L += ['_Пока пусто — станьте первым: кнопка «Поделиться» в nfqws2-ui._', '']
    for a in order:
        n = name(a)
        L.append(f'- [AS{a}{" · " + n if n else ""}](#as{a}) — {len(groups[a])}')
    for a in order:
        n = name(a)
        L += ['', f'## AS{a}', ''] + ([f'**{n}**', ''] if n else [])
        for proto in ('tls', 'http'):
            items = sorted((x for x in groups[a] if x['proto'] == proto), key=lambda x: x['last'], reverse=True)
            items.sort(key=lambda x: -x['reports'])   # больше подтверждений — выше, при равенстве — свежее
            if not items:
                continue
            L += [f'### {PROTO[proto]}', '', '| Strategy / Стратегия | Sites / Где работала | Confirmed / Подтвердили | Last / Последний раз | Issues / Заявки |',
                  '|---|---|---|---|---|']
            for x in items:
                code = '<br>'.join(f'`{s.removeprefix("--lua-desync=")}`' for s in x['steps'])
                sites = [' · '.join(filter(None, [t.get('host'), f'AS{t["asn"]}' if t.get('asn') else None])) for t in x['targets']]
                where = '<br>'.join(sites[:5]) + (f'<br>и ещё {len(sites) - 5}' if len(sites) > 5 else '') if sites else '—'
                iss = ' '.join(f'[#{i}]({REPO}/issues/{i})' for i in sorted(x['issues']))
                L.append(f'| **{short(x["steps"])}**<br>{code} | {where} | {x["reports"]} | {x["last"]} | {iss} |')
            L.append('')
    return '\n'.join(L).rstrip('\n') + '\n'


def write_page(data):
    with open(PAGE, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(render(data))


def main():
    if sys.argv[1] == '--render':
        write_page(json.load(open(DATA, encoding='utf-8')))
        return
    ev = json.load(open(sys.argv[1], encoding='utf-8'))
    issue = ev['issue']
    login = issue['user']['login']
    try:
        data = json.load(open(DATA, encoding='utf-8'))
    except FileNotFoundError:
        data = {'version': 1, 'updated': '', 'items': []}
    today = datetime.date.today().isoformat()
    changed = False
    try:
        s = check(fields(issue.get('body')))
        item, new, seen, has_target = record(data, s, login, issue['number'], today)
        if seen and not has_target:
            label = 'confirmed'
            text = (f"Эта стратегия от вас уже учтена — подтверждений: {item['reports']}.\n\n"
                    f"Already counted — confirmations: {item['reports']}.")
        else:
            label = 'accepted' if new else 'confirmed'
            changed = True
            data['updated'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            data['items'].sort(key=lambda x: (x['asn'], x['proto'], -x['reports'], x['id']))
            text = ((f"Спасибо! Стратегия добавлена для AS{s['asn']} ({s['proto']}). nfqws2-ui у абонентов этого провайдера "
                     f"будет пробовать её при подборе.\n\nThanks! Added for AS{s['asn']} ({s['proto']}).")
                    if new else
                    (f"Сайт добавлен к стратегии, подтверждений по-прежнему {item['reports']} (от вас уже было).\n\n"
                     f"Site added; confirmations stay at {item['reports']} (you were already counted).")
                    if seen else
                    (f"Спасибо! Подтверждение засчитано — теперь их {item['reports']}.\n\n"
                     f"Thanks! Confirmation counted — {item['reports']} now."))
        text += f"\n\n`{item['id']}` · [все стратегии / all strategies]({REPO}/blob/main/{PAGE}#as{s['asn']})"
    except Bad as e:
        label = 'rejected'
        text = (f"Не принято: {e}.\n\nNot accepted — the strategy must be `--lua-desync=…` steps without file paths, "
                f"result N/N, protocol tls or http. Use the «Поделиться» button in nfqws2-ui, it fills the form correctly.")
    if changed:
        with open(DATA, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write('\n')
        write_page(data)
    with open('reply.md', 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text + '\n')
    out = f"changed={'true' if changed else 'false'}\nlabel={label}\n"
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as fh:
            fh.write(out)
    else:
        print(out + text)


if __name__ == '__main__':
    main()
