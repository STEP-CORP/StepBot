<div align="center">

<img src="docs/assets/vpnhub-banner.png" alt="VPN-HUB" width="640">

# VPN-HUB BOT

**Конструктор Telegram-ботов для продажи VPN на базе [Remnawave](https://remna.st)**

Принимает оплату, выдаёт подписки, управляет пользователями — а весь бот собирается
и настраивается **через веб-кабинет, без правки кода и перезапусков**.

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://postgresql.org)
[![React](https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)](docker/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

[📖 Документация](https://docs.vpn-hub.pro) · [🤖 Тестовый бот](https://t.me/bot_vpn4_bot) · [🖥 Демо-стенд](https://testbot.tvss-911.com/admin/) · [💬 Чат сообщества](https://t.me/vpnhub_community)

📗 Инструкция оператору: [docs/GO-LIVE.md](docs/GO-LIVE.md)

</div>

---

## 🧩 Что это?

**VPN-HUB BOT** — платформа из трёх частей поверх общего ядра:

- 🤖 **Telegram-бот** — продажи, триал, баланс, рефералка, тикеты. Меню бота
  (кнопки, цвета, вложенные экраны) собирается в конструкторе кабинета.
- 🖥 **Админ-кабинет** — веб-SPA на React: 14 разделов, «конструктор всего» —
  тарифы, акции, рассылки, пользователи, платёжки, кнопки бота, мини-аппа,
  серверы и 70+ параметров с **hot-reload**.
- 📱 **Mini App** — личный кабинет подписчика в Telegram: **8 визуальных тем**
  на выбор владельца, оплата Stars / с баланса / картой и криптой, подключение в 3 шага.

> Строгое ядро в 4 кольца (чистая архитектура, mypy strict, 85 тестов) +
> прагматичный набор бизнес-фич — всё управляется из кабинета.

<div align="center">
<img src="docs/assets/admin-dashboard.png" alt="Админ-кабинет — дашборд" width="920">
</div>

---

## ✨ Возможности

<table>
<tr>
<td width="50%" valign="top">

### 🤖 Бот

- 🧱 **Меню из конструктора** — кнопки, цвета (Bot API styles), custom emoji, вложенные экраны
- 🛒 Покупка: **баланс**, **Telegram Stars**, **карта/СБП/крипта** — кнопки появляются сами при включении платёжки
- 🎁 Триал с лимитами из настроек
- 🔀 **Смена тарифа** с зачётом остатка дней и **докупка гигабайт** к текущей подписке
- 👥 Рефералка: `?start=ref_…`, **+N дней обоим**, % с пополнений и **вывод заработка** (карта/USDT/TON)
- 📱 Устройства: юзер сам видит свои HWID и отвязывает лишние
- 🎁 Gift-коды: `?start=gift_…` активирует подарок в один тап
- 📢 Гейт подписки на каналы (несколько, скоуп all/trial/buy)
- 🛒 Умная корзина: не хватило на балансе → пополнил → покупка сама
- 🌍 Статус серверов юзеру (🟢/🟠/🔴), S2S-постбеки для арбитража
- 🌐 **Покупка с сайта без Telegram**: регистрация/логин по email или через
  Google/Yandex, гостевая покупка по одному email — подписка приходит письмом
- 📈 Атрибуция рекламных кампаний по deep-link
- 🎫 Тикеты: текст боту → тикет в кабинете → ответ в личку
- 🔧 Режим техработ, блокировки, мультиязычность RU/EN

</td>
<td width="50%" valign="top">

### 🖥 Кабинет

- 📊 Дашборд: выручка, подписки, онлайн, аудит-лента
- 👤 Пользователи: поиск, фильтры, drawer-карточка (баланс, продление, HWID, бан)
- 💸 Тарифы + **конструктор цен** (периоды, пакеты трафика)
- 🏷 Промокоды, промогруппы, календарь акций, **пачки gift-кодов с CSV**
- 📣 Рассылки с **живым прогрессом** и обходом лимитов Telegram
- ⏰ Умные напоминания о продлении
- 🌍 Серверы Remnawave: синк, нагрузка, «в продаже»
- 🚚 **Миграция с remnawave-shopbot**: users.db → импорт в пару кликов
- 🔄 Ночная сверка бот↔панель (лечит ручные правки), НалоGO-чеки
- 👥 **Device Guard**: детект шеринга по онлайн-IP, алерт/дроп/отключение
- 🚨 Авто-техрежим: панель упала — бот сам отвечает заглушкой, поднялась — снял
- ⚙️ 70+ настроек с **hot-reload** (секреты шифруются)

</td>
</tr>
</table>

---

## 📱 Mini App — 8 тем на выбор

Одна мини-аппа, восемь характеров: Минимал · Прайват · Бадди · Нативный ·
Терминал · Журнал · Неон · Поп. Владелец выбирает тему и акцентный цвет в кабинете —
применяется мгновенно. Светлая/тёмная — по теме Telegram.

<div align="center">
<img src="docs/assets/miniapp-minimal.png" alt="Минимал" width="23%">
<img src="docs/assets/miniapp-private.png" alt="Прайват" width="23%">
<img src="docs/assets/miniapp-buddy.png" alt="Бадди" width="23%">
<img src="docs/assets/miniapp-native.png" alt="Нативный" width="23%">
<br>
<img src="docs/assets/miniapp-terminal.png" alt="Терминал" width="23%">
<img src="docs/assets/miniapp-magazine.png" alt="Журнал" width="23%">
<img src="docs/assets/miniapp-neon.png" alt="Неон" width="23%">
<img src="docs/assets/miniapp-pop.png" alt="Поп" width="23%">
<br>
<sub>Минимал · Прайват · Бадди · Нативный · Терминал · Журнал · Неон · Поп</sub>
</div>

Внутри — три вкладки: **Главная** (статус, тарифы со скидками, оплата),
**Подключение** (стор по платформе → персональная ссылка → deep-link в
happ/v2raytun/hiddify/streisand), **Кабинет** (промокоды, история платежей, поддержка).

---

## 🧱 Кабинет изнутри

Всё, что видит юзер, собирается мышкой — и применяется сразу, без рестартов:

- **01 · Темы Mini App** — каждая карточка и телефон справа — настоящая мини-аппа
  на мок-данных, превью интерактивное.
- **02 · Конструктор кнопок бота** — дерево меню → редактор кнопки (тип, цвет,
  custom emoji) → живое превью чата. Сохранил — бот уже отвечает новым меню.
- **03 · Настройки с hot-reload** — блоки, поиск, человекочитаемые названия;
  изменения доезжают до бота и воркеров за секунды.

<div align="center">
<img src="docs/assets/admin-collage.png" alt="Кабинет: темы Mini App, конструктор кнопок, настройки" width="920">
</div>

---

## 💳 Платёжные провайдеры

| | Провайдер | Способы | Статус |
|---|---|---|---|
| ⭐ | **Telegram Stars** | XTR-инвойсы в боте и мини-аппе | ✅ работает |
| 💼 | **Manual / баланс** | начисление админом, оплата с баланса | ✅ работает |
| 🏦 | **YooKassa** | карта, СБП + **автосписания по сохранённой карте** | ✅ работает |
| 👛 | **ЮMoney (кошелёк)** | quickpay: карта / кошелёк | ✅ работает |
| 🤖 | **Robokassa** | карта, СБП, кошельки | ✅ работает |
| 🏦 | **Platega** | СБП, карта, крипта | ✅ работает |
| 🏦 | **WATA** | карта, СБП, TPay, SberPay | ✅ работает |
| 🪙 | **CryptoBot** | крипта по курсу к ₽ (Crypto Pay API) | ✅ работает |
| 🪙 | **Cryptomus** | крипта, 15+ монет | ✅ работает |
| 🪙 | **Heleket** | крипта | ✅ работает |
| 🧾 | **FreeKassa** / **KassaAI** | карта, СБП, кошельки | ✅ работают |
| 💳 | **PayPalych** / **MulenPay** | карта, СБП | ✅ работают |
| ☁️ | **CloudPayments** | карта + **API-возвраты** | ✅ работает |
| 🌋 | **Lava** / **RollyPay** | карта, СБП / + крипта | ✅ работают |
| 🐆 | **Antilopay** / **RioPay** / **SeverPay** / **AuraPay** | карта, СБП (RSA/HMAC/MD5-подпись) | ✅ работают |
| 💠 | Tribute / PayPear / Overpay | ещё 3 нишевых | 🔌 drop-in, файл на кассу |

**21 живой провайдер** за одним ABC + 3 заготовки-заглушки. Как это выглядит для
юзера: включил провайдера в кабинете (ключи шифруются,
кнопка **Тест** делает реальную пробу API) — и в боте с мини-аппой сами появляются
кнопки оплаты. Юзер жмёт → получает счёт-ссылку → платит → подписка выдаётся
**автоматически по вебхуку**. Если вебхук потерялся или панель моргнула,
фоновый **реконсилятор** каждые 5 минут сам добивает оплаченные счета —
оплата не теряется никогда.

**Возвраты** — прямо из кабинета: у YooKassa / Cryptomus / Heleket / CloudPayments
деньги уходят обратно по API провайдера, у остальных фиксируется возврат (платите
вручную), опционально отключается выданная подписка, юзер получает уведомление.

Все провайдеры живут за одним ABC и одним вебхук-роутом — новый подключается
одним файлом ([гайд](docs/recipes/add-payment-gateway.md)). У каждого —
выбор пейформ (СБП / карта / крипта), а его комиссия участвует в расчёте
**чистой прибыли** (оборот − комиссии − налог) в разделе «Платежи».

---

## 🚀 Установка одной командой

Никаких `.env` руками — скрипт спросит только токен бота (и домен, если есть),
сам поставит git и Docker, склонирует репозиторий, сгенерирует секреты,
поднимет весь стек и отдаст ссылку на кабинет. На чистом VPS (Ubuntu/Debian):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/STEP-CORP/StepBot/main/scripts/install.sh)
```

<details>
<summary>То же из клона репозитория</summary>

```bash
git clone https://github.com/STEP-CORP/StepBot.git && cd StepBot
./scripts/install.sh
```

</details>

> **Требования:** 1 vCPU / 1–2 GB RAM. На 1 GB машине установщик сам поднимает
> 2 GB swap, чтобы сборка не упала по OOM.

Через пару минут: `https://ваш-домен/admin/` (HTTPS сам, через Caddy + Let's Encrypt).
Всё остальное — тарифы, платёжки, меню бота, мини-аппа — настраивается в UI.
Нет панели Remnawave? Просто нажми Enter — поднимется встроенная мок-панель,
и стек можно пощупать целиком.

## 🔄 Обновление

```bash
cd ~/StepBot 2>/dev/null || cd ~/HUB-BOT   # папка вашей установки
./scripts/update.sh
```

Не помните, куда установлен бот? Найдите папку одной командой:

```bash
find / -name update.sh -path '*/scripts/*' -not -path '*/node_modules/*' 2>/dev/null | head
```

Скрипт **сначала снимает бэкап БД** в `backups/`, потом забирает обновления,
пересобирает образы и прогоняет миграции. Если новая версия не поднялась —
печатает готовые команды отката (прежний коммит + восстановление бэкапа),
данные не теряются никогда.

## 🚚 Переезд с другого бота

Уже продаёте через **remnawave-shopbot**? Кабинет → Обслуживание → «Миграция»:
загрузите `users.db` из папки бота — юзеры с балансами, подписки (с ключами панели —
**подписчики не заметят переезда**), история платежей и промокоды переедут автоматически.
Повторный импорт безопасен, дубликатов не будет.

<details>
<summary>Ручной запуск для разработки</summary>

```bash
cp .env.example .env        # заполни секреты (см. комментарии в файле)
make install                # uv sync --extra dev
make up                     # postgres + redis + web + worker + scheduler (docker)
make bot                    # запустить бота (long polling)
```

</details>

Админ-кабинет: `http://localhost:8080/admin/` (логин/пароль — `ADMIN__USERNAME` /
`ADMIN__PASSWORD` из `.env`; `ADMIN__DEMO_ENABLED=true` включает публичный
read-only демо-вход одной кнопкой). Мини-аппа: `http://localhost:8080/app/?mock=1`.

Нет живой панели Remnawave? В комплекте **мок-панель** с теми же эндпоинтами:

```bash
uv run uvicorn scripts.mock_panel:app --port 3010
# .env: REMNAWAVE__BASE_URL=http://127.0.0.1:3010
```

Деплой на VPS (systemd + nginx + LE): [docs/deploy-vps.md](docs/deploy-vps.md).

---

## 🏗 Стек и архитектура

**Python 3.12** async · aiogram 3 · SQLAlchemy 2.0 · Alembic · PostgreSQL · Redis ·
taskiq · FastAPI · httpx — и **React 18 + TypeScript + Vite** для кабинета.
Мини-аппа — ваниль HTML/CSS/JS без сборки.

```
web/ (FastAPI: admin API, cabinet API, вебхуки)  ─┐
bot/ (aiogram)                                    ├─►  application/ (сервисы)  ─►  core/
taskiq (рассылки, напоминания, синк, бэкапы)      │         ▲
infrastructure/ (Postgres, Redis, Remnawave, платежи) ──────┘  (только через Protocol'ы)
```

Ключевые инварианты — panel-first dual-write, деньги в minor-units, идемпотентные
вебхуки, замороженные снапшоты цен — описаны в [ARCHITECTURE.md](ARCHITECTURE.md) и
[docs/context/](docs/context/). Тесты: `make check` (lint + mypy strict + pytest).

---

## 📚 Документация

| Файл | О чём |
|---|---|
| [docs/GO-LIVE.md](docs/GO-LIVE.md) | С нуля до живого: деплой → настройка → продажи |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Переезд с shopbot / Bedolaga / RemnaShop / 3x-ui |
| [docs/TELEMETRY.md](docs/TELEMETRY.md) | Телеметрия ошибок: что шлём, как выключить, сервер приёма |
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | Карта проекта: компоненты, дерево, запуск |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Кольца, потоки данных, сущности БД |
| [docs/context/](docs/context/) | Домен Remnawave, платежи, рефералка, грабли |
| [docs/deploy-vps.md](docs/deploy-vps.md) | Деплой на VPS (systemd + nginx) |
| [miniapp/CONTRACT.md](miniapp/CONTRACT.md) | Контракт cabinet API мини-аппы |
| [docs/adr/](docs/adr/) | Архитектурные решения |

---

<div align="center">

**MIT** · сделано для тех, кто продаёт VPN, а не настраивает ботов

</div>
