# План: полноценная админка-конструктор ВНУТРИ бота (кнопки, ряды, стикеры, кастом-эмодзи, стили)

> Проект: `StepBot` (aiogram 3.29, чистая архитектура, SQLAlchemy+alembic, taskiq+redis, FastAPI веб-админка, miniapp). VPN-шоп на Remnawave.
> Цель владельца дословно: «сделать админку ещё и В БОТЕ, полноценную: настраивать кнопки, вставлять стикеры — обычные И кастомные, делать кнопки НЕСКОЛЬКО В РЯД, менять их СТИЛЬ».

---

## 1. TL;DR и главная развилка

**Что делаем:** второй фронт-редактор — прямо в чате бота (aiogram FSM) — поверх ТЕХ ЖЕ таблиц (`menu_nodes`, `broadcasts`, `bot_config`), что уже правит веб-админка. Не дублируем бизнес-логику: бот и веб редактируют один источник данных, отправка постов идёт через уже существующий движок `send_broadcast`.

**Развилка — три пути:**

| Путь | Что даёт | Чего не может | Стоимость |
|---|---|---|---|
| **A. Нативный in-bot конструктор (FSM)** | Кнопки/ряды/типы/стили/стикеры/эмодзи/посты прямо в чате. Стикеры и эмодзи ловятся «бесплатно» из присланного сообщения. Быстрые правки в одно касание. | Нет drag-drop и пиксельного WYSIWYG — раскладка рядов делается кнопками ◀▶▲▼ / «новый ряд» / слайдером «в ряд N». | Средняя |
| **B. Mini App конструктор (WebView)** | Настоящий drag-drop многорядной клавиатуры + правдивый визуальный превью с реальными цветами и темой Telegram. | Стикер из системного пикера в WebView не поймать — всё равно нужен бот-захват file_id. Тяжелее в разработке, отдельная фронт-страница. | Высокая |
| **C. Оба (гибрид)** | Нативный FSM для 90% операций + Mini App только для визуальной drag-drop раскладки и WYSIWYG. Пишут в одну модель через один сервис. | — | A потом B |

**РЕКОМЕНДАЦИЯ: путь C, но поэтапно.** Сначала полностью делаем **A** (закрывает дословную цель владельца), потом при желании добавляем **B** как визуальную надстройку поверх той же схемы (поле `row_index` уже будет готово). Mini App — не для MVP.

Почему так: 90% швов уже в коде — `style_for_hex`, `simple_keyboard(columns)` (уже умеет N-в-ряд), `WELCOME_STICKER`/`answer_sticker` (уже ловит и шлёт стикеры), `Broadcast`+`send_broadcast` (готовый движок постов), `MENU_ACTIONS`/`DEFAULT_MENU` (единый каталог действий), `MenuNodeDAO`/`uow` (запись в то же дерево, что и веб). Net-new минимален.

---

## 1-bis. Обязательные правки из адверсариального ревью (вшиты в фазы)

Вердикт ревью: **APPROVE WITH FIXES** — план технически честен и подтверждён по коду (нативный цвет кнопки = 3 пресета + дефолт в aiogram 3.29.1; кастом-эмодзи в ТЕКСТЕ кнопки невозможен; стикеры — отдельным сообщением). До ввода фаз в прод вшить:

