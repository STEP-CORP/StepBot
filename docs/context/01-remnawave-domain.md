# 01 — Remnawave: доменные понятия

Remnawave — самохостовая панель управления VPN. База общается с ней по HTTP API
(исходящие вызовы) и принимает от неё вебхуки (входящие события). Это критическая
внешняя система — большинство «дорогих» граблей проекта (см. `07-gotchas.md`) про неё.

## Объекты панели

- **User (panel-side)** — на панелях **2.x идентифицируется UUID**, на **3.0+ — числовым `id`**
  (uuid из user-роутов и вебхуков 3.0 выпилен полностью). Локально подписка хранит оба ключа
  (`remnawave_uuid` / `remnawave_id`), наружу отдаётся `Subscription.panel_ref`, а клиент сам
  выбирает ключ под пробнутую версию; имеет `telegram_id`, `email`, `username`,
  `description`, лимит трафика (в **байтах**), стратегию трафика, дату истечения (`expire`),
  лимит устройств HWID, subscription URL, членство в squad'ах.
  **«Безлимит»** моделируется как `expire` = год 2099 / ~3650 дней и трафик `0`.
- **Internal squads** = продаваемые серверы/локации. У каждого squad'а UUID.
  Наша таблица `server_squads` зеркалит их (синк на старте) с локальной ценой, капасити,
  страной, гейтингом по промо-группе.
- **External squads** = группировки маршрутизации/выхода; подписка может нести один `external_squad` UUID.
- **Nodes** = реальные VPN-серверы; панель отдаёт статус/метрики/рестарт ноды.
  События up/down ноды приходят вебхуком.
- **HWID devices** = отпечатки устройств пользователя; панель энфорсит лимит устройств.
  **Именно поэтому** мульти-тариф требует **один panel-user на подписку** — иначе два тарифа
  схлопываются в одного user'а и делят/дерутся за наименьший HWID-лимит.
- **Subscription URL** = ссылка, которую импортирует клиент (Happ, v2rayNG и т.п.);
  revoke ссылки её ротирует (старая перестаёт работать → надо уведомить пользователя новой).

## Аутентификация к API панели (топ-1 источник падений)

Панель бывает развёрнута по-разному, поэтому auth — «движущаяся мишень». Поддерживаем:

- `Authorization: Bearer <token>` **и/или** `X-Api-Key: <token>` — **по умолчанию шлём оба**
  (разным деплоям нужно разное).
- Опционально за **Caddy** (secret-key), за **Cloudflare Access**
  (`CF-Access-Client-Id` / `CF-Access-Client-Secret`), или за nginx/Caddy secret-key **cookie**.
- **basic** auth (user/password) как отдельная стратегия.

### Local vs external — инъекция заголовков

Когда панель достаётся по «голому» http внутри docker-сети (bare host / docker service name /
приватный IP), панель со своей trust-логикой отвергнет запрос, если **не** прислать:

```
X-Forwarded-Proto: https
X-Forwarded-For: 127.0.0.1
X-Real-IP: 127.0.0.1
Host: localhost
```

плюс отключить TLS-verify. Это инкапсулируется в `ConnectionProfile`, вычисляемом один раз
при создании клиента. Внешний домен → `https`, verify on.

## Версии и capability-map

Не пиннить версию жёстко. На старте `try_connection()` пробит `get_metadata`, проверяет
версию `>= 2.8.0` и строит набор capability-флагов. Пример дрейфа:
**2.8.0 удалил `POST /system/tools/happ/encrypt`** — бизнес-код проверяет capability, а не версию.

Версия читается из `GET /api/system/metadata` (есть и на 2.8+, и на 3.x; `/health` на 3.0
версию больше не отдаёт) с фолбэком на `/health` для старых панелей. Мажор `>= 3` даёт
capability **`v3_api`** и переключает клиент на контракт 3.0.

## Remnawave 3.0 — двойной контракт клиента

3.0 сломала REST API (панель при апгрейде требует env
`I_UNDERSTAND_REST_API_BREAKING_CHANGES=true`). Клиент (`client.py`) держит обе таблицы
роутов и выбирает по пробнутому мажору; остальному коду версия панели не видна. Маппинг:

