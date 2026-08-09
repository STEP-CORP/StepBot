# Архитектура

Слоистая архитектура в четыре кольца. Зависимости направлены строго **внутрь**: внешние кольца знают о внутренних, не наоборот. Бизнес-логика зависит только от протоколов, поэтому тестируется с фейками и не привязана к aiogram/FastAPI/SQLAlchemy.

```
web/  ─┐
        ├─► application/ ─► core/
infra/ ─┘        ▲
                 └── application зависит только от application/common (Protocol'ы)
```

## Четыре кольца

| Кольцо | Что внутри |
|---|---|
| `src/core/` | Чистые типы и утилиты без внешних побочек: `Money` (minor-units), enums, config (pydantic-settings), i18n-loader, исключения, логирование |
| `src/application/` | Сердце. `common/` — контракты (Protocol): UoW, DAO, RemnawaveClient, PaymentGateway, EventBus, Notifier, Translator. `services/` — бизнес-правила через эти контракты. `events/` — доменные события (несут `i18n_key` + kwargs, не готовый текст). `dto/` |
| `src/infrastructure/` | Конкретика: SQLAlchemy-модели/DAO/миграции, httpx-клиент панели Remnawave, платёжные шлюзы, taskiq, redis, DI-контейнер, сервисы (бэкапы, уведомления, health) |
| `src/web/` + `src/bot/` | Тонкая презентация: FastAPI (вебхуки платежей и панели, admin/cabinet API, health) и aiogram-хендлеры. Бизнес-логики не держат — зовут сервисы через DI |

Композиционный корень — `src/infrastructure/di/container.py` (`AppContainer`): строит app-синглтоны (engine, redis, клиент панели, `GatewayFactory`, event bus, сервисы) из `Settings` и выдаёт свежий `UnitOfWork` на операцию (`container.uow()`). Его используют веб, бот и taskiq-воркер — фон гоняет ту же бизнес-логику.

## Protocol-швы

Правило: внешние системы — панель Remnawave, платёжные шлюзы, шина событий, уведомления — подключаются к сервисам **только** через Protocol'ы из `application/common`. Их тестируют фейками (`FakeRemnawaveClient`, `FakePaymentGateway`) и их можно подменить, не трогая бизнес-логику.

Персистентность — сознательное исключение: проект закоммичен на Postgres/SQLAlchemy, поэтому сервисы типизируются против конкретного `UnitOfWork` и ORM-моделей (модель = доменная сущность). Тестируемость не страдает — тесты гоняют настоящий `UnitOfWork` против in-memory sqlite. Enforcement простой: `application/services` не импортирует `infrastructure`; понадобилось — значит, нужен новый протокол.

## Основные потоки

- **Покупка:** web или бот → `PurchaseService` → `PricingService` (цена) → `Transaction(PENDING)` → шлюз (инвойс) → вебхук → taskiq → panel-first provision → `Subscription` → `EventBus` (рефералка, уведомления).
- **Синк с панелью:** `sync_subscription` + reconcile-джоб + retry-queue неудавшихся panel-write.
- **Вебхук панели:** verify (HMAC) → типизированное событие → обработчик (enable/disable/expiry/hwid/node) → локальный апдейт + уведомление.

## Ключевые инварианты (ADR)

Каждое решение записано как ADR — контекст, решение, последствия. Полные тексты — в репозитории:

| ADR | Решение | Суть |
|---|---|---|
| [0001](https://github.com/STEP-CORP/StepBot/blob/main/docs/adr/0001-layered-architecture.md) | Слоистая архитектура | 4 кольца, зависимости внутрь, протоколы вместо прямых импортов. Без церемонии «один Interactor = один файл» |
| [0002](https://github.com/STEP-CORP/StepBot/blob/main/docs/adr/0002-money-minor-units.md) | Деньги — целые minor-units | Все суммы — целые копейки (`BIGINT`, суффикс `_minor`). `Decimal` (ROUND_HALF_UP) — только на границе шлюза; для крипты/Stars — толеранс сверки сумм |
| [0003](https://github.com/STEP-CORP/StepBot/blob/main/docs/adr/0003-one-panel-user-per-subscription.md) | Один panel-user на подписку | Каждая подписка = свой panel-user с постоянным `short_id` (не выводится из мутабельного id). Мульти-тариф не ломает HWID-лимиты; активность энфорсится partial-unique индексом |
| [0004](https://github.com/STEP-CORP/StepBot/blob/main/docs/adr/0004-single-payment-webhook-route.md) | Единый платёжный ABC + один вебхук-роут | `BasePaymentGateway` + `GatewayFactory` + один роут `POST /api/v1/payments/{gateway_type}`. Вебхук: verify → enqueue → 200, фулфиллит воркер. Новый провайдер = один файл + enum + seed-row |
| [0005](https://github.com/STEP-CORP/StepBot/blob/main/docs/adr/0005-panel-first-dual-write.md) | Panel-first dual-write без 2PC | Панель пишется **первой, вне DB-транзакции**, затем локальный коммит. Окно «remote orphan» закрывают retry-queue и reconcile-джоб; операции панели идемпотентны по `short_id`↔`uuid` |

К ним примыкают сквозные правила: идемпотентность вебхуков (CAS + `UNIQUE(external_id, gateway_type)` + `FOR UPDATE`), замороженные снапшоты `plan_snapshot`+`pricing` на транзакции **и** подписке (изменение тарифа задним числом не меняет уже проданное), UTC-aware даты везде, Fernet-шифрование кредов шлюзов и секретов настроек.

::: warning Инварианты — не рекомендации
PR, нарушающий любой из пунктов таблицы (фулфилмент в вебхуке, float в деньгах, panel-вызов внутри DB-транзакции, `short_id` из id), не пройдёт ревью. Если решение кажется неудобным — сначала читайте «Контекст» соответствующего ADR: каждое выбрано после реального антипримера.
:::

## Куда дальше

- Добавить платёжку — [рецепт](/dev/add-payment-gateway).
- Добавить модель БД — [рецепт](/dev/add-db-model).
- Правила PR и локальная разработка — [Как участвовать](/dev/contributing).