- **[HIGH] Веб-`save_menu` тоже пишет `row_index`.** `save_menu` (`web/routes/admin/menu.py`) делает wipe+reinsert — `row_index` добавить не только в `NodeIn`/`_serialize`, но и в конструктор `MenuNode(...)` внутри цикла вставки + в payload SPA. Регресс-тест: бот собрал многорядное меню → веб GET → веб PUT без изменений → `row_index` сохранился. *(Фаза 1)*
- **[HIGH] Идемпотентность рассылки — поднять из Фазы 4 в Фазу 3.** `send_broadcast` принимает статус в `(PENDING, RUNNING)` и стартует с индекса 0 → повторный `.kiq(id)` / redelivery taskiq / двойной клик = вся аудитория получит дубль. До того как in-bot композер сделает запуск в один тап: (а) атомарный CAS `PENDING→RUNNING` (`UPDATE...WHERE status=PENDING RETURNING`), (б) redis `SETNX` на `(broadcast_id, chat_id)` или чекпоинт `last_sent_offset`. *(в Фазу 3)*
- **[MED] Медиа-file_id: переиспользовать `photo_input`, не плодить резолвер.** В `deliver()` не гейтить на `Path(media_path).is_file()` (file_id-строка провалит `is_file` → уедет в text-only, потеряв медиа). Сначала резолвить ref, потом диспатч по типу `BroadcastMedia` с готовым значением (`str` file_id | `FSInputFile`). *(Фаза 0)*
- **[MED] Аудит in-bot правок — telegram-идентичность.** `audit()` в `web/_common.py` ждёт web-`AdminIdentity`. Не подделывать её: добавить overload / тонкую запись `AuditLog` с `actor="tg:<id>"`. *(Фаза 2)*
- **[MED] `audience_stmt` вынести из web-роута.** `send_broadcast` уже импортит `audience_stmt` из `web.routes.admin.broadcasts`; композер бота усугубит зависимость bot→web-route. Вынести `audience_stmt` + счётчики в `src/application/services/audience.py`, импортить из обоих. *(Фаза 3)*
- **[LOW] Нормализация рядов на чтении:** группировать сиблингов, сжимать `row_index` в 0..N, кап 8 кнопок/ряд — чтобы разреженные/дублирующиеся `row_index` после правок не давали пустой/гигантский ряд. *(Фаза 1)*
- **[LOW] Стиль/значок — сначала проверить рендер на живом клиенте.** Bot API 9.4 за пределами cutoff: поля есть, но виден ли ЦВЕТ/значок — проверить одной тест-кнопкой на реальном клиенте владельца. PRIMARY-механика стиля — Unicode-квадрат в подписи (работает везде), нативный `style` — как усиление. *(Фаза 2, до полного пикера)*
- **[LOW] Стикер+клавиатура = 2 send.** Слать текст+кнопки первым (смысловое сообщение), стикер — после успеха; пару считать одной доставкой (fail, если любая нога упала); заложить 2× в flood-бюджет. *(Фаза 1/3)*
- **[LOW] Многорядный редактор — реалистичный effort.** Свободный ◀▶▲▼-reorder — самый крупный пункт Фазы 2; сначала ship пресеты плотности (1/2/3/4-в-ряд), свободный reorder — fast-follow.

---

## 2. Честно про «стиль кнопок» — что можно, а что нет

Это критично проговорить, иначе в UI будет «баг», которого нет. Telegram Bot API (9.4, фев-2026; в проекте aiogram 3.29.1) для inline-кнопки даёт:

**МОЖНО нативно:**
- **Цвет кнопки — ТОЛЬКО 3 пресета** + «по теме»: `primary` (синий), `success` (зелёный), `danger` (красный), либо default. `style_for_hex(hex)` уже честно снаппит любой HEX к ближайшему из трёх. Показываем 4 кнопки-пресета, **НЕ** color-wheel.
- **Кастом-эмодзи-значок слева от текста** кнопки (`icon_custom_emoji_id`) — но только при Telegram Premium у владельца бота (или Fragment-username), и виден лишь на клиентах после фев-2026.
- **Несколько кнопок в ряд** (до 8/ряд, до ~100 на клавиатуру) — это не «стиль», это раскладка `inline_keyboard=[[...],[...]]`.

**НЕЛЬЗЯ (не обещать владельцу):**
- Произвольный HEX/RGB/градиент, шрифт, размер, форма, фон кнопки.
- Кастом-эмодзи в **тексте подписи** кнопки (label — plain-текст без entities). Только нативный icon-слот.
- Разный цвет текста.

**Честные обходы для «поменять цвет», которые работают ВЕЗДЕ и всегда:**
1. Цветной Unicode-квадрат в подписи: `🟢 Сервер онлайн`, `🔴 Оффлайн`, `🟡 Внимание` — работает на всех клиентах, в отличие от нативного `style`.
2. Эмодзи-префиксы в тексте (🎁🔌🌍🛠).
3. Именованные пресеты стиля (комбо цвет+квадрат+значок).
4. Web_app-кнопка → брендированный UI в мини-аппе, где цвет любой (через `themeParams`).

