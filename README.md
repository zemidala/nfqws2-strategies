# nfqws2 strategies

**[→ All strategies by provider / Все стратегии по провайдерам](STRATEGIES.md)**

Working [nfqws2](https://github.com/bol-van/zapret2) strategies shared by users of
[nfqws2-ui](https://github.com/zemidala/nfqws2-ui), grouped by the internet provider (AS number).

DPI differs between providers, so a strategy that works for someone on your provider is the best
first guess for you. nfqws2-ui downloads only your provider's file (`data/AS<number>.json`, a few KB
whatever the size of the base) and **tests the strategies on your router** before anything is applied — the same way it tests
its own strategies. Nothing from here is applied blindly.

## How to share

In nfqws2-ui, after a strategy opened a site every time:

- **Подбор стратегии** (pick) → result row → **Поделиться** (share), or
- **История подборов** (pick history) → site → **Поделиться**.

The button opens this repository's form already filled in. You check it and press *Create*.
A strategy from this list that worked for you has a **Подтвердить** (confirm) button instead —
confirmations move it up for others.

A GitHub Action checks every submission and closes the issue with a reply:

- only `--lua-desync=…` steps are accepted — no file paths, no `@file` blobs, no other options;
- the result must be *N of N* (opened the site every time);
- the same strategy for the same provider is one entry; each person counts once.

## What is published

The issue is public. It contains your provider's AS number and name, the protocol, the strategy,
and — only if you leave it — the site name and its network. Your IP address is not sent.

## Layout

- `data/AS<number>.json` — one file per provider; this is what routers download.
- `index.json` — summary: providers, number of strategies and confirmations.
- [`STRATEGIES.md`](STRATEGIES.md) — the list of providers; `providers/AS<number>.md` — a provider's strategies.
- `scripts/intake.py` — checks a submission and rebuilds the files of that provider and the summary.

All of it is written by the GitHub Action, do not edit by hand. To remove an entry, the owner puts the
`remove` label on any of its issues. Submissions from GitHub accounts younger than 7 days are declined.

## Data format

One entry in `data/AS<number>.json` → `items`:

```json
{ "id": "d20f36c7f9d9", "asn": 12389, "provider": "…", "proto": "tls",
  "steps": ["--lua-desync=hostfakesplit:tcp_md5:tcp_ts_up:repeats=16:host=ya.ru"],
  "targets": [{ "host": "king.hr", "asn": 24940 }],
  "reports": 2, "users": ["<hash>", "…"], "fails": 1, "fail_users": ["<hash>"], "last_fail": "2026-10-07", "issues": [5, 4, 1],
  "first": "2026-10-07", "last": "2026-10-07", "nfqws2": "" }
```

`users` / `fail_users` are salted hashes of GitHub logins — only to count each person once; a person's latest word
(works / did not work) is what counts.

---

## Русский

Рабочие стратегии [nfqws2](https://github.com/bol-van/zapret2), которыми делятся пользователи
[nfqws2-ui](https://github.com/zemidala/nfqws2-ui), — по провайдерам (номер AS).

DPI у провайдеров разный, поэтому стратегия, которая сработала у абонента вашего провайдера, —
лучшее, с чего начать. nfqws2-ui скачивает только файл вашего провайдера (`data/AS<номер>.json`,
несколько килобайт при любом размере базы) и **проверяет стратегии на вашем роутере**, как и свои стратегии. Вслепую отсюда ничего
не применяется.

**Как поделиться.** В nfqws2-ui, когда стратегия открыла сайт каждый раз: «Подбор стратегии» →
строка результата → «Поделиться», или «История подборов» → сайт → «Поделиться». Откроется эта
форма, уже заполненная, — проверьте и нажмите *Create*. У стратегии из этого списка, которая у
вас сработала, вместо этого кнопка «Подтвердить»: подтверждения поднимают её для других.

Каждую заявку проверяет GitHub Action: принимаются только шаги `--lua-desync=…` без путей к
файлам и блобов `@файл`, результат «N из N»; одна стратегия у одного провайдера — одна запись,
каждый человек засчитывается один раз.

**Что публикуется.** Issue публичный: номер AS и название провайдера, протокол, стратегия и — если
вы их оставите — имя сайта и его сеть. Ваш IP-адрес не отправляется.

Вопросы — в [чате nfqws2-ui в Telegram](https://t.me/nfqws2_ui).
