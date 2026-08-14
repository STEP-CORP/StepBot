# Mini App: контракт cabinet API

Мини-аппа — чистый фронтенд: все данные она читает и пишет только через cabinet API (`/api/cabinet/*`), реализованный в `src/web/routes/cabinet.py` поверх тех же сервисов, что и бот. Контракт полезен, если вы пишете собственный фронтенд кабинета или интегрируетесь с базой снаружи.

Соглашения по данным:

- деньги — целые **minor units** (копейки/центы; Stars — без дробной части);
- объёмы — **байты**;
- время — **ISO-8601 UTC**.

Base URL — тот же origin, что и мини-аппа. Если API хостится отдельно, клиент переопределяет его через `window.__CABINET_API_BASE__` — тогда CORS должен разрешать этот origin (`WEB__CORS_ORIGINS`).

## Аутентификация

Каждый запрос несёт креденшл Telegram Mini Apps:

```http
Authorization: tma <initData>
```

Сервер валидирует `initData` (HMAC на токене бота) и резолвит пользователя по верифицированному telegram id — ту же строку `users`, что ведёт бот. Если пользователя ещё нет, он создаётся на месте (первый контакт через мини-аппу порождает то же событие регистрации, что и `/start` в боте). Заблокированный пользователь получает `403`.

Те же эндпоинты принимают и `Authorization: Bearer <JWT>` — так авторизуется веб-кабинет (вход по e-mail/OAuth), эндпоинты общие для обеих поверхностей.

::: info Mock-режим
Вне Telegram (или с `?mock=1`) клиент мини-аппы не ходит в API вообще — отдаёт демо-данные из `mock.js`. Это режим превью тем в кабинете владельца.
:::

## Перечень эндпоинтов

| Метод и путь | Auth | Назначение |
|---|---|---|
| `GET /api/cabinet/me` | tma | Профиль + текущая подписка + конфиг оформления |
| `GET /api/cabinet/plans` | tma | Каталог тарифов с персональными ценами |
| `GET /api/cabinet/constructor` | tma | Прайс конструктора (периоды + пакеты трафика) |
| `GET /api/cabinet/referral` | tma | Реферальный код, ссылка, статистика |
| `GET /api/cabinet/payments` | tma | Последние 20 транзакций пользователя |
| `GET /api/cabinet/connection` | tma | Ссылка подписки + deep-links для подключения |
| `GET /api/cabinet/traffic` | tma | Текущий расход + серия по дням для графика |
| `GET /api/cabinet/devices` | tma | HWID-устройства подписки (из панели) |
| `DELETE /api/cabinet/devices/{hwid}` | tma | Отвязать устройство |
| `GET /api/cabinet/support` · `POST /api/cabinet/support` | tma | Чат поддержки (история / новое сообщение) |
| `POST /api/cabinet/purchase` | tma | Покупка/продление тарифа |
| `POST /api/cabinet/topup` | tma | Пополнение баланса (Stars / онлайн-шлюз) |
| `POST /api/cabinet/promocode` | tma | Активация промокода |
| `POST /api/cabinet/trial` | tma | Активация пробного периода |
| `POST /api/cabinet/subscription/reset-devices` (алиас `reset-link`) | tma | Ротация ссылки подписки + сброс сессий |
| `GET /api/cabinet/linked` | tma/JWT | Способы входа аккаунта: почта, Telegram, OAuth-привязки |
| `POST /api/cabinet/link/email` → `…/confirm` | tma/JWT | Привязка почты+пароля: код письмом → подтверждение |
| `POST /api/cabinet/link/telegram` | JWT | Одноразовая deep-link-ссылка в бота для слияния аккаунтов |
| `DELETE /api/cabinet/link/oauth/{provider}` | tma/JWT | Отвязать ВК/Яндекс/Google (последний способ входа не отвязывается) |
| `GET /api/cabinet/config` | нет | Публичный конфиг темы (шелл красится до проверки initData) |
| `GET /api/cabinet/public/plans` | нет | Тарифы для гостя (только при `WEB_CABINET_ENABLED`) |
| `GET /api/cabinet/public/landing` | нет | Данные публичного сайта на `/` |

Записи идемпотентны на сервере и следуют правилам базы: panel-first dual-write, фулфилмент платежей — через вебхук и очередь задач (см. [Платежи](/payments/)).

## Чтение

### GET /me

Аккаунт, конфиг оформления и текущая подписка одним запросом.