**Кастом-эмодзи в ТЕКСТЕ сообщения/экрана/поста** (`<tg-emoji emoji-id=...>`) — работает, и не-премиум ПОЛУЧАТЕЛИ видят анимацию. Но требует Telegram Premium у ВЛАДЕЛЬЦА бота. Уже реализовано в `tasks.py::send_broadcast`.

**Стикеры** (обычные, .tgs, .webm, из паков, кастом-эмодзи-стикеры) — самая лёгкая часть, шлются одинаково по `file_id`, **без Premium**. Всегда ОТДЕЛЬНОЕ сообщение над меню/текстом, не часть клавиатуры.

---

## 3. Что уже есть в StepBot и что расширяем

### Готовые швы (переиспользуем, почти не трогая)
| Что | Файл | Роль в конструкторе |
|---|---|---|
| `style_for_hex(hex)` → 3 стиля | `src/bot/keyboards.py` | Рендер стиля кнопки уже готов |
| `simple_keyboard(buttons, columns=N)` | `src/bot/keyboards.py` | Механика N-в-ряд уже доказана |
| `WELCOME_STICKER` + `answer_sticker` | `src/bot/menu_render.py`, `config_registry.py` | Приём/показ стикера уже работает |
| `/setsticker`, `/setlogo`, `_set_config` | `src/bot/handlers/admin.py` | Паттерн захвата `file_id` из чата |
| `Broadcast` + `send_broadcast.kiq(id)` | `models/broadcast.py`, `taskiq/tasks.py` | Готовый движок постов (медиа/кнопка/эмодзи/аудитория/прогресс/отмена) |
| `MENU_ACTIONS`, `DEFAULT_MENU` | `src/bot/default_menu.py` | Каталог действий (тот же, что веб `/actions`) |
| `MenuNodeDAO` (tree/replace_all), `uow.menu_nodes` | `dao/admin.py`, `uow.py` | Запись в то же дерево, что веб |
| FSM-паттерн (StatesGroup+FSMContext+Redis) | `handlers/promo.py`, `withdraw.py` | Шаблон многошагового мастера |
| `show_screen` (edit-or-send) | `src/bot/screen.py` | Безопасная перерисовка экрана |
| `photo_input(ref)` (file_id/URL/uploads) | `src/bot/media.py` | Резолв медиа |

### Что расширяем
| Файл | Изменение |
|---|---|
| `src/bot/keyboards.py` | `menu_keyboard`: группировка сиблингов по `row_index` вместо жёсткого `[[_button(n)]]`; вынести общий `build_markup(rows)`; прокинуть `icon_custom_emoji_id` в `_button` |
| `src/infrastructure/taskiq/tasks.py` | `send_broadcast`: заменить хардкод `[[одна кнопка]]` на `build_markup`; ветка `send_sticker`; `media_input` для file_id |
| `src/infrastructure/database/models/menu_node.py` | `+ row_index` (единственный дефицит модели под «несколько в ряд») |
| `src/infrastructure/database/models/broadcast.py` | `+ keyboard` (JSON), `+ sticker_file_id`, опц. `+ scheduled_at`/`is_template` |
| `src/bot/media.py` | `+ media_input(ref)`: локальный файл → FSInputFile, иначе file_id-строка |
| `src/core/enums.py` | `BroadcastMedia.STICKER` |
| `src/web/routes/admin/menu.py` | `row_index` в `NodeIn`/`_serialize` (паритет веб↔бот) |

### Net-new файлы
- `src/bot/handlers/menu_builder.py` — роутер конструктора меню (namespace `mb:`)
- `src/bot/handlers/post_builder.py` — роутер композера постов (namespace `pb:`)
- `src/bot/styles.py` — каталог `STYLE_PRESETS`
- `src/application/services/menu_tree.py` — единый сервис сохранения дерева (вынос логики из web `save_menu`)

---

## 4. Целевая модель данных

Всё **аддитивно**, ничего не ломается. Одна ревизия Alembic, `down_revision="c4d9e1f2a3b7"` (текущий head — `reminder_steps`; перепроверить `alembic heads` перед генерацией).