| Было (2.x) | Стало (3.0) |
|---|---|
| `GET/DELETE /api/users/{uuid}`, actions по uuid | те же роуты, но **числовой `{id}`** |
| `PATCH /api/users` c `uuid` в теле | `PATCH /api/users` c `id` (числом) в теле |
| `GET /api/users/by-telegram-id/{tg}` | `GET /api/users/stream?telegramId=...` (keyset) |
| — | `POST /api/users/resolve` — id по `username`/`shortUuid` |
| `POST /api/users/{uuid}/actions/drop-connections` | `POST /api/connections/drop` (body: userIds) |
| `POST /api/ip-control/fetch-users-ips/{node}` + `/result/{job}` | `POST/GET /api/connections/by-node/{...}` |
| hwid: `GET devices/{uuid}`, delete body `userUuid` | `GET devices/{id}`, delete body `userId` (число) |
| `activeInternalSquads`: список uuid-строк | список **объектов** `{uuid, name}` |
| squad `membersCount` сверху | внутри `info.membersCount` |

Ключевые решения:
- Подписка, созданная на 2.x, знает только uuid. На 3.0 клиент **сам ре-резолвит** числовой id
  через `POST /users/resolve` (сначала `username = sub_<short_id>`, затем `shortUuid`) и кэширует;
  вебхук/ресинк добивают `remnawave_id` в БД. Апгрейд панели владельца → ничего руками не делать.
- `PATCH` на 3.0 отвергает `expireAt` в прошлом (400) — клиент клампит такие даты к `now+2м`
  («истечь сейчас» превращается в «истечь почти сразу»).

### 3.1 → 3.4.4: контракт тот же

Сверено по контракту бэкенда (теги `3.0.0` … `3.4.4`): роуты, методы, тела запросов и формы
ответов всех вызовов клиента (users, resolve, stream, actions, hwid, connections, metadata,
internal-squads, nodes) и конверт вебхука (`scope/event/timestamp/data/meta`, подпись
`X-Remnawave-Signature`) не менялись. Отдельной ветки под 3.4 в клиенте нет — все 3.x идут
через контракт 3.0. Что изменилось и на бота не влияет:

- node: добавлены числовой `id`, `ips[]` (3.2.2), `integrationUuids` (3.3) — нода по-прежнему
  адресуется uuid'ом;
- `tags` у internal/external squads, конфиг-профилей, шаблонов (3.4) — клиент их не читает;
- `vlessUuid` валидируется как «guid», а не строгий RFC-uuid (3.2.2) — импорт из 3x-ui со
  «странными» uuid больше не ловит 400;
- `SHORT_UUID_METHOD=nanoid|uuid|custom` (3.4): `shortUuid` может быть длиннее 16 символов.
  Наш `short_id` — `String(16)`, поэтому фолбэк «найти по shortUuid» для импортированных
  подписок работает только при дефолтном nanoid-16;
- массовое удаление юзеров (bulk delete) с 3.4 шлёт `user.deleted` на каждого — вебхук
  помечает подписки удалёнными, как и при одиночном удалении;
- 3.2.1 при апгрейде удаляет API-токены с невалидным uuid → бот получит 401, нужен новый токен.

## Вебхуки панели → бот

HMAC-валидируются секретом `REMNAWAVE__WEBHOOK_SECRET`. Типы событий:

- `user.*` — created / updated / enabled / disabled / deleted.
- `user_hwid_devices.*` — подключение устройства.
- `node.*` — up / down.
- `torrent_blocker.report` — репорт торрент-блокера.

**Грабли вебхуков:**
- `user.created` прилетает и для пользователей, которых **создавали не вы** →
  игнорировать, если не помечен `IMPORTED` (иначе двойное создание).
