# 🔁 Webhook Relay

<p>
  <a href="https://github.com/sonoyumi/webhook-relay/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/sonoyumi/webhook-relay/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-outbox-003B57?logo=sqlite&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

**🇬🇧 [English](#en)** · **🇮🇹 [Italiano](#it)** · **🇺🇦 [Українська](#uk)** · **🇷🇺 [Русский](#ru)**

---

<a name="en"></a>

## 🇬🇧 English

A reliable middleman for webhooks. Stripe, GitHub or your website form sends events to one place; the relay checks
that they are genuine, stores them, drops duplicates and delivers each event to the systems that need it (CRM,
notifications, CI), retrying until the receiver answers. Nothing is lost when a receiver is down for an hour,
and a human can see and replay what could not be delivered.

```
Stripe ─┐                        ┌─ verify signature + timestamp ─┐
GitHub ─┼─► POST /hooks/{source} ┤                                ├─► SQLite: event + deliveries ─► 202 in ms
Form ───┘                        └─ JSON · de-duplicate by id ────┘          (outbox)
                                                                                  │ dispatcher
                                            200 ─► delivered                      ▼
     CRM / notify / CI ◄── POST, signed by us ──── 5xx/timeout ─► retry: 30 s, 1 min, 2 min … (± jitter)
                                                   4xx / attempts over ─► dead ─► admin: replay
```

### Features

- **Signature checks:** Stripe (`Stripe-Signature`, several signatures during secret rotation), GitHub
  (`X-Hub-Signature-256`), a generic HMAC scheme with timestamp, or a shared token. Timestamps outside the tolerance
  are refused: a captured request cannot be replayed later. Constant-time comparisons.
- **Outbox pattern:** receiving only writes the event and its deliveries in one transaction and answers
  `202` in milliseconds; sending happens in a separate loop. A restart or a receiver outage loses nothing.
- **De-duplication:** the sender's event id (or the body hash) is unique per source; a re-sent event answers `200`
  with `duplicate: true` and creates no new deliveries.
- **Routing** by event type with shell-style patterns (`invoice.*`), several targets per source.
- **Delivery with retries:** exponential backoff with ±20 % jitter and a ceiling, `Retry-After` respected, 4xx treated
  as final, a maximum number of attempts, then `dead`. Leases return deliveries of a crashed process to the queue.
- **Outgoing signatures:** targets receive `X-Signature` (HMAC of timestamp + body) and `X-Relay-*` headers,
  so they can trust the relay the same way the relay trusts the senders.
- **Admin API** (key in `X-Admin-Key`): stats, events with bodies, deliveries by status, replay one or all dead.
- **Configuration in TOML** (secrets only in `.env`), all errors at once; retention of delivered events.
- **CLI:** `serve`, `check`, `stats`, `replay`, and `sign`, which prints a ready signed `curl` for testing a source.

### Example

A real run: the relay, a CRM that answers 200 and a broken receiver that answers 500.

```bash
webhook-relay check
eval "$(webhook-relay sign --source stripe examples/stripe_checkout.json)"
```

```
Настройки в порядке.
POST /hooks/stripe  (stripe)
   → crm: http://127.0.0.1:8801/payments  [checkout.session.*] · подпись
POST /hooks/site-form  (hmac)
   → broken: http://127.0.0.1:8802/leads  [все события]

{"event":1,"event_id":"evt_1QdemoCheckout","type":"checkout.session.completed","duplicate":false,"deliveries":1}
{"event":1,…,"duplicate":true,"deliveries":0}                       ← the same event again
{"event":2,"event_id":"evt_1QdemoRefund","type":"charge.refunded",…,"deliveries":0}   ← no route: stored only
{"detail":"invalid signature: timestamp outside the tolerance window (replayed or clock skew)"}   ← forged

[8801] checkout.session.completed id=evt_1QdemoCheckout signature_ok=True -> 200
[8802] contatti id=f-20260930-0001 -> 500      (×3, then dead: "HTTP 500 (gave up after 3 attempts)")
```

`check` output is in Russian: "Settings are fine", "подпись" = signed route, "все события" = every event.

### Quick start

```bash
git clone https://github.com/sonoyumi/webhook-relay.git
cd webhook-relay
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp relay.example.toml relay.toml     # secrets in .env, sources and routes in relay.toml
webhook-relay check
webhook-relay serve                                          # http://127.0.0.1:8095/hooks/<source>
```

Tests: `pytest` (52 tests: every signature scheme, replay protection and secret rotation, config validation, the
outbox store with leases, backoff, jitter, Retry-After and dead letters, the HTTP API, an end-to-end receive → deliver
run, and the CLI's signed `curl` verified against the real check).

### Project structure

```
src/webhook_relay/
├── signatures.py  # verify and sign: stripe, github, hmac, token
├── config.py      # relay.toml + .env: sources, routes, settings
├── store.py       # SQLite outbox: events, deliveries, claim with lease, replay, stats
├── dispatcher.py  # delivery loop: retries, backoff, jitter, dead letters
├── api.py         # POST /hooks/{source}, admin API
└── cli.py         # webhook-relay serve | check | stats | replay | sign
```

### Author

**Vladyslav Shokun** ([@sonoyumi](https://github.com/sonoyumi)), Python developer: Telegram bots, web scraping, automation.

[![Telegram](https://img.shields.io/badge/Telegram-write%20me-2CA5E0?logo=telegram&logoColor=white)](https://t.me/sonoyumiii)
[![Email](https://img.shields.io/badge/Email-contact-EA4335?logo=gmail&logoColor=white)](mailto:sonoyumiii@gmail.com)

> 💼 Payments, forms and CRM that lose events now and then? Get in touch, I'll connect them reliably.

### License

MIT, see [LICENSE](LICENSE).

---

<a name="it"></a>

## 🇮🇹 Italiano

**[🇬🇧 English](#en)** · **🇮🇹 Italiano** · **[🇺🇦 Українська](#uk)** · **[🇷🇺 Русский](#ru)**

Un intermediario affidabile per i webhook. Stripe, GitHub o il modulo del vostro sito inviano gli eventi in un unico punto;
il relay verifica che siano autentici, li salva, scarta i duplicati e consegna ogni evento ai sistemi che ne hanno bisogno
(CRM, notifiche, CI), riprovando finché il destinatario risponde. Nulla va perso se un destinatario è fermo per un'ora,
e una persona può vedere e reinviare ciò che non è stato consegnato.

### Funzionalità

- **Verifica delle firme:** Stripe (`Stripe-Signature`, più firme durante la rotazione del segreto), GitHub
  (`X-Hub-Signature-256`), uno schema HMAC generico con timestamp o un token condiviso. I timestamp fuori tolleranza
  vengono rifiutati: una richiesta intercettata non può essere reinviata più tardi. Confronti a tempo costante.
- **Pattern outbox:** la ricezione scrive solo l'evento e le sue consegne in un'unica transazione e risponde `202` in pochi
  millisecondi; l'invio avviene in un ciclo separato. Un riavvio o un destinatario fermo non fanno perdere nulla.
- **Deduplicazione:** l'id dell'evento (o l'hash del corpo) è unico per sorgente; un evento reinviato risponde `200` con
  `duplicate: true` e non crea nuove consegne.
- **Instradamento** per tipo di evento con pattern stile shell (`invoice.*`), più destinazioni per sorgente.
- **Consegna con ritentativi:** backoff esponenziale con jitter ±20 % e un tetto, `Retry-After` rispettato, 4xx considerati
  definitivi, numero massimo di tentativi, poi `dead`. I lease riportano in coda le consegne di un processo caduto.
- **Firme in uscita:** i destinatari ricevono `X-Signature` (HMAC di timestamp + corpo) e gli header `X-Relay-*`.
- **API di amministrazione** (chiave in `X-Admin-Key`): statistiche, eventi con il corpo, consegne per stato, reinvio.
- **Configurazione in TOML** (segreti solo in `.env`), tutti gli errori insieme; conservazione degli eventi consegnati.
- **CLI:** `serve`, `check`, `stats`, `replay` e `sign`, che stampa un `curl` firmato pronto per provare una sorgente.

### Esempio

Vedi la sezione inglese: un giro reale con un CRM che risponde 200 e un destinatario rotto che risponde 500 — evento
consegnato con firma verificata, duplicato riconosciuto, evento senza route solo salvato, falso rifiutato, tre tentativi
e poi `dead`.

### Avvio rapido

```bash
git clone https://github.com/sonoyumi/webhook-relay.git
cd webhook-relay
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp relay.example.toml relay.toml     # segreti in .env, sorgenti e route in relay.toml
webhook-relay check
webhook-relay serve                                          # http://127.0.0.1:8095/hooks/<sorgente>
```

Test: `pytest` (52 test: ogni schema di firma, protezione dal replay e rotazione del segreto, validazione della
configurazione, lo store outbox con i lease, backoff, jitter, Retry-After e dead letter, l'API HTTP, un giro completo
ricezione → consegna e il `curl` firmato della CLI verificato con il vero controllo).

### Struttura del progetto

```
src/webhook_relay/
├── signatures.py  # verifica e firma: stripe, github, hmac, token
├── config.py      # relay.toml + .env: sorgenti, route, impostazioni
├── store.py       # outbox SQLite: eventi, consegne, lease, reinvio, statistiche
├── dispatcher.py  # ciclo di consegna: ritentativi, backoff, jitter, dead letter
├── api.py         # POST /hooks/{sorgente}, API di amministrazione
└── cli.py         # webhook-relay serve | check | stats | replay | sign
```

### Autore

**Vladyslav Shokun** ([@sonoyumi](https://github.com/sonoyumi)), sviluppatore Python: bot Telegram, web scraping, automazione.

[![Telegram](https://img.shields.io/badge/Telegram-write%20me-2CA5E0?logo=telegram&logoColor=white)](https://t.me/sonoyumiii)
[![Email](https://img.shields.io/badge/Email-contact-EA4335?logo=gmail&logoColor=white)](mailto:sonoyumiii@gmail.com)

> 💼 Pagamenti, moduli e CRM che ogni tanto perdono eventi? Scrivetemi, li collego in modo affidabile.

### Licenza

MIT, vedi [LICENSE](LICENSE).

---

<a name="uk"></a>

## 🇺🇦 Українська

**[🇬🇧 English](#en)** · **[🇮🇹 Italiano](#it)** · **🇺🇦 Українська** · **[🇷🇺 Русский](#ru)**

Надійний посередник для вебхуків. Stripe, GitHub або форма вашого сайту надсилають події в одне місце; ретранслятор
перевіряє, що вони справжні, зберігає їх, відкидає дублікати й доставляє кожну подію в системи, яким вона потрібна
(CRM, сповіщення, CI), повторюючи, доки одержувач не відповість. Нічого не губиться, якщо одержувач лежить годину,
а людина бачить і може повторити те, що не вдалося доставити.

### Можливості

- **Перевірка підписів:** Stripe (`Stripe-Signature`, кілька підписів під час заміни секрету), GitHub
  (`X-Hub-Signature-256`), загальна HMAC-схема з міткою часу або спільний токен. Мітки часу поза допуском
  відхиляються: перехоплений запит не можна надіслати пізніше. Порівняння за сталий час.
- **Патерн outbox:** приймання лише записує подію та її доставки однією транзакцією й відповідає `202` за мілісекунди;
  надсилання — в окремому циклі. Перезапуск чи недоступний одержувач нічого не губить.
- **Дедуплікація:** id події (або хеш тіла) унікальний для джерела; повторна подія отримує `200` з `duplicate: true`
  і не створює нових доставок.
- **Маршрутизація** за типом події з шаблонами як у shell (`invoice.*`), кілька адрес на джерело.
- **Доставка з повторами:** експоненційна затримка з ±20 % jitter і стелею, `Retry-After` враховується, 4xx — остаточні,
  максимум спроб, потім `dead`. Оренда (lease) повертає в чергу доставки процесу, що впав.
- **Вихідні підписи:** одержувачі отримують `X-Signature` (HMAC мітки часу + тіла) і заголовки `X-Relay-*`.
- **Адмін-API** (ключ у `X-Admin-Key`): статистика, події з тілом, доставки за статусом, повтор.
- **Налаштування в TOML** (секрети лише в `.env`), усі помилки разом; зберігання доставлених подій обмежене в часі.
- **CLI:** `serve`, `check`, `stats`, `replay` і `sign`, що друкує готовий підписаний `curl` для перевірки джерела.

### Приклад

Дивіться англійський розділ: реальний прогін із CRM, що відповідає 200, і зламаним одержувачем, що відповідає 500 —
доставка з перевіреним підписом, дублікат розпізнано, подія без маршруту лише збережена, підробку відхилено, три спроби
й потім `dead`.

### Швидкий старт

```bash
git clone https://github.com/sonoyumi/webhook-relay.git
cd webhook-relay
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp relay.example.toml relay.toml     # секрети в .env, джерела й маршрути в relay.toml
webhook-relay check
webhook-relay serve                                          # http://127.0.0.1:8095/hooks/<джерело>
```

Тести: `pytest` (52 тести: кожна схема підпису, захист від повторів і заміна секрету, перевірка налаштувань, сховище
outbox з орендою, затримки, jitter, Retry-After і «мертві» доставки, HTTP API, повний прогін приймання → доставка
та підписаний `curl` з CLI, перевірений справжньою перевіркою).

### Структура проєкту

```
src/webhook_relay/
├── signatures.py  # перевірка й підпис: stripe, github, hmac, token
├── config.py      # relay.toml + .env: джерела, маршрути, налаштування
├── store.py       # outbox у SQLite: події, доставки, оренда, повтор, статистика
├── dispatcher.py  # цикл доставки: повтори, затримки, jitter, «мертві»
├── api.py         # POST /hooks/{джерело}, адмін-API
└── cli.py         # webhook-relay serve | check | stats | replay | sign
```

### Автор

**Vladyslav Shokun** ([@sonoyumi](https://github.com/sonoyumi)) — Python-розробник: Telegram-боти, парсинг, автоматизація.

[![Telegram](https://img.shields.io/badge/Telegram-write%20me-2CA5E0?logo=telegram&logoColor=white)](https://t.me/sonoyumiii)
[![Email](https://img.shields.io/badge/Email-contact-EA4335?logo=gmail&logoColor=white)](mailto:sonoyumiii@gmail.com)

> 💼 Платежі, форми й CRM іноді гублять події? Напишіть мені, з'єднаю їх надійно.

### Ліцензія

MIT — див. [LICENSE](LICENSE).

---

<a name="ru"></a>

## 🇷🇺 Русский

**[🇬🇧 English](#en)** · **[🇮🇹 Italiano](#it)** · **[🇺🇦 Українська](#uk)** · **🇷🇺 Русский**

Надёжный посредник для вебхуков. Stripe, GitHub или форма вашего сайта шлют события в одно место; ретранслятор
проверяет, что они настоящие, сохраняет их, отбрасывает дубли и доставляет каждое событие в системы, которым оно нужно
(CRM, уведомления, CI), повторяя, пока получатель не ответит. Ничего не теряется, если получатель лежит час,
а человек видит и может повторить то, что доставить не удалось.

### Возможности

- **Проверка подписей:** Stripe (`Stripe-Signature`, несколько подписей при смене секрета), GitHub
  (`X-Hub-Signature-256`), общая HMAC-схема с меткой времени или общий токен. Метки времени вне допуска отклоняются:
  перехваченный запрос нельзя отправить позже. Сравнение за постоянное время.
- **Паттерн outbox:** приём только записывает событие и его доставки одной транзакцией и отвечает `202` за миллисекунды;
  отправка — в отдельном цикле. Перезапуск или недоступный получатель ничего не теряют.
- **Дедупликация:** id события (или хеш тела) уникален для источника; повторное событие получает `200` с
  `duplicate: true` и не создаёт новых доставок.
- **Маршрутизация** по типу события с шаблонами как в shell (`invoice.*`), несколько адресов на источник.
- **Доставка с повторами:** экспоненциальная задержка с ±20 % jitter и потолком, учёт `Retry-After`, 4xx — окончательные,
  максимум попыток, затем `dead`. Аренда (lease) возвращает в очередь доставки упавшего процесса.
- **Исходящие подписи:** получатели получают `X-Signature` (HMAC метки времени + тела) и заголовки `X-Relay-*`.
- **Админ-API** (ключ в `X-Admin-Key`): статистика, события с телом, доставки по статусу, повтор.
- **Настройки в TOML** (секреты только в `.env`), все ошибки сразу; срок хранения доставленных событий.
- **CLI:** `serve`, `check`, `stats`, `replay` и `sign`, который печатает готовый подписанный `curl` для проверки источника.

### Пример

Вывод настоящего прогона — в английском разделе выше: CRM отвечает 200, сломанный получатель — 500; доставка с
проверенной подписью, дубль распознан, событие без маршрута только сохранено, подделка отклонена, три попытки и `dead`.

### Быстрый старт

```bash
git clone https://github.com/sonoyumi/webhook-relay.git
cd webhook-relay
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && cp relay.example.toml relay.toml     # секреты в .env, источники и маршруты в relay.toml
webhook-relay check
webhook-relay serve                                          # http://127.0.0.1:8095/hooks/<источник>
```

Тесты: `pytest` (52 теста: каждая схема подписи, защита от повторов и смена секрета, проверка настроек, хранилище
outbox с арендой, задержки, jitter, Retry-After и «мёртвые» доставки, HTTP API, полный прогон приём → доставка
и подписанный `curl` из CLI, проверенный настоящей проверкой).

### Структура проекта

```
src/webhook_relay/
├── signatures.py  # проверка и подпись: stripe, github, hmac, token
├── config.py      # relay.toml + .env: источники, маршруты, настройки
├── store.py       # outbox в SQLite: события, доставки, аренда, повтор, статистика
├── dispatcher.py  # цикл доставки: повторы, задержки, jitter, «мёртвые»
├── api.py         # POST /hooks/{источник}, админ-API
└── cli.py         # webhook-relay serve | check | stats | replay | sign
```

### Автор

**Vladyslav Shokun** ([@sonoyumi](https://github.com/sonoyumi)) — Python-разработчик: Telegram-боты, парсинг, автоматизация.

[![Telegram](https://img.shields.io/badge/Telegram-write%20me-2CA5E0?logo=telegram&logoColor=white)](https://t.me/sonoyumiii)
[![Email](https://img.shields.io/badge/Email-contact-EA4335?logo=gmail&logoColor=white)](mailto:sonoyumiii@gmail.com)

> 💼 Платежи, формы и CRM иногда теряют события? Напишите мне, свяжу их надёжно.

### Лицензия

MIT — см. [LICENSE](LICENSE).
