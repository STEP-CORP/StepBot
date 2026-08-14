"""Owner-editable screen captions with placeholders (safe templating for dynamic screens).

Screens like the cabinet build their caption from live user data (name, balance, subscription,
expiry). To let the owner rewrite that text WITHOUT losing the data, the caption becomes a
template: the owner types free text and drops ``{placeholder}`` tokens where a live value
should go. ``caption_for`` fills the known tokens from a context dict; unknown tokens are left
as-is, and if anything goes wrong the original (code-built) caption is used - so a bad template
can never brick a screen.

An empty template ⇒ the screen renders exactly as before (byte-for-byte), which keeps every
un-configured bot behaving identically.
"""

from __future__ import annotations

import re

# Tokens are ASCII/Cyrillic words in braces, e.g. {имя}, {баланс}. HTML (<b>…</b>) is untouched.
_TOKEN = re.compile(r"\{([A-Za-zА-Яа-яЁё0-9_]+)\}")


def render_template(template: str, context: dict[str, str]) -> str:
    """Replace every known ``{token}`` with its value; leave unknown tokens literally in place."""

    def repl(m: re.Match[str]) -> str:
        return context.get(m.group(1), m.group(0))

    return _TOKEN.sub(repl, template)


def caption_for(template: str | None, default_text: str, context: dict[str, str]) -> str:
    """The screen caption: the owner's template filled from ``context``, or the default.

    Empty template ⇒ default (unchanged behaviour). Any failure, or a template that renders to
    blank ⇒ default. This is the single safety valve that keeps a mis-typed template harmless.
    """
    tpl = (template or "").strip()
    if not tpl:
        return default_text
    try:
        out = render_template(tpl, context)
    except Exception:
        return default_text
    return out if out.strip() else default_text


# Placeholders offered in the editor per screen: (token, human description). The UI shows these
# as chips the owner can click to insert; the handler must supply the same keys in its context.
CABINET_PLACEHOLDERS: list[tuple[str, str]] = [
    ("имя", "Имя пользователя"),
    ("id", "Telegram ID"),
    ("баланс", "Баланс"),
    ("друзей", "Приглашено друзей"),
    ("подписка", "Блок подписки (статус, срок, устройства)"),
    ("срок", "Дата окончания подписки"),
    ("осталось", "Сколько осталось (дни/часы)"),
    ("устройств", "Лимит устройств"),
    ("трафик", "Использование трафика"),
]

# User-level token catalogue reused across screens (same values the cabinet exposes). Building
# a per-screen subset keeps each editor's chips relevant to what that screen naturally shows.
USER_TOKEN_DESCS: dict[str, str] = {
    "имя": "Имя пользователя",
    "id": "Telegram ID",
    "баланс": "Баланс",
    "друзей": "Приглашено друзей",
    "подписка": "Блок подписки (статус, срок, устройства)",
    "срок": "Дата окончания подписки",
    "осталось": "Сколько осталось (дни/часы)",
    "устройств": "Лимит устройств",
    "трафик": "Использование трафика",
    "автопродление": "Автопродление (вкл/выкл)",
}


def _pl(*tokens: str) -> list[tuple[str, str]]:
    return [(t, USER_TOKEN_DESCS[t]) for t in tokens]


SCREEN_TEXT_PLACEHOLDERS: dict[str, list[tuple[str, str]]] = {
    "cabinet": CABINET_PLACEHOLDERS,
    "subscription": _pl(
        "имя",
        "id",
        "подписка",
        "срок",
        "осталось",
        "устройств",
        "трафик",
        "автопродление",
        "баланс",
    ),
    "connect": _pl("имя", "id", "подписка", "срок", "осталось"),
    "devices": _pl("имя", "id", "устройств", "подписка"),
    "traffic": _pl("имя", "id", "трафик", "осталось", "подписка"),
    "balance": _pl("имя", "id", "баланс"),
    "topup": _pl("имя", "id", "баланс"),
    "topup_method": _pl("имя", "id", "баланс"),
    "topup_invoice": _pl("имя", "id", "баланс"),
    "referral": _pl("имя", "id", "друзей"),
    "withdraw": _pl("имя", "id", "баланс", "друзей"),
    "trial": _pl("имя", "id"),
    "history": _pl("имя", "id", "баланс"),
    "support": _pl("имя", "id"),
    "nodes": _pl("имя", "id"),
    "proxy": _pl("имя", "id"),
    "promocode": _pl("имя", "id", "баланс"),
    "buy": _pl("имя", "id", "баланс"),
    "durations": _pl("имя", "id", "баланс"),
    "payment": _pl("имя", "id", "баланс"),
}