- **3.0: в payload юзер-событий НЕТ `uuid`** — только числовой `id` (+ `shortUuid`, `username`).
  Резолв подписки: uuid → `remnawave_id` → username `sub_<short_id>` → shortUuid (фолбэки
  работают только для payload'ов без uuid); первое совпавшее событие бэкфиллит `remnawave_id`.
- `torrent_blocker.report` спамит → дедуп через Redis-лок
  `torrent_blocker_lock:{user}:{node}:{ip}` с TTL = длительность блока.
- Мелькание ноды up/down → коалесить.

## Эндпоинты, которые реально дёргают конкуренты

Клиент базы (`src/infrastructure/remnawave/client.py`) должен покрыть как минимум:

- users: `create_user`, `update_user`, `enable_user`, `disable_user`, `delete_user`,
  `get_user_by_uuid`, `get_user_by_telegram_id`, `get_user_by_email`,
  `reset_traffic`, `revoke_subscription`.
- hwid: `get_hwid_devices`, `delete_hwid_device`, `drop_connections`.
- squads: `get_internal_squads`, `get_external_squads`.
- nodes: `get_nodes`, node actions (restart/enable/disable).
- system: `get_stats`, `get_health`, `get_metadata` (версия + capability probe).

Все методы возвращают **типизированные DTO**, не сырые dict.

## Единицы и шаблоны

- Трафик: снаружи GB → внутрь **байты**.
- Безлимит: `expire` 2099 / 3650 дней, трафик 0.
- `username`/`description` панель-user'а — шаблонятся с постоянным суффиксом `short_id`
  подписки; `username` клампится по длине.

## Реальные имена полей API (проверено на живой панели)

Сверено read-only против рабочей панели (`scripts/check_panel.py`) — используй эти имена,
у клиента `_to_panel_user` уже под них выровнен:

- **User** (`GET /api/users`): `uuid`, `shortUuid` (НЕ `shortId`), `username`, `status`,
  `expireAt`, `trafficLimitBytes`, `userTraffic` (использованный трафик; на свежих панелях это
  ОБЪЕКТ `{usedTrafficBytes, lifetimeUsedTrafficBytes, onlineAt, ...}`, числа могут приходить
  строками; на старых — плоское число; top-level `usedTrafficBytes` там нет),
  `hwidDeviceLimit`, `subscriptionUrl`, `telegramId`, `activeInternalSquads` (список),
  `externalSquadUuid` (НЕ `activeExternalSquad`), `tag`, `trafficLimitStrategy`, `email`,
  `vlessUuid`/`trojanPassword`/`ssPassword` (секреты протоколов).
- **Internal squad** (`GET /api/internal-squads` → `response.internalSquads[]`): `uuid`, `name`,
  `info`, `inbounds`, `viewPosition`.
- **Node** (`GET /api/nodes`): `uuid`, `name`, `isConnected`, `countryCode`, `address`, `port`,
  `trafficUsedBytes`, `trafficLimitBytes`, `usersOnline`, `isDisabled`, `xrayUptime`.
- **Версия**: `GET /api/system/health` и `/api/system/stats` версию **НЕ** отдают → probe
  версии не должен ронять старт (`ensure_supported` при неизвестной версии лишь предупреждает).
  Источник версии — `GET /api/system/metadata` (`response.version`, есть на 2.8+ и 3.x).
- **3.0 (проверено на ЖИВОЙ панели 3.0.0, throwaway в docker):** у юзера `id` — **число**,
  поля `uuid` нет; `activeInternalSquads` — объекты `{uuid, name}`; `userTraffic` — объект
  (как на свежих 2.x). E2E прогнан по всей поверхности клиента: probe/create/get по id/
  резолв uuid-only ref через `sub_<short>`/stream по telegramId/actions/update с клампом
  прошлого expireAt/devices/squads/nodes/delete — всё сходится. Нюансы живой 3.0:
  без `X-Forwarded-*` панель молча рвёт соединение (наш local-профиль их шлёт);
  `POST /api/connections/drop` без подключённых нод отвечает 404 «Connected nodes not found»
  (call-sites это терпят); admin-JWT для ручных curl требует заголовок
  `X-Remnawave-Client-Type: browser` (API-токена это не касается).
- **Write-путь (create/update user) НЕ проверен** — на проде не тестировали; имена input-полей
  выровнять на тестовой панели перед провижинингом.

См. дальше: `02-subscription-lifecycle.md` — как всё это склеивается в покупку/продление/синк.
