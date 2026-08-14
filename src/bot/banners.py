"""Resolve the banner photo for a bot screen.

Every screen shows a photo above its caption. A screen key maps to a config key
(``BANNER_<AREA>``); resolution falls through: the screen's own banner -> ``BANNER_DEFAULT``
-> ``WELCOME_IMAGE`` -> the bundled ``assets/banner.png`` that ships with the app. The owner
sets any of these from the cabinet (upload/URL/file_id) or from the bot via ``/setbanner``.
When ``BANNER_ENABLED`` is off, screens render as plain text (no photo).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile, InlineKeyboardMarkup

from src.bot.screen import show_media_screen
from src.bot.screen_buttons import apply_screen_buttons, load_screen_configs
from src.bot.screen_text import _TOKEN, caption_for

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from src.infrastructure.di import AppContainer

# The banner that ships in the repo (deploys with the app, unlike runtime uploads/).
_BUNDLED = Path(__file__).resolve().parent / "assets" / "banner.png"

# Screen key -> config key. Screens sharing a funnel share a banner; anything not listed
# falls back to BANNER_DEFAULT. Keys here must exist in the config registry.
_SCREEN_BANNER_KEY: dict[str, str] = {
    "menu": "BANNER_MENU",
    "buy": "BANNER_BUY",
    "durations": "BANNER_BUY",
    "payment": "BANNER_BUY",
    "cabinet": "BANNER_CABINET",
    "subscription": "BANNER_SUBSCRIPTION",
    "connect": "BANNER_SUBSCRIPTION",
    "devices": "BANNER_SUBSCRIPTION",
    "traffic": "BANNER_TRAFFIC",
    "balance": "BANNER_BALANCE",
    "topup": "BANNER_BALANCE",
    "topup_method": "BANNER_BALANCE",
    "topup_invoice": "BANNER_BALANCE",
    "referral": "BANNER_REFERRAL",
    "support": "BANNER_SUPPORT",
    "trial": "BANNER_TRIAL",
}

# Screen key -> the config key the owner edits, exposed so /setbanner accepts the same names.
SCREEN_KEYS: tuple[str, ...] = tuple(_SCREEN_BANNER_KEY)


def banner_config_key(screen_key: str) -> str:
    """The BANNER_* config key a screen (or 'default') resolves to — used by /setbanner."""
    if screen_key in ("default", "all", ""):
        return "BANNER_DEFAULT"
    return _SCREEN_BANNER_KEY.get(screen_key, "BANNER_DEFAULT")


async def banner_for(container: AppContainer, screen_key: str) -> str | FSInputFile | None:
    """Media reference for ``screen_key``: the configured banner (a photo OR a GIF/MP4), else a
    fallback, else None. Returned raw — the send helpers (media_input/is_animated) resolve it,
    so an animation banner is sent with send_animation instead of a frozen send_photo."""
    async with container.uow() as uow:
        cfg = container.bot_config
        if not bool(await cfg.value(uow, "BANNER_ENABLED")):
            return None
        mode = str(await cfg.value(uow, "BANNER_MODE") or "one")
        # «one» — one image everywhere: use only BANNER_DEFAULT, ignore per-screen banners.
        # «per_screen» — the screen's own banner, falling back to default.
        if mode == "one":
            candidates: tuple[str | None, ...] = ("BANNER_DEFAULT", "WELCOME_IMAGE")
        else:
            candidates = (_SCREEN_BANNER_KEY.get(screen_key), "BANNER_DEFAULT", "WELCOME_IMAGE")
        for key in candidates:
            if not key:
                continue
            ref = str(await cfg.value(uow, key) or "").strip()
            if ref:
                return ref
    return FSInputFile(str(_BUNDLED)) if _BUNDLED.is_file() else None


async def _apply_overrides(
    container: AppContainer,
    screen_key: str,
    caption: str,
    markup: InlineKeyboardMarkup | None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Apply the owner's per-screen text/button overrides. Both no-op when unconfigured.

    Text: a static template replaces the caption; a template with unfilled {token}
    placeholders is left to the code-built caption (which already carries the live data),
    so a data screen never shows a raw {token}. Buttons: apply_screen_buttons rewrites the
    keyboard, keeping any button the config didn't mention — an unset screen is byte-for-byte.
    """
    async with container.uow() as uow:
        cfg = container.bot_config
        texts_raw = str(await cfg.value(uow, "SCREEN_TEXTS") or "")
        buttons_raw = str(await cfg.value(uow, "SCREEN_BUTTONS") or "")
    if texts_raw.strip():
        try:
            import json

            texts = json.loads(texts_raw)
            tpl = texts.get(screen_key) if isinstance(texts, dict) else None
            if isinstance(tpl, str) and tpl.strip():
                candidate = caption_for(tpl, caption, {})
                if not _TOKEN.search(candidate):
                    caption = candidate
        except Exception:
            pass
    if markup is not None:
        try:
            configs = load_screen_configs(buttons_raw)
            if configs.get(screen_key):
                markup = apply_screen_buttons(screen_key, markup, configs[screen_key])
        except Exception:
            pass
    return caption, markup


async def render_screen(
    target: CallbackQuery | Message,
    container: AppContainer,
    screen_key: str,
    caption: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Resolve ``screen_key``'s banner and render the caption + buttons over it (one call).

    ``target`` is an inline tap (CallbackQuery — edit in place) or a reply-keyboard tap /
    command (Message — send fresh), so the same handlers render from both entry points.
    """
    caption, markup = await _apply_overrides(container, screen_key, caption, markup)
    photo = await banner_for(container, screen_key)
    await show_media_screen(target, photo, caption, markup)
