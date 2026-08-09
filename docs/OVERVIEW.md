# OVERVIEW — карта проекта (точка входа)

Единый обзор всей системы, чтобы в ней ориентироваться. Для глубокого
доменного контекста — `docs/context/`; здесь — **что где лежит и как это запустить**.

---

## 1. Что это

Telegram VPN-шоп: продаёт VPN-подписки (VLESS/XTLS) и провижнит их на панели **Remnawave**.
Состоит из четырёх «лиц» на одном общем бэкенд-ядре:

| Лицо | Что | Где живёт | URL |
|---|---|---|---|
| 🤖 **Бот** | Telegram-бот (продажи/триал/баланс/промо/тикеты) | `src/bot/` | @bot_vpn4_bot |
| 📱 **Мини-аппа** | Веб-приложение внутри Telegram (для юзера) | `miniapp/app/` | `/app/` |
| 🛠 **Админка** | React-SPA для управления (15 экранов) | `admin/` | `/admin/` |
| ⚙️ **Бэкенд** | FastAPI + фоновые задачи + ядро бизнес-логики | `src/` | `/api/…` |

**Демо-стенд:** `https://testbot.tvss-911.com` (см. §6).

---

## 2. Из чего состоит (компоненты)

```
                 ┌───────────────────────── общий бэкенд (src/) ─────────────────────────┐
Telegram ──▶ Бот (aiogram, long polling)  ─┐                                              │
                                            ├─▶  AppContainer (DI) ─▶ сервисы ─▶ БД(Postgres)
Мини-аппа /app ─▶ Cabinet API (/api/cabinet)┤        │                    │        Redis
Админ-SPA /admin ─▶ Admin API (/api/admin) ─┤        │                    └─▶ Remnawave (панель)
Платёжки/панель ─▶ вебхуки (/webhook, /api/v1/payments) ┘                          (real или mock)
                                            │
                          taskiq worker + scheduler (рассылки, бэкапы, синк нод, обработка оплат)
```

- **Один бэкенд-процесс-граф** (`AppContainer`) переиспользуется в web, боте и воркере — та же
  бизнес-логика везде.
- Стек: **Python 3.12 · aiogram 3 · FastAPI · SQLAlchemy 2 (async) · Postgres · Redis · taskiq**.
  Фронты: **React+TS+Vite** (админка), **ванильный JS** (мини-аппа).

---

## 3. Карта репозитория

```
StepBot/
├── src/                          ← бэкенд (Python), 4 «кольца»
│   ├── core/                     конфиг, enums, деньги (Money), i18n, логирование
│   ├── application/              бизнес-ядро
│   │   ├── common/               Protocol'ы (контракты: панель, платежи, события, нотификатор)
│   │   ├── services/             PricingService, PurchaseService, PaymentService,
│   │   │                         SubscriptionService, ReferralService, PromoService,
│   │   │                         RemnawaveService, PanelSyncService, BotConfigService,
│   │   │                         ShopbotImportService (миграция с remnawave-shopbot)
│   │   ├── dto/  events/         объекты передачи + доменные события
│   ├── infrastructure/           адаптеры
│   │   ├── database/             models/ (29 моделей) · dao/ · uow.py · migrations/
│   │   ├── remnawave/            клиент панели (auth/retry/mapping) + webhook-verifier
│   │   ├── payments/             base ABC + factory + gateways/ (21 живых: manual, stars, yookassa, cryptobot, cryptomus, heleket, platega, robokassa, yoomoney, wata, freekassa, paypalych, cloudpayments, lava, mulenpay, kassa_ai, rollypay, riopay, severpay, aurapay, antilopay)
│   │   ├── taskiq/               broker.py + tasks.py (фоновые задачи)
│   │   ├── redis/  di/  services/  локи · AppContainer · notification/backup/health
│   ├── bot/                      ← ТЕЛЕГРАМ-БОТ
│   │   ├── main.py               точка входа (long polling)
│   │   ├── middlewares.py        ContextMiddleware (юзер + контейнер + maintenance)
│   │   ├── keyboards.py  menu_render.py  screen.py   клавиатуры + рендер + edit-or-send
│   │   └── handlers/             start · purchase · promo · withdraw · tickets · actions
│   └── web/                      ← FastAPI
│       ├── app.py                сборка приложения + монтирование /admin, /app
│       └── routes/
│           ├── admin/            17 роутеров под /api/admin (JWT-auth), в т.ч.
│           │                     migration.py (импорт shopbot) · withdrawals.py (выводы)
│           ├── cabinet.py        /api/cabinet (initData-auth) — для мини-аппы
│           ├── panel.py          /webhook/panel — вебхук Remnawave
│           ├── payments.py       /api/v1/payments/{gateway} — вебхук платежей
│           └── health.py
├── admin/                        ← АДМИН-SPA (React+TS+Vite); src/ = исходники,
│                                   dist/ = собранная (npm run build), отдаётся на /admin
├── miniapp/                      ← МИНИ-АППА
│   ├── app/                      served на /app (3 таба × 8 тем, зовёт cabinet API)  ← актуальная
│   └── templates/ shared/ mock/  ← СТАРОЕ/orphaned (не монтируется, чистить)
├── scripts/                      install.sh · update.sh · deploy.sh · smoke.py · mock_panel.py · seed_demo.py
├── docs/                         context/ (домен) · adr/ (решения) · recipes/ · deploy-vps.md · OVERVIEW.md
├── tests/                        unit + integration (69 тестов, зелёные)
├── docker/                       Dockerfile · compose.local.yml
├── locales/                      en.json · ru.json (i18n; бот пока RU-хардкод)
├── Makefile · pyproject.toml · uv.lock · alembic.ini
```