# The stock caption expressed as a template. Shown in the editor when the owner hasn't set their
# own text yet ("вот что бот пишет сейчас"), and used as the base for the live preview. Kept in
# sync with the layout built in handlers/actions.py::act_cabinet.
CABINET_DEFAULT_TEMPLATE = (
    "<b>👤 Профиль</b>\n"
    "\n"
    "Привет, {имя}! 👋\n"
    "ID: <code>{id}</code>\n"
    "──────────\n"
    "{подписка}\n"
    "\n"
    "💳 Баланс: <b>{баланс}</b>   ·   🎁 Друзей: <b>{друзей}</b>"
)

SCREEN_TEXT_DEFAULTS: dict[str, str] = {
    "cabinet": CABINET_DEFAULT_TEMPLATE,
    # Static prose the bot shows on each screen (token-free; live numbers are filled by the
    # bot itself and intentionally omitted here). Verbatim where the screen is fully static;
    # the empty-state / header for data-driven screens otherwise.
    "connect": (
        "<b>🔌 Подключение</b>\n"
        "\n"
        "1) Поставь приложение для VPN.\n"
        "2) Открой мини-приложение (импорт в один тап + QR)"
    ),
    "history": (
        "<b>🧾 История операций</b>\n\nПока пусто - здесь появятся все пополнения и платежи."
    ),
    "trial": (
        "<b>⭐ Пробный период активирован</b>\n"
        "\n"
        "Доступ открыт. Подключайся и тестируй без ограничений."
    ),
    "balance": (
        "<b>💳 Баланс</b>\n"
        "\n"
        "Текущий баланс: <b>{баланс}</b>\n"
        "\n"
        "Пополни любым подключённым способом - с баланса подписка оплачивается в один тап."
    ),
    "referral": (
        "<b>🎁 Приглашай друзей</b>\n"
        "\n"
        "Уже с тобой: <b>{друзей}</b> 🤝\n"
        "За каждого друга вы <b>оба</b> получаете бонусные дни подписки.\n"
        "\n"
        "Поделись своей ссылкой - она в кнопке ниже."
    ),
    "support": (
        "<b>🆘 Поддержка</b>\n\nПиши в наш саппорт-бот - оператор на связи и ответит прямо там."
    ),
    "terms": (
        "<b>📄 Пользовательское соглашение</b>\n"
        "\n"
        "Раздел пока не заполнен - задайте текст в кабинете → Настройки."
    ),
    "privacy": (
        "<b>🔒 Политика конфиденциальности</b>\n"
        "\n"
        "Раздел пока не заполнен - задайте текст в кабинете → Настройки."
    ),
    "subscription": (
        "<b>📶 Подписка</b>\n"
        "\n"
        "У тебя пока нет активной подписки.\n"
        "Оформи за пару тапов - и сразу подключайся."
    ),
    "buy": (
        "<b>🛒 Выбери тариф</b>\n"
        "\n"
        "Цена и трафик - прямо в кнопках.\n"
        "Жми подходящий, срок выберешь на следующем шаге."
    ),
    "durations": ("<b>🛒 Выбор срока</b>\n\nВыбери срок - чем длиннее, тем дешевле месяц."),
    "payment": ("<b>💳 Способ оплаты</b>\n\nВыбери, чем платишь."),
    "topup": (
        "<b>💳 Пополнение баланса</b>\n"
        "\n"
        "Выбери сумму - способ оплаты (Stars, карта, СБП…) выберешь на следующем шаге."
    ),
    "topup_method": ("<b>💳 Пополнение баланса</b>\n\nВыбери способ оплаты."),
    "topup_invoice": (
        "<b>💳 Счёт создан</b>\n"
        "\n"
        "Оплати по кнопке ниже - баланс пополнится автоматически сразу после оплаты ⚡\n"
        "Если оплатил, а баланс не изменился - жми «Проверить оплату»."
    ),
    "traffic": (
        "<b>📈 Докупить трафик</b>\n"
        "\n"
        "Гигабайты добавятся к лимиту текущей подписки сразу после оплаты - срок не меняется."
    ),
    "devices": ("<b>📱 Устройства</b>\n\nПока ни одно устройство не подключалось."),
    "nodes": ("<b>🌍 Статус серверов</b>\n\nДанные ещё собираются - загляни чуть позже."),
    "proxy": (
        "<b>🔌 MTProto-прокси</b>\n"
        "\n"
        "Один тап - и Telegram пойдёт через наш прокси. Выручает там, где мессенджер режут.\n"
        "\n"
        "Жми кнопку ниже 👇"
    ),
    "withdraw": ("<b>💸 Вывод заработка</b>\n\nОтправим всю сумму разом - выбери, куда:"),
    "promocode": ("<b>🎟 Промокод</b>\n\nПришли код одним сообщением - начислим бонус сразу."),
}

