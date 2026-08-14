"""Owner-editable buttons for the built-in bot screens (safe, static-nav subset).

Most built-in screens (Подключение, История, Соглашение, Политика, Пробный, Баланс,
Реферал, Поддержка) render a fixed set of navigation buttons in code. This module lets the
owner rename / recolor / reorder / hide those buttons and add their own - WITHOUT touching
the bot's logic - by rewriting the already-built keyboard right before it's sent.

The hook lives in ``banners.render_screen``: it loads ``SCREEN_BUTTONS`` (a JSON object keyed
by screen), and if this screen has an override, ``apply_screen_buttons`` reshuffles the live
markup. No override ⇒ the markup passes through byte-for-byte, so an unconfigured bot behaves
exactly as before (important: these screens sit next to the payment flow).

Only STATIC buttons are managed - a button is identified by its callback signature (or the
web-app/url role). Dynamic buttons a handler builds from state (plan lists, autopay toggles,
device rows) are NOT in the safe registry and, if present, are preserved untouched at the end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.keyboards import style_for_hex


@dataclass(frozen=True, slots=True)
class SysButton:
    key: str  # stable identity (see button_identity) - matches the live button's signature
    label: str  # default caption, exactly as the bot renders it out of the box


@dataclass(frozen=True, slots=True)
class ScreenDef:
    key: str
    title_ru: str
    title_en: str
    buttons: tuple[SysButton, ...]  # default static buttons, in default (top-to-bottom) order


# The safe subset registers, for each screen, ONLY its static navigation buttons (back/menu
# links and fixed actions). Dynamic buttons a handler builds from state - plan lists, payment
# methods, autopay toggles, device rows, traffic packs - are intentionally absent: they are
# never matched, so apply_screen_buttons keeps them in place at the end, untouched. This is why
# even the payment-adjacent screens are safe to expose - the owner can only rename/reorder the
# static nav buttons; the money-carrying buttons pass through byte-for-byte. The cabinet itself
# is NOT here: it has its own dedicated editor.
SAFE_SCREENS: tuple[ScreenDef, ...] = (
    ScreenDef(
        "connect",
        "Подключение",
        "Connect",
        (
            SysButton("app", "📱 Открыть приложение"),
            SysButton("act:subscription", "👤 Моя подписка"),
            SysButton("nav:root", "‹ Меню"),
        ),
    ),
    ScreenDef(
        "history",
        "История операций",
        "History",
        (SysButton("act:cabinet", "‹ Кабинет"),),
    ),
    ScreenDef(
        "trial",
        "Пробный период",
        "Trial",
        (
            SysButton("act:subscription", "👤 Моя подписка"),
            SysButton("nav:root", "‹ Меню"),
        ),
    ),
    ScreenDef(
        "balance",
        "Баланс",
        "Balance",
        (
            SysButton("topup:menu", "💳 Пополнить"),
            SysButton("act:support", "🆘 Поддержка"),
            SysButton("act:cabinet", "‹ Кабинет"),
        ),
    ),
    ScreenDef(
        "referral",
        "Пригласить друга",
        "Referral",
        (
            SysButton("url", "📤 Поделиться"),
            SysButton("withdraw:start", "💸 Вывести"),
            SysButton("act:cabinet", "‹ Кабинет"),
        ),
    ),
    ScreenDef(
        "support",
        "Поддержка",
        "Support",
        (
            SysButton("url", "💬 Открыть поддержку"),
            SysButton("app", "💬 Открыть поддержку"),  # miniapp-mode support opens as a WebApp
            SysButton("act:cabinet", "‹ Кабинет"),
        ),
    ),
    ScreenDef(
        "terms",
        "Пользовательское соглашение",
        "Terms",
        (SysButton("nav:root", "‹ Меню"),),
    ),
    ScreenDef(
        "privacy",
        "Политика конфиденциальности",
        "Privacy",
        (SysButton("nav:root", "‹ Меню"),),
    ),
    ScreenDef(
        "subscription",
        "Моя подписка",
        "My subscription",
        (
            SysButton("act:connect", "🔌 Подключить"),
            SysButton("plan", "🔄 Продлить"),
            SysButton("act:devices", "📱 Устройства"),
            SysButton("act:buy", "🔀 Сменить тариф"),
            SysButton("traffic:menu", "➕ Трафик"),
            SysButton("autopay:toggle", "🔁 Автопродление"),  # live ✅/❌ state kept at render
            SysButton("autopay:card", "💳 Автосписание картой"),  # only when autopay is on
            # appended when SUBSCRIPTION_MINI_APP_URL is set
            SysButton("app", "📱 Открыть приложение"),
            SysButton("nav:root", "‹ Меню"),
        ),
    ),
    ScreenDef(
        "buy",
        "Тарифы",
        "Plans",
        (SysButton("nav:root", "‹ Меню"),),
    ),
    ScreenDef(
        "durations",
        "Выбор срока",
        "Duration",
        (SysButton("act:buy", "‹ Назад"),),
    ),
    ScreenDef(
        "payment",
        "Способ оплаты",
        "Payment",
        (SysButton("act:promocode", "🎟 У меня промокод"),),
    ),
    ScreenDef(
        "topup",
        "Пополнение баланса: сумма",
        "Top up: amount",
        (SysButton("act:balance", "‹ Назад"),),
    ),
    ScreenDef(
        # Was folded into "topup" (three different renders sharing one key — an owner's saved
        # text/buttons for the amount screen leaked onto method selection and vice versa).
        # Split into its own key: method selection is a distinct screen in the funnel.
        "topup_method",
        "Пополнение баланса: способ",
        "Top up: method",
        (SysButton("topup:menu", "‹ Назад"),),
    ),
    ScreenDef(
        # Also split out of "topup" — the "invoice created" screen used to inherit both the
        # amount screen's saved text (losing "Счёт создан") and its custom buttons (which would
        # render ABOVE the real "Оплатить" button). Only the static back-to-menu button is in the
        # safe registry here, same as "devices": the "Оплатить" url + "Проверить оплату" buttons
        # carry a per-invoice url/payment_id and are never reusable across renders.
        "topup_invoice",
        "Пополнение баланса: счёт",
        "Top up: invoice",
        (SysButton("nav:root", "‹ Меню"),),
    ),
    ScreenDef(
        "traffic",
        "Докупить трафик",
        "Traffic",
        (SysButton("act:subscription", "‹ Назад"),),
    ),
    ScreenDef(
        "devices",
        "Устройства",
        "Devices",
        (SysButton("nav:root", "‹ Меню"),),
    ),
    ScreenDef(
        "nodes",
        "Статус серверов",
        "Servers",
        (SysButton("nav:root", "‹ Меню"),),
    ),
    ScreenDef(
        "proxy",
        "MTProto-прокси",
        "MTProto proxy",
        (
            SysButton("url", "🔌 Подключить прокси"),
            SysButton("nav:root", "‹ Меню"),
        ),
    ),
    ScreenDef(
        "withdraw",
        "Вывод заработка",
        "Withdraw",
        (SysButton("act:referral", "‹ Назад"),),
    ),
    ScreenDef(
        "promocode",
        "Промокод",
        "Promo code",
        (SysButton("act:cabinet", "‹ Кабинет"),),
    ),
)

SCREENS_BY_KEY: dict[str, ScreenDef] = {s.key: s for s in SAFE_SCREENS}

# Buttons whose caption carries a live state glyph (✅/❌) the handler appends from state.
# When the owner renames such a button, we keep the trailing glyph so the indicator, which is
# the whole point of the button, never freezes to a stale value.
_DYNAMIC_STATE_KEYS: frozenset[str] = frozenset({"autopay:toggle", "autopay:card"})


def is_safe_screen(key: str) -> bool:
    return key in SCREENS_BY_KEY


def button_identity(btn: InlineKeyboardButton) -> str:
    """Stable identity for a live button, so an override can find it across renders.

    web_app -> "app"; a callback -> the callback with a trailing numeric node-id stripped
    ("act:cabinet:0" -> "act:cabinet", "nav:root" stays); a url button -> "url" (these screens
    carry at most one). Callers de-duplicate collisions with a "#n" suffix.
    """
    if getattr(btn, "web_app", None) is not None:
        return "app"
    cb = getattr(btn, "callback_data", None)
    if cb:
        parts = cb.split(":")
        if len(parts) >= 2 and parts[-1].isdigit():
            parts = parts[:-1]
        return ":".join(parts)
    if getattr(btn, "url", None) is not None:
        return "url"
    return "text"


def _identities(buttons: list[InlineKeyboardButton]) -> list[str]:
    """Per-button identities with "#n" suffixes disambiguating same-signature duplicates."""
    out: list[str] = []
    counts: dict[str, int] = {}
    for b in buttons:
        base = button_identity(b)
        counts[base] = counts.get(base, 0) + 1
        out.append(base if counts[base] == 1 else f"{base}#{counts[base]}")
    return out


def _restyle(btn: InlineKeyboardButton, item: dict[str, Any]) -> InlineKeyboardButton:
    """A copy of a live (system) button with the owner's label/color/icon applied."""
    upd: dict[str, object] = {}
    label = str(item.get("label") or "").strip()
    if label:
        # Keep the live ✅/❌ state glyph the handler put on autopay-style buttons, so a rename
        # relabels only the base text and the indicator stays accurate render-to-render.
        if button_identity(btn) in _DYNAMIC_STATE_KEYS:
            live = btn.text or ""
            if live and live[-1] in ("✅", "❌") and label[-1] not in ("✅", "❌"):
                label = f"{label}: {live[-1]}"
        upd["text"] = label
    color = str(item.get("color") or "").strip()
    if color:
        style = style_for_hex(color)
        if style:
            upd["style"] = style
    icon = str(item.get("icon") or "").strip()
    if icon.isdigit():
        upd["icon_custom_emoji_id"] = icon
    return btn.model_copy(update=upd) if upd else btn