---

## 4. По поверхностям — что готово

### 🤖 Бот (`src/bot/`)
Готово: `/start` + диплинк-атрибуция (`ref_`/кампании/**`gift_<код>`** — активация подарка),
меню (конструктор из админки + дефолт), **триал**, **покупка** (NEW/RENEW; каталог тарифов ИЛИ
конструктор период+пакет по SALES_MODE), оплата **балансом**, **Telegram Stars** и
**онлайн-шлюзами** (редирект-счёт, авто-выдача по вебхуку), **пополнение баланса** (Stars),
**промокод** (в т.ч. с экрана оплаты; мгновенные награды: баланс/скидка/дни/подписка),
**рефералка** (комиссия с пополнений + «+N дней обоим» + **вывод заработка** карта/USDT/TON),
**устройства HWID** (список + отвязка), **смена тарифа** (CHANGE, автодетект + зачёт остатка),
**докупка трафика** (пакеты), **тикеты**, «моя подписка», уведомления. Выбор языка — пока нет.

### 📱 Мини-аппа (`miniapp/app/`)
3 таба (Главная/Подключение/Аккаунт), 8 тем, RU/EN. Зовёт `/api/cabinet/*` с `Authorization: tma
<initData>`. Вне Telegram — мок-фолбэк (`?mock=1&variant=a..h`). `miniapp/templates/` — старый
неиспользуемый вариант, стоит удалить.

### 🛠 Админка (`admin/` + `src/web/routes/admin/`)
JWT-логин (`ADMIN__USERNAME`/`ADMIN__PASSWORD`). 15 экранов на реальном API: Dashboard, Users,
Тарифы, Promos, Конструктор меню, Miniapp (темы), Рассылки, Smart-напоминания, Кампании, Платежи,
Тикеты, Серверы, Настройки, Maintenance. Реализованы: **миграция с remnawave-shopbot**
(users.db → probe → импорт), **выводы рефералки** (Платежи → Выводы), **масс-генерация gift-кодов**
(CSV с диплинками). Заглушки: host-действия maintenance; импорт из Postgres-ботов — только probe.

### ⚙️ Cabinet API (`src/web/routes/cabinet.py`)
`/api/cabinet/*` для мини-аппы: me, plans, constructor, purchase (баланс + Stars + онлайн-шлюзы),
promocode, trial, referral, connection (deep-links happ/v2raytun/hiddify/streisand),
devices (список/отвязка HWID). Готово.

### ⏰ Фоновые задачи (`src/infrastructure/taskiq/tasks.py`)
`process_payment` (оплата по вебхуку + уведомление, с ретраями), `reconcile_pending_payments`
(каждые 5 мин поллит шлюзы по зависшим PENDING — страховка от потерянных вебхуков),
`sync_panel_nodes` (каждые 15 мин), `device_guard_scan` (шеринг по онлайн-IP, каждые 20 мин),
`panel_watchdog` (авто-техрежим при падении панели, каждые 2 мин),
`send_smart_reminders` / `send_holiday_promos` (по расписанию MSK), `run_backup` (pg_dump),
`send_broadcast`. Нужен `BOT__TOKEN` для рассылок.

### 🔌 Панель Remnawave (`src/infrastructure/remnawave/`)
Клиент (create/update/delete/enable/disable/reset/revoke user, squads, nodes) + вебхук (применяет
`user.*` события к локальной подписке). **Переключение real ↔ mock — один env** `REMNAWAVE__BASE_URL`.
Мок: `scripts/mock_panel.py` (:3010).

### 💳 Платежи (`src/infrastructure/payments/`)
Единый ABC + фабрика + один вебхук-роут. Работают: **manual** (админ-подтверждение), **telegram_stars**
(in-bot), **yookassa** (карта/СБП, редирект + вебхук-рефетч), **cryptobot** (крипта по курсу к ₽).
Добавить провайдера = 1 файл + enum + seed-row (см. `docs/recipes/add-payment-gateway.md`).

---

## 5. Где что настраивается

- **Секреты/окружение** → `.env` (шаблон `.env.example`): токен бота, БД, Redis, панель, crypt-key,
  admin-логин. На сервере уже заполнен.
- **Рантайм-настройки** (тексты, цены Stars, триал, рефералка, суппорт-режим, min-депозит и т.д.)
  → **админ-кабинет** (bot-config в БД, hot-reload). Не в `.env`.
- **Меню бота** → конструктор в админке (или дефолт из `menu_render.py`).
- **Темы мини-аппы** → экран Miniapp в админке.

---

## 6. Как запустить

### Локально (host-режим)
```bash
cp .env.example .env         # заполнить APP__CRYPT_KEY, APP__JWT_SECRET, BOT__TOKEN,
                             # ADMIN__PASSWORD; DATABASE__HOST/REDIS__HOST=localhost;
                             # REMNAWAVE__BASE_URL=http://127.0.0.1:3010
make install
docker compose -f docker/compose.local.yml up -d postgres redis
uv run python scripts/seed_demo.py            # демо-план + шлюзы
npm ci --prefix admin && npm run build --prefix admin   # собрать SPA (для /admin)
make migrate
uv run uvicorn scripts.mock_panel:app --port 3010 &     # мок-панель
uv run uvicorn src.web.app:app --port 8080 &            # web+admin+cabinet
uv run taskiq worker src.infrastructure.taskiq.broker:broker src.infrastructure.taskiq.tasks &
uv run taskiq scheduler src.infrastructure.taskiq.broker:scheduler &
make bot                                                # бот (polling)
```
`make check` — линт+типы+тесты (гейт перед коммитом).

### VPS (systemd + nginx)
Раскладка и провижининг — `docs/deploy-vps.md` (systemd `vpnshop-*`, nginx+LE, Postgres/Redis в docker, `.env`). Обновление:
```bash
./scripts/deploy.sh user@host https://your-domain   # SPA локально → rsync → uv sync → alembic → restart → health
ssh user@host 'journalctl -u vpnshop-bot -f'         # логи бота
```
Замена мок-панели на живую Remnawave — один env (см. `docs/deploy-vps.md`).

---

## 7. Документация (куда смотреть)
- `ARCHITECTURE.md` — кольца, потоки данных, инварианты.
- `docs/context/00–08` — домен Remnawave, lifecycle подписки, платежи, рефералка, разбор конкурентов, грабли.
- `docs/adr/` — ключевые архитектурные решения.
- `docs/deploy-vps.md` — раскладка сервера + деплой.
- `docs/recipes/` — рецепты: добавить платёжку, добавить модель БД.

---

## 8. Известные проблемы / TODO
1. ~~Worker в цикле смертей~~ — **починено**: причиной был redis-py 8.x (рвёт простаивающий
   блокирующий BRPOP), а не нехватка RAM; закреплён пин `redis>=5.0,<8` в pyproject.
   1 GB RAM всё равно впритык — для боевого магазина лучше 2 GB.
2. ~~Платёжки: только manual/stars/yookassa/cryptobot~~ — **21 живой провайдер** за одним ABC
   (список в дереве выше); включаются в кабинете при наличии ключей мерчанта. Ещё 3
   (Tribute/PayPear/Overpay) — заготовки-заглушки без публичной спеки.
4. Язык/i18n в боте — RU-хардкод; `Translator` загружен, но не используется.
5. `miniapp/templates/` — устаревший неиспользуемый вариант, удалить.
6. Admin: host-действия maintenance (update/restart) — заглушки; импорт из shopbot реализован,
   из Postgres-ботов — только probe.
7. ~~CI собирает только Python (не SPA)~~ — **починено**: job `frontend` в ci.yml гоняет
   `npm ci && npm run build` (tsc + vite) для admin-SPA.

---

История в git (`STEP-CORP/StepBot`, ветка `main`).