### 4.1 `menu_nodes` — ОДНО обязательное поле
```python
# src/infrastructure/database/models/menu_node.py
row_index: Mapped[int] = mapped_column(default=0)  # номер ряда среди сиблингов
# order_index остаётся позицией ВНУТРИ ряда
```
**Backfill в миграции (обязателен!):** для каждого `parent_id` проставить `row_index` = порядковый номер по возрастанию `order_index` (каждый существующий узел → свой ряд). Без этого `default 0` схлопнет всё меню в один ряд → прод-регресс.

Уже есть и переиспользуем без новых колонок: `custom_emoji_id` String(32) (оживляем — прокидываем в `icon_custom_emoji_id`), `color` String(9), `image_path` String(512) (принимает file_id из бота).

Опционально: `sticker_file_id` String(128) для стикера на экране-узле (или переиспользовать `image_path` со схемой `"sticker:<file_id>"` — 0 миграций для MVP).

### 4.2 `broadcasts` — многорядная клавиатура + стикер
```python
# src/infrastructure/database/models/broadcast.py
keyboard: Mapped[dict | None]  # JSONB.with_variant(JSON,'sqlite'), nullable
# схема: {"rows": [[{"text","kind":"url|action|web_app|copy","payload","color","emoji_id"}]]}
sticker_file_id: Mapped[str | None]  # String(128)
scheduled_at: Mapped[datetime | None]  # AwareDateTime — для отложенной отправки (nice)
is_template: Mapped[bool]  # default False — шаблоны (high)
name: Mapped[str | None]  # String(64)
```
Скалярные `button_*` **ОСТАВИТЬ навсегда**. Воркер: `keyboard != NULL` → `build_markup(keyboard["rows"])`; `keyboard == NULL` → старый путь из `button_*` (обратная совместимость со всеми существующими и уже-PENDING рассылками). Прецедент JSON-колонки — `miniapp_config.ui` в ревизии `a68a369a14c4`.

### 4.3 `enums.py`
```python
BroadcastMedia.STICKER = "sticker"  # native_enum=False → БЕЗ DB-миграции (VARCHAR)
```

### 4.4 Config-ключи (`config_registry.py`, БЕЗ миграции — через `bot_config`)
- `CONSTRUCTOR_DEFAULT_STYLE` (default `""` = по теме)
- `CONSTRUCTOR_DEFAULT_COLUMNS` (int, default 1)
- `BROADCAST_CONCURRENCY_GUARD` (bool)

### 4.5 Опциональные будущие таблицы (Фаза 5+, НЕ для MVP)
- `media_assets` — библиотека медиа/стикеров/эмодзи (kind, file_id, custom_emoji_id, set_name, title, created_by). Требует GC сирот.
- `menu_versions` — снапшоты дерева для отката/черновиков (страховка от деструктивного веб-PUT).
- `button_clicks` — аналитика кликов по кнопкам.

**Лимиты длин зеркалить в FSM/pydantic-валидации (в JSON БД не проверит):** label≤64, payload≤4096, url≤512, color≤9, custom_emoji_id/emoji_id≤32, image_path/media_path≤512, sticker_file_id≤128, callback_data≤64 байта, caption медиа≤1024.

---

## 5. Фичи по фазам

Effort: **S**=часы, **M**=день, **L**=2-4 дня, **XL**=неделя+. Value: **must**/**high**/**nice**.

---

### ФАЗА 0 — Рефактор без новых фич (де-риск, невидимый деплой)

Сначала свести к одному, потом расширять. Иначе превью разойдётся с реальной отправкой, а веб и бот будут затирать правки друг друга.

| Задача | Effort | Value | Файлы | Готово когда |
|---|---|---|---|---|
| Единый билдер `build_markup(rows)→InlineKeyboardMarkup` | M | must | `keyboards.py` | `menu_keyboard` и `send_broadcast` строят markup через одну функцию |
| `media_input(ref)`: file_id / disk / URL | S | must | `media.py`, `tasks.py` | Медиа-file_id реально отправляются воркером |
| Вынести `MenuTreeService.load/serialize/save` из web `save_menu` | M | must | `services/menu_tree.py`, `web/routes/admin/menu.py` | Веб-PUT зовёт сервис; поведение не изменилось |

**Критерий фазы:** деплой без видимых изменений; `/start` и рассылка рендерятся байт-в-байт как раньше.

---

### ФАЗА 1 — Миграция схемы (аддитивно, обратно совместимо)