```jsonc
{
  "user": {
    "id": 4210, "first_name": "Иван", "username": "ivan_petrov",
    "language": "ru", "currency": "RUB",
    "balance_minor": 15000,
    "referral_code": "AB12CD",
    "personal_discount_pct": 10,
    "is_trial_available": false        // TRIAL_ENABLED && триал не использован
  },
  "app": {                             // оформление и фичи, заданные владельцем
    "template": "minimal",             // тема a..h
    "title": "My VPN", "greeting": "Привет!",
    "accent_color": "#7C5CFF",
    "bot_username": "my_bot",
    "mtproto_proxy": null,             // ссылка t.me/proxy?… или null
    "ui": { },                         // кастомизация: секции, кнопки, блоки
    "payment_methods": [ { "id": "yookassa", "label": "Карта / СБП" } ],
    "balance_enabled": true,
    "hide_subscription_link": false,
    "show_traffic_usage": true,
    "sales_mode": "plans",             // plans | constructor
    "min_deposit_minor": 5000,         // MIN_DEPOSIT_AMOUNT — минимум для POST /topup
    "max_deposit_minor": 100000000     // фиксированный потолок (не настраивается) — максимум для POST /topup
  },
  "subscription": {                    // null, если подписки не было
    "status": "active",                // trial|active|limited|expired|disabled|pending
    "is_trial": false,
    "plan_name": "Premium",            // из plan_snapshot
    "start_at": "2026-06-10T09:00:00Z", "expire_at": "2026-08-01T09:00:00Z",
    "device_limit": 5,
    "traffic": { "used_bytes": 32212254720, "limit_bytes": 107374182400, "unlimited": false },
    "subscription_url": "https://…",   // null при HIDE_SUBSCRIPTION_LINK
    "crypto_link": "happ://add/…",
    "autopay_enabled": true
  }
}
```

### GET /plans

Каталог покупаемых тарифов. Для авторизованного пользователя `price_minor` каждой длительности — **финальная котировка** (персональная скидка + промо-группа + активная распродажа, кап 100%): цена на витрине равна цене списания, клиент не должен применять скидки сам. `base_price_minor` — прайсовая цена для зачёркивания, `price_stars` — цена в Stars по курсу `STARS_RATE_RUB`.

```jsonc
{
  "currency": "RUB",
  "items": [ {
    "id": 42, "public_code": "premium", "name": "Premium",
    "description": "5 устройств · для всей семьи",
    "traffic_limit_bytes": 107374182400,  // 0/null — безлимит
    "device_limit": 5,
    "is_current": true,                   // тариф активной подписки пользователя
    "durations": [ { "days": 30, "months": 1, "price_minor": 19900,
                     "base_price_minor": 19900, "price_stars": 100 } ]
  } ]
}
```

### GET /referral

```jsonc
{
  "code": "AB12CD",
  "link": "https://t.me/YourVPNBot?start=ref_AB12CD",
  "bonus_days": 7,             // REFERRAL_BONUS_DAYS
  "commission_percent": 25,    // REFERRAL_PERCENT
  "invited_count": 7,
  "earnings_minor": 45000      // сумма по леджеру referral_earnings
}
```

Подробнее о механике — [Реферальная программа](/bot/referral).

### GET /connection

Данные шага 2 вкладки «Подключение»: персональная ссылка и deep-links. `404` — если активной подписки нет.

```jsonc
{
  "subscription_url": "https://…/s/AB12CD",  // null при HIDE_SUBSCRIPTION_LINK
  "expires_at": "2026-08-01T09:00:00Z",
  "deep_links": {
    "happ": "happ://add/https://…",          // crypto_link панели, если есть
    "v2raytun": "v2raytun://import/https://…",
    "hiddify": "hiddify://import/https://…",
    "streisand": "streisand://import/https://…"
  },
  "hide_link": false
}
```

### GET /devices

HWID-устройства, привязанные к panel-юзеру текущей подписки (запрашиваются у панели; `502` — панель недоступна).

```jsonc
{ "items": [ { "hwid": "a1b2…", "platform": "iOS", "model": "iPhone 15", "created_at": "…" } ],
  "device_limit": 5 }
```

`DELETE /devices/{hwid}` отвязывает устройство → `{ "ok": true }`. Как это соотносится с лимитами устройств — см. [Устройства](/bot/devices).

## Запись

### POST /purchase

Режим тарифов: `{ "plan_id": 42, "days": 30, "method": "balance" }` — тариф можно указать и как `public_code`. Режим конструктора: `{ "period_id": 1, "pack_id": 2, "method": "stars" }` — сервер сам собирает цену из выбранных строк. `method` — `balance`, `stars` или тип онлайн-шлюза из `me.app.payment_methods` (например `yookassa`).

