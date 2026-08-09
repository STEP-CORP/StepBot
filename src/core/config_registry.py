"""Bot-config parameter registry — the catalog behind admin screen 13 («Настройки бота»).

Single source of truth for every hot-reloadable parameter: key, category, editor type,
default, secret flag and RU/EN display strings. The DB (``bot_config_values``) stores
only admin overrides; the settings screen and ``ConfigService`` merge the two. Adding a
parameter here requires NO migration.

Conventions:
- keys are SCREAMING_SNAKE_CASE and stable (they are the public API of the registry);
- ``secret`` params render as password inputs and are Fernet-encrypted at rest;
- money values are minor units, durations are days/hours, times are "HH:MM" MSK.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from src.core.enums import ConfigCategory, ConfigParamType

BOOL = ConfigParamType.BOOL
INT = ConfigParamType.INT
STR = ConfigParamType.STR
SECRET = ConfigParamType.SECRET


@dataclass(frozen=True, slots=True)
class ParamSpec:
    key: str
    category: ConfigCategory
    type: ConfigParamType
    default: Any
    name_ru: str
    name_en: str
    desc_ru: str = ""
    desc_en: str = ""
    secret: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret", self.type is SECRET)


def _p(
    key: str,
    cat: ConfigCategory,
    typ: ConfigParamType,
    default: Any,
    ru: str,
    en: str,
    dru: str = "",
    den: str = "",
) -> ParamSpec:
    return ParamSpec(key, cat, typ, default, ru, en, dru, den)


C = ConfigCategory

REGISTRY: tuple[ParamSpec, ...] = (
    # --- MAIN ---------------------------------------------------------------
    _p(
        "BOT_USERNAME",
        C.MAIN,
        STR,
        "",
        "Username бота",
        "Bot username",
        "Без @; используется в ссылках (рефералка, кампании)",
        "Without @; used in deep links",
    ),
    _p(
        "ADMIN_IDS",
        C.MAIN,
        STR,
        "",
        "Администраторы",
        "Admin IDs",
        "Telegram ID через запятую — им доступна админка бота",
        "Comma-separated Telegram IDs",
    ),
    _p(
        "SUPPORT_CHAT_ID",
        C.MAIN,
        STR,
        "",
        "Группа поддержки",
        "Support chat ID",
        "ID группы, куда дублируются тикеты",
        "Group that mirrors support tickets",
    ),
    _p(
        "MAINTENANCE_MODE",
        C.MAIN,
        BOOL,
        False,
        "Режим техработ",
        "Maintenance mode",
        "Бот отвечает заглушкой всем, кроме админов",
        "Bot serves a stub to non-admins",
    ),
    _p(
        "MAINTENANCE_MESSAGE",
        C.MAIN,
        STR,
        "Ведутся технические работы, зайдите позже 🙏",
        "Текст техработ",
        "Maintenance text",
    ),
    _p(
        "START_MESSAGE",
        C.MAIN,
        STR,
        "👋 Это твой личный VPN.\n\nБыстрый, без лимитов скорости, сразу на всех "
        "устройствах. Оформи подписку за пару тапов или попробуй бесплатно.\n\n"
        "Выбирай, что дальше 👇",
        "Приветствие /start",
        "/start greeting",
    ),
    _p(
        "WELCOME_IMAGE",
        C.MAIN,
        STR,
        "",
        "Лого / приветственное фото",
        "Welcome image (logo)",
        "Лого вверху /start. Источник: загрузка в кабинете, URL, file_id или /setlogo в боте.",
        "Logo atop /start. Source: cabinet upload, URL, file_id, or /setlogo in the bot.",
    ),
    _p(
        "WELCOME_STICKER",
        C.MAIN,
        STR,
        "",
        "Приветственный стикер",
        "Welcome sticker",
        "file_id стикера вверху /start. Задать из бота: /setsticker (ответом на стикер).",
        "Sticker file_id at the top of /start. Set via the bot: /setsticker (reply to a sticker).",
    ),
    _p(
        "ADMIN_PANEL_URL",
        C.MAIN,
        STR,
        "",
        "Ссылка на веб-админку",
        "Web admin URL",
        "URL веб-кабинета для кнопки в /admin боте, напр. https://…/admin/",
        "Web admin URL for the /admin button in the bot, e.g. https://…/admin/",
    ),
    _p(
        "MAIN_MENU_MODE",
        C.MAIN,
        STR,
        "inline",
        "Режим главного меню",
        "Main menu mode",
        "inline — кнопки под сообщением, reply — клавиатура",
        "inline or reply keyboard",
    ),
    # --- SUBSCRIPTIONS & TRIAL ----------------------------------------------
    _p(
        "SALES_MODE",
        C.SUBSCRIPTIONS,
        STR,
        "plans",
        "Режим продаж",
        "Sales mode",
        "plans — готовые планы, constructor — конструктор",
        "plans or constructor",
    ),
    _p("TRIAL_ENABLED", C.SUBSCRIPTIONS, BOOL, True, "Пробный период", "Trial enabled"),
    _p("TRIAL_DURATION_DAYS", C.SUBSCRIPTIONS, INT, 3, "Длительность триала", "Trial days"),
    _p(
        "TRIAL_TRAFFIC_GB",
        C.SUBSCRIPTIONS,
        INT,
        10,
        "Трафик на триале (ГБ)",
        "Trial traffic GB",
        "0 — безлимит",
        "0 = unlimited",
    ),
    _p("TRIAL_DEVICE_LIMIT", C.SUBSCRIPTIONS, INT, 1, "Устройств на триале", "Trial devices"),
    _p(
        "DEFAULT_DEVICE_LIMIT", C.SUBSCRIPTIONS, INT, 3, "Устройств по умолчанию", "Default devices"
    ),
    _p(
        "AUTO_RENEWAL_ENABLED",
        C.SUBSCRIPTIONS,
        BOOL,
        True,
        "Автопродление с баланса",
        "Auto-renewal",
        "Списывать с баланса до истечения",
        "Charge balance before expiry",
    ),
    _p(
        "AUTO_RENEWAL_DAYS_BEFORE",
        C.SUBSCRIPTIONS,
        INT,
        1,
        "Автопродление за (дней)",
        "Auto-renew days before",
    ),
    _p(
        "CONSTRUCTOR_EXTRA_DEVICE_PRICE",
        C.SUBSCRIPTIONS,
        INT,
        5000,
        "Цена доп. устройства (коп./30 дн)",
        "Extra device price (minor/30d)",
    ),
    _p("CONSTRUCTOR_MAX_DEVICES", C.SUBSCRIPTIONS, INT, 10, "Макс. устройств", "Max devices"),
    _p(
        "SUBSCRIPTION_MINI_APP_URL",
        C.SUBSCRIPTIONS,
        STR,
        "",
        "URL мини-аппы",
        "Mini-app URL",
        "Открывается кнопками «Продлить» и меню",
        "Opened by renew buttons and the menu",
    ),
    # --- PAYMENTS -------------------------------------------------------------
    _p(
        "MIN_DEPOSIT_AMOUNT",
        C.PAYMENTS,
        INT,
        5000,
        "Минимальное пополнение (коп.)",
        "Min deposit (minor)",
    ),
    _p(
        "BALANCE_ENABLED",
        C.PAYMENTS,
        BOOL,
        True,
        "Кошелёк-баланс",
        "Wallet balance",
        "Разрешить пополнение и оплату с баланса",
        "Allow top-ups and balance payments",
    ),
    _p(
        "TAX_RATE_PERCENT",
        C.PAYMENTS,
        INT,
        6,
        "Ставка налога %",
        "Tax rate %",
        "Для расчёта чистой прибыли в разделе Платежи",
        "Feeds net-profit math",
    ),
    _p(
        "STARS_RATE_RUB",
        C.PAYMENTS,
        INT,
        130,
        "Курс Stars (коп. за ★)",
        "Stars rate (minor per ★)",
        "Для пересчёта цен в Stars",
        "Converts RUB prices to Stars",
    ),
    _p(
        "REFUND_ENABLED",
        C.PAYMENTS,
        BOOL,
        False,
        "Возвраты",
        "Refunds",
        "Разрешить возвраты из админки",
        "Allow refunds from the cabinet",
    ),
    _p(
        "PAYMENT_METHOD_ORDER",
        C.PAYMENTS,
        STR,
        "",
        "Порядок способов оплаты",
        "Payment methods order",
        "Через запятую: balance, stars, yookassa… — что раньше в списке (бот и мини-апп). "
        "Не указанные идут после в обычном порядке.",
        "Comma-separated: balance, stars, yookassa… — earliest first (bot + mini-app). "
        "Unlisted methods follow in default order.",
    ),
    _p(
        "PAYMENT_BALANCE_LABEL",
        C.PAYMENTS,
        STR,
        "",
        "Название кнопки «Баланс»",
        "Balance button label",
        "Пусто = «С баланса»",
        "Empty = «С баланса»",
    ),
    _p(
        "PAYMENT_STARS_LABEL",
        C.PAYMENTS,
        STR,
        "",
        "Название кнопки «Stars»",
        "Stars button label",
        "Пусто = «Telegram Stars»",
        "Empty = «Telegram Stars»",
    ),
    # --- NOTIFICATIONS --------------------------------------------------------
    _p(
        "REPORT_GROUP_ID",
        C.NOTIFICATIONS,
        STR,
        "",
        "Группа отчётов",
        "Report group ID",
        "Форум-группа, куда бот пишет отчёты по топикам",
        "Forum group for topic reports",
    ),
    # --- REFERRAL ---------------------------------------------------------------
    _p("REFERRAL_ENABLED", C.REFERRAL, BOOL, True, "Реферальная программа", "Referral program"),
    _p(
        "REFERRAL_BONUS_RUB",
        C.REFERRAL,
        INT,
        5000,
        "Бонус за приглашённого (коп.)",
        "Invite bonus (minor)",
        "Начисляется после первой оплаты друга",
        "Granted after friend's first payment",
    ),
    _p(
        "REFERRAL_BONUS_DAYS",
        C.REFERRAL,
        INT,
        7,
        "Бонус дней обоим",
        "Bonus days for both",
        "+N дней подписки пригласившему и другу",
        "+N days to both inviter and friend",
    ),
    _p(
        "AUTO_MAINTENANCE_ENABLED",
        C.SECURITY,
        BOOL,
        True,  # safe default: stop selling what can't be provisioned; self-heals on recovery
        "Авто-техрежим при падении панели",
        "Auto-maintenance on panel outage",
        "3 неудачных пинга панели подряд включают режим техработ; восстановление снимает его",
        "3 consecutive failed panel pings enable maintenance; recovery lifts it",
    ),
    _p(
        "UPDATE_CHECK_ENABLED",
        C.SECURITY,
        BOOL,
        True,
        "Проверять обновления",
        "Check for updates",
        "Раз в 6 ч сверяется с GitHub и шлёт кнопку «Обновить», когда выйдет новая версия",
        "Checks GitHub every 6h and DMs you an «Update» button when a new version ships",
    ),
    _p(
        "UPDATE_REPO",
        C.SECURITY,
        STR,
        "STEP-CORP/StepBot",
        "Репозиторий обновлений",
        "Update repo",
        "GitHub-репозиторий owner/name, откуда берутся обновления",
        "GitHub owner/name the updates come from",
    ),
    _p(
        "UPDATE_BRANCH",
        C.SECURITY,
        STR,
        "main",
        "Ветка обновлений",
        "Update branch",
        "Ветка, за которой следит проверка обновлений",
        "Branch the update check tracks",
    ),
    _p(
        "AUTO_UPDATE_ENABLED",
        C.SECURITY,
        BOOL,
        False,
        "Автоустановка обновлений",
        "Auto-install updates",
        "Найденное обновление ставится само (бэкап БД → пересборка → перезапуск). "
        "Выкл — приходит только уведомление с кнопкой «Обновить». Требует модуля updater",
        "Apply a found update automatically (DB backup → rebuild → restart). "
        "Off — you just get a notification with an «Update» button. Requires the updater module",
    ),
    _p(
        "POSTBACK_ENABLED",
        C.MAIN,
        BOOL,
        False,
        "S2S-постбеки (арбитраж)",
        "S2S postbacks (arbitrage)",
        "GET на URL трекера. Макросы: {user_id} {tg_id} {amount} {subid} {event}",
        "GET a tracker URL. Macros: {user_id} {tg_id} {amount} {subid} {event}",
    ),
    _p(
        "POSTBACK_URL_REGISTRATION",
        C.MAIN,
        STR,
        "",
        "URL постбека: регистрация",
        "Postback URL: registration",
    ),
    _p("POSTBACK_URL_TRIAL", C.MAIN, STR, "", "URL постбека: триал", "Postback URL: trial"),
    _p("POSTBACK_URL_PURCHASE", C.MAIN, STR, "", "URL постбека: покупка", "Postback URL: purchase"),
    _p(
        "NODE_STATUS_ENABLED",
        C.INTERFACE,
        BOOL,
        False,
        "Статус серверов юзеру",
        "Server status for users",
        "Кнопка «Статус серверов» в боте: 🟢/🟠/🔴 по нодам с онлайном",
        "«Server status» button in the bot: 🟢/🟠/🔴 per node with online counts",
    ),
    _p(
        "REMNAWAVE_RESYNC_ENABLED",
        C.SECURITY,
        BOOL,
        False,
        "Ночная сверка с панелью",
        "Nightly panel resync",
        "Каждую ночь сверяет подписки с Remnawave и чинит дрейф (ручные правки в панели)",
        "Nightly reconciliation with Remnawave; heals drift from manual panel edits",
    ),
    _p(
        "WEB_CABINET_ENABLED",
        C.INTERFACE,
        BOOL,
        False,
        "Веб-кабинет (покупка с сайта)",
        "Web cabinet (buy from a site)",
        "Регистрация/логин по email на сайте и покупка без Telegram",
        "Email register/login on a site and purchase without Telegram",
    ),
    _p(
        "CABINET_URL",
        C.INTERFACE,
        STR,
        "",
        "URL веб-кабинета",
        "Web cabinet URL",
        "Адрес страницы кабинета — обычно https://ваш-домен/web/ (на «/» лежит лендинг). "
        "Отсюда же берётся Redirect URI для входа через Google/Яндекс/VK.",
        'The cabinet page URL — usually https://your-domain/web/ ("/" serves the landing). '
        "The OAuth redirect URI is derived from it.",
    ),
    _p(
        "CABINET_EMAIL_VERIFICATION",
        C.INTERFACE,
        BOOL,
        True,
        "Подтверждать email при регистрации",
        "Verify email on registration",
        "Выкл — аккаунт активен сразу (без письма)",
        "Off — the account is active immediately (no email)",
    ),
    _p("SMTP_HOST", C.INTERFACE, STR, "", "SMTP хост", "SMTP host"),
    _p("SMTP_PORT", C.INTERFACE, INT, 587, "SMTP порт", "SMTP port"),
    _p("SMTP_USER", C.INTERFACE, STR, "", "SMTP логин", "SMTP user"),
    _p("SMTP_PASSWORD", C.INTERFACE, SECRET, "", "SMTP пароль", "SMTP password"),
    _p("SMTP_FROM", C.INTERFACE, STR, "", "Отправитель писем", "Mail from-address"),
    _p(
        "OAUTH_GOOGLE_CLIENT_ID",
        C.INTERFACE,
        STR,
        "",
        "Google OAuth client id",
        "Google OAuth client id",
        "console.cloud.google.com → APIs & Services → Credentials → OAuth client ID (Web). "
        "В «Authorized redirect URIs» впишите ровно: https://ваш-домен/web",
        "console.cloud.google.com → Credentials → OAuth client ID (Web). Authorized redirect "
        "URI must be exactly: https://your-domain/web",
    ),
    _p(
        "OAUTH_GOOGLE_CLIENT_SECRET",
        C.INTERFACE,
        SECRET,
        "",
        "Google OAuth secret",
        "Google OAuth secret",
        "Client secret из того же OAuth-клиента Google",
        "Client secret of the same Google OAuth client",
    ),
    _p(
        "OAUTH_YANDEX_CLIENT_ID",
        C.INTERFACE,
        STR,
        "",
        "Yandex OAuth client id",
        "Yandex OAuth client id",
        "oauth.yandex.ru → создать приложение → Веб-сервисы, доступ «Электронная почта» "
        "(login:email). Redirect URI впишите ровно: https://ваш-домен/web",
        "oauth.yandex.ru → new app → Web services, scope login:email. Redirect URI must be "
        "exactly: https://your-domain/web",
    ),
    _p(
        "OAUTH_YANDEX_CLIENT_SECRET",
        C.INTERFACE,
        SECRET,
        "",
        "Yandex OAuth secret",
        "Yandex OAuth secret",
        "Пароль приложения из oauth.yandex.ru",
        "App password from oauth.yandex.ru",
    ),
    _p(
        "OAUTH_VK_CLIENT_ID",
        C.INTERFACE,
        STR,
        "",
        "VK ID client id (id.vk.com)",
        "VK ID client id (id.vk.com)",
        "id.vk.com → приложение → «Доверенный redirect URL»: https://ваш-домен/web",
        "id.vk.com → app → trusted redirect URL: https://your-domain/web",
    ),
    _p(
        "OAUTH_VK_CLIENT_SECRET",
        C.INTERFACE,
        SECRET,
        "",
        "VK ID защищённый ключ",
        "VK ID client secret",
        "Защищённый ключ приложения VK ID",
        "VK ID app secure key",
    ),
    _p(
        "NALOGO_ENABLED",
        C.PAYMENTS,
        BOOL,
        False,
        "Чеки НалоГО (самозанятые)",
        "NalogGO receipts (self-employed)",
        "Регистрирует оплаты как доход в «Мой налог», ссылку на чек шлёт покупателю",
        "Registers payments as income in «Мой налог»; sends the receipt link to the buyer",
    ),
    _p("NALOGO_INN", C.PAYMENTS, STR, "", "ИНН самозанятого", "Self-employed INN"),
    _p(
        "NALOGO_TOKEN",
        C.PAYMENTS,
        SECRET,
        "",
        "Токен «Мой налог»",
        "«Мой налог» token",
        "Device-токен из приложения/регистрации lknpd.nalog.ru",
        "Device token from the lknpd.nalog.ru app/registration",
    ),
    _p(
        "NALOGO_SERVICE_NAME",
        C.PAYMENTS,
        STR,
        "Доступ к VPN-сервису",
        "Название услуги в чеке",
        "Service name on the receipt",
    ),
    _p(
        "DEVICE_GUARD_ENABLED",
        C.SECURITY,
        BOOL,
        False,
        "Детект шеринга (Device Guard)",
        "Device Guard (sharing detection)",
        "Считает уникальные онлайн-IP подписки по всем нодам и сравнивает с лимитом устройств",
        "Counts unique online IPs per subscription across nodes vs the device limit",
    ),
    _p(
        "DEVICE_GUARD_MAX_IPS",
        C.SECURITY,
        INT,
        0,
        "Лимит IP по умолчанию",
        "Default IP limit",
        "Для подписок без лимита устройств; 0 — такие не проверять",
        "For subscriptions without a device limit; 0 — skip them",
    ),
    _p(
        "DEVICE_GUARD_TOLERANCE",
        C.SECURITY,
        INT,
        1,
        "Допуск IP",
        "IP tolerance",
        "Запас сверх лимита (NAT/смена сети), прежде чем считать шерингом",
        "Slack above the limit (NAT/network switching) before flagging",
    ),
    _p(
        "DEVICE_GUARD_ACTION",
        C.SECURITY,
        STR,
        "alert",
        "Действие при шеринге",
        "Sharing action",
        "alert — только алерт админам; drop — рвать соединения; disable — выключить подписку",
        "alert — admins only; drop — kill connections; disable — turn the subscription off",
    ),
    _p(
        "REFERRAL_WITHDRAWAL_ENABLED",
        C.REFERRAL,
        BOOL,
        False,
        "Вывод реф-заработка",
        "Referral withdrawals",
        "Кнопка «Вывести» в рефералке: заявка админу, выплата вручную",
        "«Withdraw» button: request to admin, manual payout",
    ),
    _p(
        "REFERRAL_WITHDRAWAL_MIN",
        C.REFERRAL,
        INT,
        50000,
        "Мин. сумма вывода (коп.)",
        "Min withdrawal (minor)",
        "Порог в копейках; 50000 = 500 ₽",
        "Threshold in kopeks; 50000 = 500 RUB",
    ),
    _p(
        "REFERRAL_PERCENT",
        C.REFERRAL,
        INT,
        10,
        "Процент с платежей рефералов",
        "Referral %",
        "Доля с каждого пополнения реферала",
        "Share of each referral top-up",
    ),
    # --- SECURITY ----------------------------------------------------------------
    _p(
        "BLACKLIST_CHECK_ENABLED",
        C.SECURITY,
        BOOL,
        False,
        "Проверка по чёрному списку",
        "Blacklist check",
        "Проверять юзеров по базе недоброжелателей",
        "Check users against the shared blacklist",
    ),
    _p(
        "RATE_LIMIT_ENABLED",
        C.SECURITY,
        BOOL,
        False,
        "Антифлуд (rate-limit)",
        "Anti-flood rate limit",
        "Игнорировать частые действия одного юзера в боте",
        "Ignore a user's too-frequent bot actions",
    ),
    _p(
        "RATE_LIMIT_COOLDOWN_SEC",
        C.SECURITY,
        INT,
        1,
        "Кулдаун антифлуда (сек)",
        "Anti-flood cooldown (sec)",
        "Минимум секунд между действиями одного юзера",
        "Minimum seconds between one user's actions",
    ),
    _p(
        "AUTO_PURCHASE_AFTER_TOPUP",
        C.MAIN,
        BOOL,
        True,
        "Автопокупка после пополнения",
        "Auto-purchase after top-up",
        "Не хватило на балансе → пополнил → подписка покупается сама",
        "Short on balance → top up → the pending purchase completes itself",
    ),
    _p(
        "CART_TTL_SECONDS",
        C.MAIN,
        INT,
        86400,
        "Время жизни корзины (сек)",
        "Cart TTL (sec)",
        "Сколько хранить отложенную покупку до пополнения",
        "How long a pending purchase waits for a top-up",
    ),
    _p(
        "TRIAL_PRICE",
        C.SUBSCRIPTIONS,
        INT,
        0,
        "Цена пробного (коп., 0 = бесплатно)",
        "Trial price (minor, 0 = free)",
        "Платный триал: сумма к оплате за пробный период",
        "Paid trial: amount charged for the trial",
    ),
    _p(
        "TRIAL_CARRYOVER_DAYS",
        C.SUBSCRIPTIONS,
        BOOL,
        True,
        "Переносить остаток триала в платную",
        "Carry trial days into paid",
        "При первой оплате добавить неиспользованные дни триала",
        "On the first paid purchase, add the unused trial days",
    ),
    _p(
        "GRACE_ENABLED",
        C.SUBSCRIPTIONS,
        BOOL,
        False,
        "Спасательный круг (grace)",
        "Grace period",
        "Истёкшая ОПЛАЧЕННАЯ подписка не гаснет сразу: даём короткое окно с урезанным "
        "трафиком, оплата — полное восстановление. Триалов не касается.",
        "Expired PAID subs get a short limited-traffic window instead of going dark; "
        "paying restores everything. Trials are unaffected.",
    ),
    _p(
        "GRACE_DAYS",
        C.SUBSCRIPTIONS,
        INT,
        5,
        "Grace: дней доступа",
        "Grace: days",
        "Сколько дней держать урезанный доступ после истечения",
        "How many days the limited window lasts after expiry",
    ),
    _p(
        "GRACE_TRAFFIC_GB",
        C.SUBSCRIPTIONS,
        INT,
        1,
        "Grace: трафик, ГБ",
        "Grace: traffic GB",
        "Лимит трафика в grace-окне (0 — без лимита)",
        "Traffic cap during the grace window (0 — unlimited)",
    ),
    _p(
        "GRACE_COOLDOWN_DAYS",
        C.SUBSCRIPTIONS,
        INT,
        30,
        "Grace: кулдаун, дней",
        "Grace: cooldown days",
        "Не выдавать grace повторно чаще, чем раз в N дней (анти-абуз)",
        "Do not re-grant grace more often than once per N days (anti-abuse)",
    ),
    _p(
        "CHANNEL_SUB_REQUIRED",
        C.SECURITY,
        BOOL,
        False,
        "Обязательная подписка на канал",
        "Required channel subscription",
    ),
    _p("CHANNEL_SUB_ID", C.SECURITY, STR, "", "Канал для проверки", "Channel to check"),
    _p(
        "CHANNEL_SUB_CHANNELS",
        C.SECURITY,
        STR,
        "",
        "Каналы для подписки (по строке)",
        "Required channels (one per line)",
        "Каждая строка: @канал | Название | ссылка. Бот должен быть админом канала.",
        "Each line: @channel | Title | link. The bot must be an admin of the channel.",
    ),
    _p(
        "CHANNEL_SUB_SCOPE",
        C.SECURITY,
        STR,
        "all",
        "Что гейтить подпиской",
        "Gate scope",
        "all — всё; trial — только пробный период; buy — только покупки",
        "all — everything; trial — only the trial; buy — only purchases",
    ),
    # --- BACKUPS -------------------------------------------------------------------
    _p("BACKUP_ENABLED", C.BACKUPS, BOOL, True, "Автоматический бэкап", "Auto backup"),
    _p("BACKUP_TIME", C.BACKUPS, STR, "04:00", "Время бэкапа", "Backup time"),
    _p("BACKUP_KEEP_LAST", C.BACKUPS, INT, 7, "Хранить копий", "Keep last N"),
    _p(
        "BACKUP_ENCRYPTION_PASSWORD",
        C.BACKUPS,
        SECRET,
        "",
        "Пароль шифрования бэкапа",
        "Backup encryption password",
    ),
    # --- BOT INTERFACE ---------------------------------------------------------------
    _p(
        "HIDE_SUBSCRIPTION_LINK",
        C.INTERFACE,
        BOOL,
        False,
        "Скрывать ссылку подписки",
        "Hide subscription link",
        "Показывать только кнопку «Открыть в приложении»",
        "Show only the open-in-app button",
    ),
    _p(
        "SHOW_TRAFFIC_USAGE",
        C.INTERFACE,
        BOOL,
        True,
        "Показывать расход трафика",
        "Show traffic usage",
    ),
    _p(
        "MESSAGE_CLEANUP_ENABLED",
        C.INTERFACE,
        BOOL,
        False,
        "Очистка чата",
        "Chat cleanup",
        "Удалять сообщения пользователя после ответа, чтобы в чате был только экран бота",
        "Delete the user's messages after replying so only the bot screen remains",
    ),
    _p(
        "CONNECTION_APPS",
        C.INTERFACE,
        STR,
        "happ,incy",
        "Приложения на вкладке «Подключение»",
        "Apps on the Connect tab",
        "Список через запятую. По умолчанию два — happ, incy — чтобы не пугать простынёй. "
        "Доступны: happ, incy, v2raytun, hiddify, streisand, shadowrocket, v2box, clash, "
        "singbox. Порядок = порядок показа, первый = основной в мастере",
        "Comma-separated. Defaults to two — happ, incy — to avoid overwhelming users. "
        "Available: happ, incy, v2raytun, hiddify, streisand, shadowrocket, v2box, clash, "
        "singbox. Order = display order, first = the wizard's primary app",
    ),
    _p(
        "CABINET_BUTTONS",
        C.INTERFACE,
        STR,
        "subscription,balance,history,referral,promocode,support",
        "Кнопки в «Личном кабинете»",
        "Buttons on the «Account» screen",
        "Список через запятую: subscription, balance, history, referral, promocode, support. "
        "Убери лишние или поменяй порядок — так меняется экран «Личный кабинет» в боте",
        "Comma-separated: subscription, balance, history, referral, promocode, support. "
        "Drop the ones you don't need or reorder — controls the bot's «Account» screen",
    ),
    # Empty default = "use the built-in template" (src/bot/cabinet_text.py). Storing the long
    # default HTML here would make core depend on the bot layer; the renderer/endpoint fall back.
    _p(
        "MENU_TEXT_EMOJI",
        C.INTERFACE,
        STR,
        "",
        "Кастом-эмодзи в тексте меню",
        "Custom emoji in menu text",
        "ID премиум-эмодзи и запасной символ через пробел: «5368324170671202286 🔥». "
        "Запасной виден, если эмодзи недоступно",
        "Premium emoji id + a fallback char, space-separated: «5368324170671202286 🔥»",
    ),
    _p(
        "CABINET_TEXT_EMOJI",
        C.INTERFACE,
        STR,
        "",
        "Кастом-эмодзи в тексте кабинета",
        "Custom emoji in cabinet text",
        "ID премиум-эмодзи и запасной символ через пробел. Запасной виден, если эмодзи недоступно",
        "Premium emoji id + a fallback char, space-separated",
    ),
    _p(
        "CABINET_CUSTOM_BUTTONS",
        C.INTERFACE,
        STR,
        "",
        "Свои кнопки в «Личном кабинете»",
        "Custom buttons on the «Account» screen",
        'JSON-список своих кнопок-ссылок: [{"label":"Наш канал","url":"https://t.me/..."}]. '
        "Проще редактировать в конструкторе",
        'JSON list of custom link buttons: [{"label":"...","url":"https://..."}]',
    ),
    _p(
        "SCREEN_BUTTONS",
        C.INTERFACE,
        STR,
        "",
        "Кнопки экранов бота",
        "Bot screen buttons",
        "JSON: по каждому экрану — переименовать/скрыть/переставить кнопки и добавить свои. "
        "Пусто — экраны как есть. Редактируется в конструкторе.",
        "JSON keyed by screen: rename/hide/reorder buttons and add custom ones. "
        "Empty — screens unchanged. Edited in the builder.",
    ),
    _p(
        "SCREEN_TEXTS",
        C.INTERFACE,
        STR,
        "",
        "Тексты экранов бота",
        "Bot screen texts",
        "JSON: свой текст для статичных экранов (Подключение, Поддержка, Соглашение и т.д.). "
        "Пусто — текст как в боте. Метки {значение} работают только на экранах с данными.",
        "JSON keyed by screen: custom text for static screens. "
        "Empty — default text. {token} placeholders apply only on data-driven screens.",
    ),
    _p(
        "CABINET_TEXT",
        C.INTERFACE,
        STR,
        "",
        "Текст «Личного кабинета»",
        "«Account» screen text",
        "HTML-текст экрана. Метки: {имя} {id} {баланс} {друзей} {подписка} — подставятся у "
        "каждого юзера. Редактируется в конструкторе кнопками-метками",
        "HTML screen text. Placeholders: {имя} {id} {баланс} {друзей} {подписка}",
    ),
    _p(
        "CABINET_SUB_ACTIVE",
        C.INTERFACE,
        STR,
        "",
        "Блок {подписка} — активна",
        "{подписка} block — active",
        "Подставляется вместо {подписка}, когда подписка активна. Метки: {срок} {осталось} "
        "{устройств} {трафик} {автопродление}",
        "Fills {подписка} when the subscription is active. Placeholders: {срок} {осталось} "
        "{устройств} {трафик} {автопродление}",
    ),
    _p(
        "CABINET_SUB_INACTIVE",
        C.INTERFACE,
        STR,
        "",
        "Блок {подписка} — не активна",
        "{подписка} block — inactive",
        "Подставляется вместо {подписка}, когда активной подписки нет",
        "Fills {подписка} when there is no active subscription",
    ),
    _p(
        "TERMS_TEXT",
        C.INTERFACE,
        STR,
        "",
        "Пользовательское соглашение",
        "Terms of Service",
        "Текст показывается по кнопке «Пользовательское соглашение» — добавьте её в конструкторе "
        "меню (действие terms). Пусто — кнопку лучше не показывать.",
        "Shown by the «Terms of Service» button — add it in the menu builder (action terms). "
        "Empty — better not to show the button.",
    ),
    _p(
        "PRIVACY_TEXT",
        C.INTERFACE,
        STR,
        "",
        "Политика конфиденциальности",
        "Privacy Policy",
        "Текст показывается по кнопке «Политика конфиденциальности» — добавьте её в конструкторе "
        "меню (действие privacy). Пусто — кнопку лучше не показывать.",
        "Shown by the «Privacy Policy» button — add it in the menu builder (action privacy). "
        "Empty — better not to show the button.",
    ),
    _p(
        "SUPPORT_MODE",
        C.INTERFACE,
        STR,
        "tickets",
        "Канал поддержки",
        "Support mode",
        "tickets — в боте, redirect — на аккаунт, bot — отдельный бот, miniapp — чат в мини-аппе",
        "tickets / redirect / bot / miniapp",
    ),
    _p(
        "SUPPORT_BOT_USERNAME",
        C.INTERFACE,
        STR,
        "",
        "Бот поддержки (username)",
        "Support bot username",
        "Для режима «bot»: @username отдельного саппорт-бота, кнопка открывает его",
        "For «bot» mode: the separate support bot's @username",
    ),
    # --- AI SUPPORT ---------------------------------------------------------------
    _p(
        "AI_SUPPORT_ENABLED",
        C.SUPPORT,
        BOOL,
        False,
        "Включить ИИ-поддержку",
        "Enable AI support",
        "ИИ отвечает в тикетах вместо оператора; сложные случаи (деньги, спор) эскалирует",
        "The AI answers tickets; hard cases (money, disputes) escalate to a human",
    ),
    _p(
        "AI_SUPPORT_API_KEY",
        C.SUPPORT,
        SECRET,
        "",
        "API-ключ Claude (sk-ant-…)",
        "Claude API key (sk-ant-…)",
        "Свой ключ с console.anthropic.com — хранится в зашифрованном виде",
        "Your key from console.anthropic.com — stored encrypted",
    ),
    _p(
        "AI_SUPPORT_MODEL",
        C.SUPPORT,
        STR,
        "claude-haiku-4-5-20251001",
        "Модель",
        "Model",
        "По умолчанию Claude Haiku 4.5 — дёшево и быстро для поддержки",
        "Defaults to Claude Haiku 4.5 — cheap and fast for support",
    ),
    _p(
        "AI_SUPPORT_KNOWLEDGE_BASE",
        C.SUPPORT,
        STR,
        "",
        "База знаний",
        "Knowledge base",
        "Факты о сервисе (тарифы, приложения, решения) — этим «обучаешь» ИИ. Пусто → базовый набор",
        "Facts about your service (plans, apps, fixes) — this «trains» the AI. Empty → defaults",
    ),
    _p(
        "AI_SUPPORT_EXTRA_PROMPT",
        C.SUPPORT,
        STR,
        "",
        "Доп. инструкции ИИ",
        "Extra AI instructions",
        "Тон, стиль, особые правила — добавляются к системному промпту",
        "Tone, style, special rules — appended to the system prompt",
    ),
    _p(
        "REPORT_DM_ADMINS",
        C.NOTIFICATIONS,
        BOOL,
        False,
        "Дублировать отчёты в ЛС админам",
        "Also DM reports to admins",
        "Отчёты/бекапы/уведомления приходят и в личку админам, не только в группу",
        "Reports/backups/notifications also arrive in admins' DMs, not only the group",
    ),
    _p(
        "SUPPORT_REDIRECT_USERNAME",
        C.INTERFACE,
        STR,
        "",
        "Аккаунт поддержки",
        "Support account",
        "@username для режима redirect",
        "@username for redirect mode",
    ),
    _p(
        "BUTTON_COLOR_DEFAULT",
        C.INTERFACE,
        STR,
        "",
        "Цвет кнопок по умолчанию",
        "Default button color",
        "#HEX; пусто — стандартный",
        "#HEX; empty = default",
    ),
    # --- Screen banners: a photo above every screen. Each key falls back to BANNER_DEFAULT,
    #     then to WELCOME_IMAGE, then to the bundled banner. Source: cabinet upload, URL,
    #     file_id, or /setbanner <screen> in the bot (reply to a photo).
    _p(
        "BANNER_ENABLED",
        C.INTERFACE,
        BOOL,
        True,
        "Баннеры на экранах",
        "Screen banners",
        "Показывать фото-баннер над каждым экраном бота",
        "Show a photo banner above every bot screen",
    ),
    _p(
        "BANNER_MODE",
        C.INTERFACE,
        STR,
        "one",
        "Режим баннеров",
        "Banner mode",
        "one — одна картинка «по умолчанию» на всех экранах; per_screen — своя картинка на каждый",
        "one — a single default image on every screen; per_screen — a distinct image per screen",
    ),
    _p(
        "BANNER_DEFAULT",
        C.INTERFACE,
        STR,
        "",
        "Баннер по умолчанию",
        "Default banner",
        "Фото для экранов без своего баннера. Загрузка в кабинете, URL, file_id или /setbanner.",
        "Photo for screens without their own banner. Cabinet upload, URL, file_id or /setbanner.",
    ),
    _p("BANNER_MENU", C.INTERFACE, STR, "", "Баннер: меню", "Banner: main menu"),
    _p("BANNER_BUY", C.INTERFACE, STR, "", "Баннер: покупка", "Banner: buy funnel"),
    _p("BANNER_CABINET", C.INTERFACE, STR, "", "Баннер: кабинет", "Banner: cabinet"),
    _p("BANNER_SUBSCRIPTION", C.INTERFACE, STR, "", "Баннер: подписка", "Banner: subscription"),
    _p("BANNER_TRAFFIC", C.INTERFACE, STR, "", "Баннер: трафик", "Banner: traffic"),
    _p("BANNER_BALANCE", C.INTERFACE, STR, "", "Баннер: баланс", "Banner: balance"),
    _p("BANNER_REFERRAL", C.INTERFACE, STR, "", "Баннер: рефералка", "Banner: referral"),
    _p("BANNER_SUPPORT", C.INTERFACE, STR, "", "Баннер: поддержка", "Banner: support"),
    _p("BANNER_TRIAL", C.INTERFACE, STR, "", "Баннер: пробный период", "Banner: trial"),
    _p(
        "MTPROTO_PROXY_ENABLED",
        C.INTERFACE,
        BOOL,
        False,
        "Кнопка MTProto-прокси",
        "MTProto proxy button",
        "Показывать кнопку прокси в боте и мини-аппе",
        "Show a proxy button in bot and mini-app",
    ),
    _p(
        "MTPROTO_PROXY_URL",
        C.INTERFACE,
        STR,
        "",
        "Ссылка MTProto-прокси",
        "MTProto proxy link",
        "t.me/proxy?server=…&port=…&secret=… (или tg://proxy?…)",
        "t.me/proxy?server=…&port=…&secret=… (or tg://proxy?…)",
    ),
)

_BY_KEY: dict[str, ParamSpec] = {p.key: p for p in REGISTRY}

# Keys are the registry's public API and must be unique. A duplicate silently shadows the
# earlier ParamSpec (its type/default/secret flag reach the cabinet instead), so fail loudly
# at import — this is what the duplicate-key guard prevents from regressing.
if len(_BY_KEY) != len(REGISTRY):
    _dupes = sorted(k for k, n in Counter(p.key for p in REGISTRY).items() if n > 1)
    raise RuntimeError(f"duplicate config-registry keys: {_dupes}")

# Display order of categories on the settings screen (mirrors the design).
CATEGORY_ORDER: tuple[ConfigCategory, ...] = (
    C.MAIN,
    C.SUBSCRIPTIONS,
    C.PAYMENTS,
    C.NOTIFICATIONS,
    C.REFERRAL,
    C.SECURITY,
    C.BACKUPS,
    C.SUPPORT,
    C.INTERFACE,
)

CATEGORY_NAMES: dict[ConfigCategory, dict[str, str]] = {
    C.MAIN: {"ru": "Основные", "en": "General"},
    C.SUBSCRIPTIONS: {"ru": "Подписки и триал", "en": "Subscriptions & trial"},
    C.PAYMENTS: {"ru": "Платежи", "en": "Payments"},
    C.NOTIFICATIONS: {"ru": "Уведомления", "en": "Notifications"},
    C.REFERRAL: {"ru": "Реферальная программа", "en": "Referral"},
    C.SECURITY: {"ru": "Безопасность", "en": "Security"},
    C.BACKUPS: {"ru": "Бэкапы", "en": "Backups"},
    C.SUPPORT: {"ru": "ИИ-поддержка", "en": "AI support"},
    C.INTERFACE: {"ru": "Интерфейс бота", "en": "Bot interface"},
}


def all_specs() -> tuple[ParamSpec, ...]:
    """The core registry (settings-screen order)."""
    return REGISTRY


def spec(key: str) -> ParamSpec:
    """Registry lookup; raises KeyError for unknown keys (typo guard)."""
    return _BY_KEY[key]


def has(key: str) -> bool:
    return key in _BY_KEY


def coerce(key: str, raw: Any) -> Any:
    """Validate/coerce an incoming value to the parameter's declared type."""
    s = spec(key)
    if s.type is BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    if s.type is INT:
        return int(raw)
    return "" if raw is None else str(raw)