def _build_custom(item: dict[str, Any]) -> InlineKeyboardButton | None:
    """A brand-new button the owner added: points at a bot action or an external URL."""
    label = str(item.get("label") or "").strip()
    if not label:
        return None
    kwargs: dict[str, object] = {"text": label}
    color = str(item.get("color") or "").strip()
    if color:
        style = style_for_hex(color)
        if style:
            kwargs["style"] = style
    icon = str(item.get("icon") or "").strip()
    if icon.isdigit():
        kwargs["icon_custom_emoji_id"] = icon
    url = str(item.get("url") or "").strip()
    action = str(item.get("action") or "").strip().lower()
    if url and url.startswith(("http://", "https://", "tg://")):
        kwargs["url"] = url
    elif action:
        kwargs["callback_data"] = f"act:{action}:0"
    else:
        return None
    return InlineKeyboardButton(**kwargs)  # type: ignore[arg-type]


def _group_rows(
    ordered: list[tuple[InlineKeyboardButton, object]], per_row: object, layout: object
) -> list[list[InlineKeyboardButton]]:
    """Chunk placed buttons into physical rows: custom = group by row index; uniform = per_row."""
    if layout == "custom":
        rows: list[list[InlineKeyboardButton]] = []
        cur: int | None = None
        started = False
        for btn, r in ordered:
            rr = r if isinstance(r, int) else 0
            if not started or rr != cur or len(rows[-1]) >= 3:
                rows.append([])
                cur = rr
                started = True
            rows[-1].append(btn)
        return rows
    width = per_row if per_row in (1, 2, 3) else 1
    btns = [b for b, _ in ordered]
    return [btns[i : i + width] for i in range(0, len(btns), width)]