Создаётся транзакция с замороженными `plan_snapshot` + `pricing`; ответ зависит от метода:

```jsonc
{ "ok": true, "paid_with": "balance" }                        // списано с баланса
{ "ok": true, "paid_with": "free" }                           // скидка 100% — уже зачислено
{ "ok": true, "invoice_link": "https://t.me/$abc" }           // Stars → openInvoice()
{ "ok": true, "redirect_url": "https://yoomoney.ru/pay/…" }   // онлайн-шлюз → страница оплаты
```

Онлайн-оплату завершает вебхук провайдера через стандартный идемпотентный пайплайн — эндпоинт сам ничего не фулфиллит. Ошибки: `402` — недостаточно баланса, `409` — уже обработано, `502` — провайдер/панель/Stars-инвойс недоступны (для Stars уже созданная `PENDING`-транзакция переводится в `CANCELED`, а не остаётся висеть).

### POST /topup

Пополнение баланса: `{ "amount_minor": 50000, "method": "stars" }`. `method` — `stars` или тип онлайн-шлюза из `me.app.payment_methods` (например `yookassa` или `platega@sbp`); **`"balance"` не принимается** — баланс нельзя пополнить с самого себя.

Создаётся транзакция типа `DEPOSIT` (не `SUBSCRIPTION_PAYMENT` — это не покупка тарифа); ответ зависит от метода:

```jsonc
{ "ok": true, "invoice_link": "https://t.me/$abc" }           // Stars → openInvoice()
{ "ok": true, "redirect_url": "https://yoomoney.ru/pay/…" }   // онлайн-шлюз → страница оплаты
```

Зачисление на баланс происходит по тому же идемпотентному пайплайну, что и оплату тарифа (вебхук / `successful_payment` → `PaymentService`), эндпоинт сам ничего не зачисляет. Сумма проверяется на сервере с обеих сторон: не ниже `MIN_DEPOSIT_AMOUNT` (`me.app.min_deposit_minor`) и не выше фиксированного потолка `me.app.max_deposit_minor`. `method` также сверяется с тем, что реально отдаёт `me.app.payment_methods` — присланный код вне этого списка (включая `manual`/`telegram_stars`, которые туда никогда не попадают) отклоняется. Ошибки: `400` — сумма ниже минимума или выше максимума, метод `balance`, неизвестный/не входящий в список метод, кошелёк выключен (`BALANCE_ENABLED=false`) или способ оплаты не включён, `502` — провайдер/Stars-инвойс недоступны (транзакция при этом переводится в `CANCELED`, а не остаётся вечным `PENDING`).

### POST /promocode

`{ "code": "WELCOME2026" }` →

```jsonc
{ "ok": true,  "reward_type": "balance", "message": null }        // balance|days|subscription|discount
{ "ok": false, "reward_type": null,      "message": "…причина…" }
```

Типы наград и их настройка — [Промокоды](/bot/promo).

### POST /trial

Без тела. Проверяет `TRIAL_ENABLED` и доступность триала у пользователя (с блокировкой строки — два параллельных вызова не пройдут оба), при платном триале (`TRIAL_PRICE` > 0) сначала списывает баланс, затем выдаёт пробную подписку с параметрами `TRIAL_DURATION_DAYS` / `TRIAL_TRAFFIC_GB` / `TRIAL_DEVICE_LIMIT`.

```jsonc
{ "ok": true, "days": 3, "subscription": { /* как в /me */ } }
```

`400` — триал выключен или уже использован, `402` — не хватает баланса на платный триал.

### POST /subscription/reset-devices

Без тела. Ротирует ссылку подписки на панели (revoke) и сбрасывает подключения — старая ссылка перестаёт работать сразу, клиент должен показать и переимпортировать новую.

```jsonc
{ "ok": true, "subscription_url": "https://…", "deep_links": { /* happ, v2raytun, … */ } }
```

::: warning Rate limit
Не чаще одного раза в 10 минут на пользователя — повторный вызов раньше вернёт `429`.
:::

## Ошибки

Не-2xx ответы несут HTTP-статус и строку причины. Типовые статусы: `401` — нет/невалидный `initData`, `403` — пользователь заблокирован, `404` — нет активной подписки, `402` — недостаточно баланса, `409` — повторная обработка, `429` — rate limit, `502` — панель или платёжный провайдер временно недоступны. Клиент мини-аппы показывает их тостом.