| Задача | Effort | Value | Файлы | Готово когда |
|---|---|---|---|---|
| `menu_nodes.row_index` + backfill `= order_index` per parent | M | must | `models/menu_node.py`, `migrations/versions/<new>.py` | До/после апгрейда `menu_keyboard` даёт идентичную раскладку |
| `menu_keyboard` группирует по `(row_index, order_index)`, ≤8/ряд | M | must | `keyboards.py` | Узлы с одинаковым row_index рисуются в один ряд |
| `broadcasts.keyboard` JSON + fallback на `button_*` | M | high | `models/broadcast.py`, `tasks.py` | keyboard=NULL → старый путь; keyboard≠NULL → build_markup |
| `BroadcastMedia.STICKER` + ветка `send_sticker` в deliver | S | high | `enums.py`, `tasks.py` | Стикер шлётся отдельным сообщением, текст+кнопки — вторым |
| `row_index` в `NodeIn`/`_serialize` (веб-паритет) | S | must | `web/routes/admin/menu.py` | Веб-сохранение не обнуляет ряды |

**Критерий фазы:** старые рассылки с `button_*` и NULL keyboard уходят по старому пути; меню после миграции идентично.

---

### ФАЗА 2 — MVP: полноценный конструктор кнопок в боте

**Это ядро запроса владельца.** Нативный FSM-редактор меню: ряды, несколько в ряд, типы кнопок, превью, стикеры+кастом-эмодзи, пресеты стиля.

| Задача | Effort | Value | Файлы | Готово когда |
|---|---|---|---|---|
| Вход «🧩 Конструктор» в `_admin_menu` → `mb:open` | S | must | `handlers/admin.py` | Из `/admin` открывается конструктор; `admin:brand` больше не тупик в веб |
| Роутер `menu_builder.py` (namespace `mb:`, FSM, гвард `is_admin`), включён ДО tickets и ДО actions | M | must | `handlers/menu_builder.py`, `handlers/__init__.py` | Текстовый ввод конструктора не улетает в тикеты |
| Навигация по дереву в чате + карточка кнопки (тап → `mb:node:<id>`) | L | must | `menu_builder.py`, `screen.py` | Видно дерево, вход в SCREEN-узлы, карточка со всеми полями |
| Добавить/удалить/скрыть кнопку (инкрементально, НЕ replace_all) | M | must | `menu_builder.py`, `dao/admin.py` | `mb:add`/`mb:del`(+confirm)/`mb:tog` работают, CASCADE сносит поддерево |
| Правка полей через FSM (label/url/text), лимиты + `html.escape` | M | must | `menu_builder.py` | Ввод режется по лимитам, `<`/`&` не ломают HTML |
| Выбор действия из `MENU_ACTIONS` (пагинация) | S | must | `menu_builder.py`, `default_menu.py` | Дропдаун действий, не хардкод кодов |
| **Многорядность:** режим правки ◀▶ (в ряду) ▲▼ (между рядами) / «новый ряд» / «слить» | L | must | `menu_builder.py`, `keyboards.py` | Кнопки переставляются в чате, реально рисуются N-в-ряд |
| Пресеты плотности (Список 1 / Пары 2 / Компакт 3 / Сетка 4) | S | high | `menu_builder.py` | Одним тапом раскидывает детей по рядам |
| Пикер стиля (честные 4: ⚪🔵🟢🔴 + сноска про лимит) | S | must | `menu_builder.py`, `keyboards.py` | Пишет color-сентинел, style_for_hex рендерит |
| Цветной квадрат-индикатор в подписи (🟢🔴🟡, дедуп ведущего) | S | high | `menu_builder.py` | Работает на всех клиентах |
| Стикер на экран (обычный/кастомный): FSM `F.sticker` → file_id | S | must | `menu_builder.py`, `actions.py`, `menu_render.py` | Стикер шлётся над экраном (answer_sticker) |
| Кастом-эмодзи-иконка: захват `entity.custom_emoji_id` → `icon_custom_emoji_id` с try/except | M | must | `menu_builder.py`, `keyboards.py` | Значок на кнопке при Premium; без — кнопка валидна |
| Фото экрана из бота: `F.photo` → file_id в `image_path` | S | high | `menu_builder.py` | Картинка экрана без веб-загрузки |
| Кастом-эмодзи + разметка в ТЕКСТЕ экрана (`<tg-emoji>`, HTML) | S | high | `menu_builder.py`, `actions.py` | Анимированный эмодзи в тексте SCREEN |
| Именованные `STYLE_PRESETS` (Действие/Покупка/Опасное/Онлайн/…) | M | high | `bot/styles.py`, `menu_builder.py` | Применение комбо к кнопке/ряду/экрану |
| Живой превью: `mb:preview` (дерево) и «как /start» (с рантайм-extras) | M | must | `menu_builder.py`, `menu_render.py` | Превью новым сообщением; режим «/start» совпадает с реальностью |
| Сохранение без конфликта с вебом (инкрементально + перечитывать дерево + audit_log) | M | must | `menu_builder.py`, `services/menu_tree.py` | Правки бота не воюют с веб-PUT; пропавший id → мягкий возврат |
| Сброс к дефолту из бота (переиспользовать `DEFAULT_MENU`) | S | nice | `menu_builder.py` | `mb:reset`(+confirm) пересобирает корень |