def apply_screen_buttons(
    screen_key: str, markup: InlineKeyboardMarkup, cfg: dict[str, Any] | None
) -> InlineKeyboardMarkup:
    """Rewrite a screen's keyboard per the owner's override. No override ⇒ return it unchanged.

    Never drops a button the config didn't mention (a conditional/dynamic one) - those are kept
    in place at the end, so a mis-scoped override can only reorder/relabel, never brick a screen.
    """
    if not cfg or not isinstance(cfg.get("items"), list) or not cfg["items"]:
        return markup
    live = [b for row in markup.inline_keyboard for b in row]
    ids = _identities(live)
    by_id = dict(zip(ids, live, strict=True))

    ordered: list[tuple[InlineKeyboardButton, object]] = []
    for it in cfg["items"]:
        if not it.get("enabled", True):
            continue  # hidden (system) or disabled (custom) - leave it out
        if it.get("custom"):
            btn = _build_custom(it)
            if btn is not None:
                ordered.append((btn, it.get("row")))
        else:
            b = by_id.get(str(it.get("key")))
            if b is None:
                continue  # a conditional button that isn't in this particular render
            ordered.append((_restyle(b, it), it.get("row")))

    # Safety net: any live button the config never referenced (e.g. a newly added dynamic one)
    # is appended untouched so nothing silently vanishes.
    referenced = {str(it.get("key")) for it in cfg["items"] if not it.get("custom")}
    for k, b in zip(ids, live, strict=True):
        if k not in referenced:
            ordered.append((b, None))

    rows = _group_rows(ordered, cfg.get("per_row"), cfg.get("layout"))
    rows = [r for r in rows if r]
    if not rows:
        return markup  # never send an empty keyboard - fall back to the original
    return InlineKeyboardMarkup(inline_keyboard=rows)


def load_screen_configs(raw: str | None) -> dict[str, dict[str, Any]]:
    """Parse the ``SCREEN_BUTTONS`` blob (a JSON object keyed by screen) into a dict."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_screen_texts(raw: str | None) -> dict[str, str]:
    """Parse the ``SCREEN_TEXTS`` blob (a JSON object keyed by screen) into {key: template}."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}