# Example values so the admin panel can render a live preview without touching the DB. These are
# illustrative only - the bot substitutes each real user's data at render time.
CABINET_SAMPLE: dict[str, str] = {
    "имя": "Иван",
    "id": "123456789",
    "баланс": "150,00 ₽",
    "друзей": "3",
    "подписка": (
        "<b>📶 Подписка активна</b>\n"
        "Действует до <b>25.08.2026</b> · осталось <b>32 дн. 5 ч.</b>\n"
        "📱 Устройств: <b>3</b>\n"
        "📈 Трафик: <b>12.4 / ∞</b>\n"
        "Автопродление: <b>вкл</b>\n"
        "Ключ-ссылка - в разделе «Моя подписка»."
    ),
    "срок": "25.08.2026",
    "осталось": "32 дн. 5 ч.",
    "устройств": "3",
    "трафик": "12.4 / ∞",
}

# Full illustrative value set for the admin preview; each screen gets the whole dict (extra
# keys are harmless - the preview only substitutes tokens that appear in the text).
_USER_SAMPLE: dict[str, str] = {**CABINET_SAMPLE, "автопродление": "вкл"}

SCREEN_TEXT_SAMPLES: dict[str, dict[str, str]] = {
    "cabinet": CABINET_SAMPLE,
    **{k: _USER_SAMPLE for k in SCREEN_TEXT_PLACEHOLDERS if k != "cabinet"},
}

# The {подписка} block is itself separately editable (active state vs no-subscription). These
# are its own placeholder catalogue + code-built defaults, mirroring the cabinet-caption system.
CABINET_SUB_PLACEHOLDERS: list[tuple[str, str]] = [
    ("срок", "Дата окончания подписки"),
    ("осталось", "Сколько осталось (дни/часы)"),
    ("устройств", "Лимит устройств"),
    ("трафик", "Использование трафика"),
    ("автопродление", "Автопродление (вкл/выкл)"),
]

# Default active block, as a template. Empty custom value ⇒ the handler's code-built block
# (which still honours SHOW_TRAFFIC_USAGE); this literal is what the editor shows as "current".
CABINET_SUB_ACTIVE_DEFAULT = (
    "<b>📶 Подписка активна</b>\n"
    "Действует до <b>{срок}</b> · осталось <b>{осталось}</b>\n"
    "📱 Устройств: <b>{устройств}</b>\n"
    "📈 Трафик: <b>{трафик}</b>\n"
    "Автопродление: <b>{автопродление}</b>\n"
    "Ключ-ссылка - в разделе «Моя подписка»."
)
CABINET_SUB_INACTIVE_DEFAULT = "<b>📶 Подписка не активна</b>\nнажми «Купить VPN» в меню."


def cabinet_sub_placeholders() -> list[dict[str, str]]:
    """Placeholder catalogue for the {подписка} sub-editors, shaped for the admin API/UI."""
    return [{"token": tok, "desc": desc} for tok, desc in CABINET_SUB_PLACEHOLDERS]


def placeholders_for(screen_key: str) -> list[dict[str, str]]:
    """Placeholder catalogue for a screen, shaped for the admin API/UI."""
    return [
        {"token": tok, "desc": desc} for tok, desc in SCREEN_TEXT_PLACEHOLDERS.get(screen_key, [])
    ]


def default_text_for(screen_key: str) -> str:
    """The stock caption template for a screen (what the bot renders with no custom text)."""
    return SCREEN_TEXT_DEFAULTS.get(screen_key, "")


def sample_for(screen_key: str) -> dict[str, str]:
    """Illustrative placeholder values for the admin live preview."""
    return dict(SCREEN_TEXT_SAMPLES.get(screen_key, {}))