**Критерий MVP:** владелец в чате бота может собрать меню с многорядными кнопками, задать тип/действие/стиль каждой, вставить стикер и кастом-эмодзи, увидеть правдивое превью, и это применяется к реальному `/start`. Веб-конструктор видит те же изменения.

---

### ФАЗА 3 — Композер постов/рассылок в боте

| Задача | Effort | Value | Файлы | Готово когда |
|---|---|---|---|---|
| Роутер `post_builder.py` (namespace `pb:`, FSM) | L | high | `handlers/post_builder.py`, `handlers/__init__.py` | FSM собирает Broadcast и ставит `send_broadcast.kiq(id)` |
| Текст (HTML) + медиа (фото/видео/гиф → file_id, стикер) + кастом-эмодзи-префикс | M | high | `post_builder.py` | Все типы медиа собираются из чата |
| Многорядные кнопки поста (тот же `build_markup`, keyboard-JSON) | M | high | `post_builder.py`, `keyboards.py` | Пост с несколькими кнопками в ряд |
| Выбор аудитории со счётчиком (переиспользовать `audience_stmt`) | M | high | `post_builder.py`, `web/routes/admin/broadcasts.py` | Live-число ALL/ACTIVE/TRIAL/EXPIRED |
| **SAFETY-контур:** превью-себе → счётчик → типизированное подтверждение (N>500 вписать число) → guard от параллельной RUNNING | M | must | `post_builder.py`, `dao/admin.py` | Нельзя случайно разослать; битый HTML/emoji ловится превью |
| Прогресс + «⏹ Остановить» (status=CANCELED) | M | high | `post_builder.py` | Опрос sent/failed, мягкая отмена |
| Шаблоны постов (`is_template`, `name`, клонирование) | M | high | `models/broadcast.py`, `post_builder.py` | «Сохранить как шаблон» → переиспользовать |

**Критерий фазы:** владелец собирает и рассылает пост с многорядной клавиатурой, медиа/стикером, кастом-эмодзи и предпросмотром прямо из бота; движок доставки не переписан.

---

### ФАЗА 4 — Надёжность движка (важно при частых постах)

| Задача | Effort | Value | Файлы | Готово когда |
|---|---|---|---|---|
| Rate-limit / запрет параллельных рассылок (redis token-bucket или guard) | M | high | `tasks.py`, `redis/locks.py` | Две рассылки разом не пробивают 30 msg/s → нет FloodWait |
| Чекпоинт `last_sent_offset` / per-(broadcast,tg) SETNX-дедуп | M | high | `tasks.py`, `models/broadcast.py` | Рестарт RUNNING не шлёт заново всей аудитории |
| Advisory-lock `menu:edit` + optimistic version (web↔bot) | M | high | `services/menu_tree.py`, `redis/locks.py` | Конкурентная правка не затирает целиком |
| Обрезка caption медиа до 1024 + пред-валидация поста | S | high | `tasks.py`, `post_builder.py` | Длинный текст на медиа не роняет доставку |
| Планировщик отложенных постов (`scheduled_at` + cron `scan_scheduled_posts`) | M | nice | `tasks.py`, `models/broadcast.py` | «Запостить в HH:MM» |

---

### ФАЗА 5 — Опционально: Mini App + библиотеки + аналитика

| Задача | Effort | Value | Файлы | Готово когда |
|---|---|---|---|---|
| Mini App drag-drop конструктор + WYSIWYG-превью | XL | nice | `miniapp/constructor/`, `web/routes/admin/deps.py` (tma+is_staff), `web/app.py` | Визуальная раскладка рядов, пишет в тот же `PUT /bot-menu` |
| Библиотека медиа/стикеров (`media_assets` + бот-захват + пикер) | L | nice | `models/media_asset.py`, `dao/admin.py`, `uow.py` | Выбор из библиотеки вместо повторной пересылки |
| Снапшоты/версии меню (`menu_versions`, откат) | M | nice | `models/menu_version.py` | «Откатить к версии» / черновик→опубликовать |
| Аналитика кликов по кнопкам (`button_clicks`, CTR) | L | nice | `models/button_click.py`, `actions.py` | Экран «клики/CTR» |
| Веб-паритет: ряды и STICKER в SPA | L | nice | `admin/src/screens/BotButtons.tsx`, `Broadcasts.tsx` | Веб не отстаёт от бота |

---

## 6. Меню «что ещё можно сделать» (отмечай галочки)

### Кнопки и меню
- [ ] Дубликат узла/поддерева (`mb:dup`)
- [ ] Перенести кнопку в другое подменю (`mb:into`)
- [ ] Свой URL на кнопке-миниаппе (сейчас игнорит `payload`, берёт глобальный)
- [ ] `copy_text`-кнопка (копировать промокод/ключ в буфер) — полезно для VPN-шопа
- [ ] `switch_inline_query`-кнопка
- [ ] Reply-клавиатура (постоянное нижнее меню, `is_persistent`) — ключ `MAIN_MENU_MODE` уже есть
- [ ] Импорт/экспорт меню JSON-файлом (бэкап/перенос между ботами)

### Посты и рассылки
- [ ] «Отправить себе» превью (must — предохранитель)
- [ ] Стикер как тип медиа рассылки
- [ ] Переменные `{name}` `{days_left}` `{balance}` (персонализация per-user)
- [ ] Отложенная отправка (`scheduled_at`)
- [ ] Повторяющиеся/регулярные посты (`repeat_cron`)
- [ ] A/B-посты (2 варианта на сегмент, сравнение CTR)
- [ ] Реакции / закреп / автоудаление постов
- [ ] Мультиязычные посты (ru/en по `db_user.language`) — i18n-шов спит, поднимать только если нужен EN

### Стиль и бренд
- [ ] Пресеты стиля кнопок (Действие/Покупка/Опасное/Онлайн/…)
- [ ] Цветной квадрат-индикатор в подписи
- [ ] Кастом-эмодзи-иконка на кнопке (Bot API 9.4, Premium-гейтед)
- [ ] Дефолтный стиль/приветствие/лого/стикер из бота (через `bot_config`)

### Управление и надёжность
- [ ] Пауза/отмена/прогресс рассылки в чате
- [ ] Rate-limit против FloodWait
- [ ] Чекпоинт/дедуп прерванной рассылки
- [ ] Аудит in-bot правок (`audit_log` с telegram-идентичностью)
- [ ] Черновики/версии меню (откат)
- [ ] Гранулярные роли (может-строить-меню / может-рассылать / только-просмотр)

### Аналитика и визуал
- [ ] Клики по кнопкам / CTR
- [ ] Библиотека медиа/стикеров
- [ ] Mini App drag-drop конструктор (для «любого цвета» через тему)

---

## 7. Риски / ограничения и как обходим

**Ограничения Telegram (в UI как факты, не баги):**
1. Цвет кнопки = 3 пресета + дефолт. Обход: 4-пресетный пикер + Unicode-квадрат-префикс + web_app для «любого цвета». `style_for_hex` снаппит честно.
2. Стиль/значок виден только на клиентах ≥ фев-2026. Смысл держать в ТЕКСТЕ кнопки — стиль лишь подсказка.
3. Кастом-эмодзи (текст и icon) требует Telegram Premium у владельца. Обычные стикеры — без Premium. В подписи кнопки кастом-эмодзи невозможен вообще.
4. Стикер — отдельное сообщение без caption/markup → пост «стикер+текст+кнопки» = 2 send на юзера (учесть в прогрессе).

**Архитектурные риски:**
5. **Деструктивный веб-PUT** (`save_menu` делает wipe+reinsert, все id меняются). Бот пишет инкрементально и перечитывает дерево; под конкуренцию — advisory-lock `menu:edit` + optimistic version. Полный мерж не делаем (дорого).
6. **`row_index` backfill** — без per-parent заполнения меню схлопнется в один ряд. Обязателен `op.execute` + проверка «рендер до==после».
7. **file_id vs uploads/-путь** — бот даёт file_id, веб грузит на диск. `media_input(ref)` различает; веб-превью для file_id покажет плейсхолдер (осознанный компромисс).
8. **`send_broadcast` без retry** — падение → зависший RUNNING, ручной рестарт → дубли. До кнопки «повторить» — чекпоинт/SETNX-дедуп (Фаза 4).
9. **Нет глобального rate-лимитера** — параллельные рассылки (частые из бота) пробивают 30 msg/s. Redis token-bucket / запрет параллели (Фаза 4).
10. **Порядок роутеров** — `mb:`/`pb:` строго ДО `tickets` (catch-all `F.text`) и ДО `actions` (generic `nav:`/`act:`). Свои `mb:cancel`/`pb:cancel` чистят FSM осознанно (не через `nav:root`).
11. **callback_data ≤64 байта** — длинные payload (URL/текст) в `state.update_data`, не в callback.
12. **`parse_mode`-разнобой** — `Bot()` создан с `parse_mode=None`, `show_screen` дефолтит HTML. Пользовательский ввод `html.escape` перед HTML-режимом.
13. **Кэш `bot_config` 15с межпроцессный** — правка из бота доедет до веб/воркера до 15с. Не баг, документировать.

**Процессное:** aiogram floor в `pyproject` = 3.13, но lock = 3.29.1 (`style`/`icon` работают ≥3.27) — не понижать. `make revision` → глазами проверить миграцию → `make check` (mypy strict + ruff) перед коммитом. Общие файлы (`uow.py`, `models/__init__.py`, `enums.py`, `tasks.py`, `handlers/__init__.py`) — коммитить своим списком, не `git add -A`. `InlineKeyboardButton(**kwargs)` держать под `# type: ignore[arg-type]`.

---

## 8. Рекомендованный порядок и первые 3 шага

**Порядок фаз:** 0 (рефактор) → 1 (миграция) → **2 (MVP-конструктор кнопок)** → 3 (посты) → 4 (надёжность) → 5 (опц. Mini App/аналитика).

Не начинать Фазу 2 до завершения 0 и 1 — иначе превью разойдётся с реальностью и бот подерётся с вебом.

### Первые 3 шага (можно начинать сразу после аппрува)

**Шаг 1 — Фаза 0, единый билдер и медиа-резолвер.**
Вынести `build_markup(rows)` в `src/bot/keyboards.py`, переписать на неё `menu_keyboard` и блок сборки markup в `send_broadcast` (`tasks.py`). Добавить `media_input(ref)` в `src/bot/media.py` и использовать в `deliver()`. Деплой невидимый, регресс-проверка `/start` + тестовая рассылка.

**Шаг 2 — Фаза 1, миграция `row_index`.**
`make revision m="menu_row_index + broadcast keyboard"`, `down_revision="c4d9e1f2a3b7"` (перепроверить `alembic heads`). Добавить `menu_nodes.row_index` с backfill `= order_index` per parent + `broadcasts.keyboard` JSON + `BroadcastMedia.STICKER`. Переписать `menu_keyboard` на группировку по `(row_index, order_index)`. Глазами проверить миграцию, `make check`.

**Шаг 3 — Фаза 2, каркас конструктора.**
Создать `src/bot/handlers/menu_builder.py` (Router `menu_builder`, namespace `mb:`, `MenuBuilderForm`), включить в `build_router()` ДО `tickets` и ДО `actions`. Добавить кнопку «🧩 Конструктор» в `admin.py::_admin_menu` → `mb:open`. Реализовать навигацию по дереву + карточку кнопки + добавление/правку/удаление узла инкрементально через `uow.menu_nodes`. Дальше по таблице Фазы 2.