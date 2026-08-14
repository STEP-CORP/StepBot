"""Navigation + built-in actions: subscription, balance, referral, trial, support."""

from __future__ import annotations

import contextlib
from html import escape as hesc
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.application.dto.pricing import PurchaseRequest
from src.application.services.connection import connection_apps, parse_enabled_apps
from src.bot.banners import render_screen
from src.bot.gate import ensure_channel
from src.bot.keyboards import menu_keyboard, simple_keyboard, webapp_button
from src.bot.media import answer_media
from src.bot.menu_render import send_main_menu
from src.bot.screen import ack, safe_answer, show_screen
from src.core.enums import Currency, PurchaseType, TransactionStatus, TransactionType
from src.core.exceptions import RemnawaveError
from src.core.logging import get_logger
from src.infrastructure.database.models.plan import Plan
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer

log = get_logger(__name__)

router = Router(name="actions")

GIB = 1024**3


def fmt_money(minor: int) -> str:
    v = minor / 100
    return f"{v:,.0f} ₽".replace(",", " ") if v == int(v) else f"{v:,.2f} ₽".replace(",", " ")


# --- navigation over admin-built screens -------------------------------------


@router.callback_query(F.data == "nav:root")
async def nav_root(
    cb: CallbackQuery, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    # The menu button must abort any pending form (promocode/ticket input) — otherwise the
    # stale FSM state silently eats the user's next message.
    await state.clear()
    await send_main_menu(cb, container, db_user)


@router.callback_query(F.data.startswith("nav:"))
async def nav_screen(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    node_id = int(parts[1]) if parts[1].isdigit() else 0
    async with container.uow() as uow:
        nodes = list(await uow.menu_nodes.tree())
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
    node = next((n for n in nodes if n.id == node_id), None)
    if node is None:
        await send_main_menu(cb, container, db_user)
        return
    if len(parts) > 2 and parts[2] == "up":
        # back button: show the parent screen (or root)
        parent = next((n for n in nodes if n.id == node.parent_id), None)
        if parent is None:
            await send_main_menu(cb, container, db_user)
            return
        node = parent
    text = node.payload or node.label
    markup = menu_keyboard(nodes, node.id, miniapp_url=miniapp_url or None, with_back=True)
    msg = cb.message if isinstance(cb.message, Message) else None
    if msg is not None and node.image_path:
        # Screens with media: send a fresh message (can't edit text->media). The media may be a
        # local uploads/ file, a Telegram file_id or a URL — and a GIF/MP4 animates (answer_media).
        try:
            await answer_media(
                msg,
                node.image_path,  # answer_media resolves the ref (file/URL/file_id, still or GIF)
                # Telegram caps photo captions at 1024 chars (text screens allow 4096).
                caption=text[:1024],
                reply_markup=markup,
                parse_mode=None,
            )
        except Exception:
            pass  # bad/unreachable image ref -> fall through to a text screen
        else:
            with contextlib.suppress(Exception):
                await msg.delete()
            await ack(cb)
            return
    await show_screen(cb, text, markup, parse_mode=None)
    await ack(cb)


# --- built-in actions ---------------------------------------------------------


@router.callback_query(F.data.startswith("act:subscription"))
async def act_subscription(
    cb: CallbackQuery | Message, container: AppContainer, db_user: User
) -> None:
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        hide_link = bool(await container.bot_config.value(uow, "HIDE_SUBSCRIPTION_LINK"))
        show_traffic = bool(await container.bot_config.value(uow, "SHOW_TRAFFIC_USAGE"))
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        autopay_global = bool(await container.bot_config.value(uow, "AUTO_RENEWAL_ENABLED"))
        if sub is not None and sub.status.is_usable and show_traffic:
            await container.traffic.refresh_used_bytes(sub)
            await uow.commit()
    if sub is None or not sub.status.is_usable:
        text = (
            "<b>📶 Подписка</b>\n\n"
            "У тебя пока нет активной подписки.\n"
            "Оформи за пару тапов — и сразу подключайся."
        )
        markup = simple_keyboard([("🛒 Купить VPN", "act:buy:0"), ("‹ Меню", "nav:root")])
    else:
        days_left = ""
        if sub.expire_at is not None:
            import datetime as dt

            left = max(0, (sub.expire_at - dt.datetime.now(dt.UTC)).days)
            days_left = f"\n⏳ Осталось: <b>{left} дн.</b> · до <b>{sub.expire_at:%d.%m.%Y}</b>"
        traffic = f"{sub.traffic_used_bytes / GIB:.1f} / " + (
            f"{sub.traffic_limit_bytes / GIB:.0f} ГБ" if sub.traffic_limit_bytes else "∞"
        )
        plan_name = hesc(str((sub.plan_snapshot or {}).get("name", "—")))
        text = f"<b>📶 Твоя подписка</b>\n\n🏷 Тариф: <b>{plan_name}</b>{days_left}"
        if show_traffic:  # SHOW_TRAFFIC_USAGE toggle now actually hides the line (SHOWTRAF-1)
            text += f"\n📈 Трафик: <b>{traffic}</b>"
        if not hide_link and sub.subscription_url:
            text += f"\n──────────\nСсылка подписки:\n<code>{sub.subscription_url}</code>"
        kb: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🔌 Подключить", callback_data="act:connect:0")],
            [
                InlineKeyboardButton(text="🔄 Продлить", callback_data=f"plan:{sub.plan_id or 0}"),
                InlineKeyboardButton(text="📱 Устройства", callback_data="act:devices:0"),
            ],
            [
                InlineKeyboardButton(text="🔀 Сменить тариф", callback_data="act:buy:0"),
                InlineKeyboardButton(text="➕ Трафик", callback_data="traffic:menu"),
            ],
        ]
        if autopay_global:
            mark = "✅" if sub.autopay_enabled else "❌"
            kb.append(
                [
                    InlineKeyboardButton(
                        text=f"🔁 Автопродление: {mark}", callback_data="autopay:toggle"
                    )
                ]
            )
            if sub.autopay_enabled:
                card_mark = "✅" if sub.autopay_card_enabled else "❌"
                kb.append(
                    [
                        InlineKeyboardButton(
                            text=f"💳 Автосписание картой: {card_mark}",
                            callback_data="autopay:card",
                        )
                    ]
                )
        if miniapp_url.startswith("https://"):
            kb.append([webapp_button("📱 Открыть приложение", miniapp_url)])
        kb.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")])
        markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await render_screen(cb, container, "subscription", text, markup)
    await safe_answer(cb)  # autopay_toggle chains here after answering


@router.callback_query(F.data == "autopay:toggle")
async def autopay_toggle(
    cb: CallbackQuery | Message, container: AppContainer, db_user: User
) -> None:
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        if sub is None:
            await ack(cb, "Нет активной подписки", alert=True)
            return
        sub.autopay_enabled = not sub.autopay_enabled
        await uow.commit()
        enabled = sub.autopay_enabled
    await ack(cb, "Автопродление включено ✅" if enabled else "Автопродление выключено ❌")
    await act_subscription(cb, container, db_user)


@router.callback_query(F.data == "autopay:card")
async def autopay_card_toggle(
    cb: CallbackQuery | Message, container: AppContainer, db_user: User
) -> None:
    """Opt-in to charge the saved card when the balance can't cover the renewal."""
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        if sub is None:
            await ack(cb, "Нет активной подписки", alert=True)
            return
        card_title = None
        if not sub.autopay_card_enabled:  # turning ON requires a saved card
            user = await uow.users.get(db_user.id)
            if user is None or not user.saved_payment_method_id:
                await ack(
                    cb,
                    "Карта ещё не сохранена. Оплати подписку картой через ЮKassa — "
                    "она привяжется автоматически, и автосписание станет доступно.",
                    alert=True,
                )
                return
            card_title = user.saved_payment_method_title
        sub.autopay_card_enabled = not sub.autopay_card_enabled
        await uow.commit()
        enabled = sub.autopay_card_enabled
    if enabled:
        card = f" ({card_title})" if card_title else ""
        await ack(cb, f"Автосписание картой{card} включено ✅")
    else:
        await ack(cb, "Автосписание картой выключено ❌")
    await act_subscription(cb, container, db_user)


@router.callback_query(F.data.startswith("act:cabinet"))
async def act_cabinet(
    cb: CallbackQuery | Message, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    """Личный кабинет — one screen: profile, balance, subscription, referral, quick actions."""
    import datetime as dt

    # The cabinet is the back-target for the promocode/support screens, which arm an FSM
    # form (PromoForm.waiting_code) expecting the next text message. Backing out here must
    # clear that form, else the user's next message is swallowed as a promo code (nav_root
    # already clears; the cabinet must too, since v1.2.14 pointed those backs here).
    await state.clear()

    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        invited = await uow.users.count(referred_by_id=db_user.id)
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        balance_on = bool(await container.bot_config.value(uow, "BALANCE_ENABLED"))
        referral_on = bool(await container.bot_config.value(uow, "REFERRAL_ENABLED"))
        show_traffic = bool(await container.bot_config.value(uow, "SHOW_TRAFFIC_USAGE"))
        cabinet_cfg = str(await container.bot_config.value(uow, "CABINET_BUTTONS") or "")
        cabinet_custom = str(await container.bot_config.value(uow, "CABINET_CUSTOM_BUTTONS") or "")
        # Owner-editable cabinet text templates (empty config -> built-in default).
        tpl_main = str(await container.bot_config.value(uow, "CABINET_TEXT") or "")
        tpl_active = str(await container.bot_config.value(uow, "CABINET_SUB_ACTIVE") or "")
        tpl_inactive = str(await container.bot_config.value(uow, "CABINET_SUB_INACTIVE") or "")
        cabinet_emoji = str(await container.bot_config.value(uow, "CABINET_TEXT_EMOJI") or "")
        if sub is not None and sub.status.is_usable and show_traffic:
            await container.traffic.refresh_used_bytes(sub)
            await uow.commit()

    from src.bot.cabinet_text import (
        DEFAULT_CABINET_TEXT,
        DEFAULT_SUB_ACTIVE,
        DEFAULT_SUB_INACTIVE,
        apply_custom_emoji,
        render_cabinet_text,
    )

    is_active = sub is not None and sub.status.is_usable
    values: dict[str, object] = {
        "имя": hesc(db_user.first_name or db_user.username or "друг"),
        "id": db_user.telegram_id,
        "баланс": fmt_money(db_user.balance_minor),
        "друзей": invited,
    }
    if is_active and sub is not None:
        now = dt.datetime.now(dt.UTC)
        values["срок"] = sub.expire_at.strftime("%d.%m.%Y") if sub.expire_at else "—"
        if sub.expire_at is not None:
            # Clamp on total seconds: a negative timedelta has .seconds in [0,86400), which
            # would render a bogus positive "23 ч." for an already-expired sub (CAB-1).
            secs = max(0, int((sub.expire_at - now).total_seconds()))
            d_left, h_left = secs // 86400, (secs % 86400) // 3600
            values["осталось"] = f"{d_left} дн. {h_left} ч." if d_left else f"{h_left} ч."
        else:
            values["осталось"] = "—"
        values["устройств"] = sub.device_limit or "—"
        # None -> the whole «Трафик» line is dropped when the owner turned traffic display off.
        values["трафик"] = (
            f"{sub.traffic_used_bytes / GIB:.1f} / "
            + (f"{sub.traffic_limit_bytes / GIB:.0f} ГБ" if sub.traffic_limit_bytes else "∞")
            if show_traffic
            else None
        )
        values["автопродление"] = "вкл" if sub.autopay_enabled else "выкл"

    text = render_cabinet_text(
        main=tpl_main or DEFAULT_CABINET_TEXT,
        sub_active=tpl_active or DEFAULT_SUB_ACTIVE,
        sub_inactive=tpl_inactive or DEFAULT_SUB_INACTIVE,
        is_active=is_active,
        values=values,
    )
    text = apply_custom_emoji(text, cabinet_emoji)

    # The cabinet is the account hub — everything about the user's account, and the one
    # place «Поддержка» lives (the main menu stays lean). «Подключить»/«Купить» are the
    # primary actions up in the main menu, so they're not duplicated here.
    # Owner-configurable button set (CABINET_BUTTONS): order + which buttons show. A disabled
    # feature (balance/referral) is still skipped even when listed, so «отключил в настройках»
    # actually hides it. The grid reflows. See src/bot/cabinet_menu.py.
    from src.bot.cabinet_menu import cabinet_rows, parse_custom_buttons

    # «Открыть приложение» (mini-app) and «Назад» are now owner-editable list buttons — both come
    # in via `rows`, no hardcoded appends. cabinet_rows renders «app» as a web_app button from
    # miniapp_url (and skips it when unset); parse_config keeps «back» present so the cabinet can't
    # lose its only exit even on a config that predates these buttons.
    rows = cabinet_rows(
        cabinet_cfg,
        flags={"BALANCE_ENABLED": balance_on, "REFERRAL_ENABLED": referral_on},
        miniapp_url=miniapp_url,
    )

    def _cab_btn(text: str, cb: str, extra: dict[str, Any]) -> InlineKeyboardButton:
        # link/miniapp buttons carry url/web_app in extra and MUST NOT also set callback_data.
        if "url" in extra or "web_app" in extra:
            return InlineKeyboardButton(text=text, **extra)
        return InlineKeyboardButton(text=text, callback_data=cb, **extra)

    kb: list[list[InlineKeyboardButton]] = [[_cab_btn(t, c, x) for t, c, x in row] for row in rows]
    # Back-compat: installs that configured the legacy CABINET_CUSTOM_BUTTONS keep their link
    # buttons after the unified rows (new custom buttons live in CABINET_BUTTONS, different key,
    # so nothing renders twice).
    for btn in parse_custom_buttons(cabinet_custom):
        kb.append([InlineKeyboardButton(text=btn["label"], url=btn["url"])])
    await render_screen(cb, container, "cabinet", text, InlineKeyboardMarkup(inline_keyboard=kb))
    await ack(cb)


@router.callback_query(F.data.startswith("cabsub:"))
async def act_cabsub(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """Owner-defined cabinet sub-screen (Подменю): the button's stored text + back to cabinet.

    Opt-in: only custom cabinet buttons the owner set to type «Подменю» reach here. A missing or
    non-screen key just bounces back silently, so a stale callback never errors.
    """
    from src.bot.cabinet_menu import parse_config
    from src.bot.screen_text import caption_for

    key = (cb.data or "").split(":", 1)[1]
    async with container.uow() as uow:
        cabinet_cfg = str(await container.bot_config.value(uow, "CABINET_BUTTONS") or "")
    item = next(
        (
            it
            for it in parse_config(cabinet_cfg)
            if it.get("custom") and it.get("key") == key and it.get("btype") == "screen"
        ),
        None,
    )
    if item is None:
        await ack(cb)
        return
    name = hesc(db_user.first_name or db_user.username or "друг")
    caption = caption_for(
        str(item.get("stext") or ""),
        str(item.get("label") or "…"),
        {"имя": name, "id": str(db_user.telegram_id)},
    )
    await render_screen(
        cb, container, key, caption, simple_keyboard([("‹ Кабинет", "act:cabinet:0")])
    )
    await ack(cb)


@router.callback_query(F.data.startswith("act:connect"))
async def act_connect(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    """Mini-app-parity Connect screen: subscription URL + per-client import links + WebApp."""
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        hide_link = bool(await container.bot_config.value(uow, "HIDE_SUBSCRIPTION_LINK"))
        # Show only the clients the owner enabled (CONNECTION_APPS), same as the mini-app —
        # otherwise this screen dumped all nine import links as a wall of text and named
        # apps the owner never offered, which is what scared users on the Connect screen.
        enabled = parse_enabled_apps(str(await container.bot_config.value(uow, "CONNECTION_APPS")))
    if sub is None or not sub.status.is_usable or not sub.subscription_url:
        await ack(cb, "Сначала оформи подписку", alert=True)
        return
    chosen = connection_apps(sub.subscription_url, sub.crypto_link, enabled)
    names = ", ".join(a["label"] for a in chosen)
    has_miniapp = miniapp_url.startswith("https://")
    # The raw happ://add/… / incy://add/… deep-link dump was removed: shown as <code> it only
    # confused people — tapping copies the whole scheme-prefixed string, which does NOT work
    # when pasted into an app's "add subscription" field. The plain subscription URL below is
    # what every client accepts. One-tap import lives in the mini-app. Honors HIDE_SUB_LINK (#5).
    url_line = "" if hide_link else f"\n\n<code>{sub.subscription_url}</code>"
    if has_miniapp:
        step2 = "2) Открой мини-приложение — импорт в один тап и QR-код."
        if not hide_link:
            step2 += (
                "\n\nИли вручную: скопируй ссылку ниже и вставь её в приложении в "
                f"«Добавить подписку»:{url_line}"
            )
    elif not hide_link:
        step2 = f"2) Скопируй ссылку и вставь её в приложении в «Добавить подписку»:{url_line}"
    else:
        step2 = "2) Нажми «Моя подписка», чтобы получить ссылку для подключения."
    text = f"<b>🔌 Подключение</b>\n\n1) Поставь приложение: {names}.\n{step2}"
    kb: list[list[InlineKeyboardButton]] = []
    if miniapp_url.startswith("https://"):
        kb.append([webapp_button("📱 Открыть приложение", miniapp_url)])
    kb.append([InlineKeyboardButton(text="👤 Моя подписка", callback_data="act:subscription:0")])
    kb.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")])
    await render_screen(cb, container, "connect", text, InlineKeyboardMarkup(inline_keyboard=kb))
    await ack(cb)


_TXN_LABEL: dict[TransactionType, str] = {
    TransactionType.DEPOSIT: "Пополнение",
    TransactionType.SUBSCRIPTION_PAYMENT: "Подписка",
    TransactionType.REFERRAL_REWARD: "Реф. бонус",
    TransactionType.REFUND: "Возврат",
    TransactionType.WITHDRAWAL: "Вывод",
    TransactionType.GIFT: "Подарок",
}
_TXN_STATUS_EMOJI: dict[TransactionStatus, str] = {
    TransactionStatus.COMPLETED: "✅",
    TransactionStatus.PENDING: "⏳",
    TransactionStatus.CANCELED: "✖️",
    TransactionStatus.FAILED: "❌",
    TransactionStatus.REFUNDED: "↩️",
}


@router.callback_query(F.data.startswith("act:history"))
async def act_history(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    async with container.uow() as uow:
        txns = await uow.transactions.list_recent(db_user.id, limit=10)
    if not txns:
        text = "<b>🧾 История операций</b>\n\nПока пусто — здесь появятся все пополнения и платежи."
    else:
        lines = [
            f"{_TXN_STATUS_EMOJI.get(t.status, '')} {t.created_at:%d.%m} · "
            f"{_TXN_LABEL.get(t.type, t.type.value)} · {fmt_money(t.amount_minor)}"
            for t in txns
        ]
        text = "<b>🧾 История операций</b>\n\n" + "\n".join(lines)
    await render_screen(
        cb, container, "history", text, simple_keyboard([("‹ Кабинет", "act:cabinet:0")])
    )
    await ack(cb)


# Owner-editable legal documents (text set in the cabinet). The owner adds a menu button with
# the matching action (terms / privacy) in the constructor; the text renders here.
_LEGAL_DOCS: dict[str, tuple[str, str]] = {
    "terms": ("📄 Пользовательское соглашение", "TERMS_TEXT"),
    "privacy": ("🔒 Политика конфиденциальности", "PRIVACY_TEXT"),
}


async def _show_legal(cb: CallbackQuery | Message, container: AppContainer, code: str) -> None:
    title, key = _LEGAL_DOCS[code]
    async with container.uow() as uow:
        body = str(await container.bot_config.value(uow, key) or "").strip()
    header = f"<b>{title}</b>\n\n"
    if not body:
        text = header + "Раздел пока не заполнен — задайте текст в кабинете → Настройки."
    else:
        text = header + body[: 4096 - len(header) - 1]  # Telegram caps a message at 4096 chars
    await render_screen(cb, container, code, text, simple_keyboard([("‹ Меню", "nav:root")]))
    await ack(cb)


@router.callback_query(F.data.startswith("act:terms"))
async def act_terms(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    await _show_legal(cb, container, "terms")


@router.callback_query(F.data.startswith("act:privacy"))
async def act_privacy(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    await _show_legal(cb, container, "privacy")


@router.callback_query(F.data.startswith("act:balance"))
async def act_balance(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            # Reachable from old messages / custom menus after the owner turned the
            # wallet off — depositing into a wallet nothing accepts strands the money.
            await ack(cb, "Баланс отключён", alert=True)
            return
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
    text = (
        "<b>💳 Баланс</b>\n\n"
        f"На счету: <b>{fmt_money(db_user.balance_minor)}</b>\n\n"
        f"Пополни от <b>{fmt_money(min_dep)}</b> — Stars, картой, СБП и другими способами.\n"
        "С баланса подписка оплачивается в один тап."
    )
    markup = simple_keyboard(
        [
            ("💳 Пополнить", "topup:menu"),
            ("🆘 Поддержка", "act:support:0"),
            ("‹ Кабинет", "act:cabinet:0"),
        ]
    )
    await render_screen(cb, container, "balance", text, markup)
    await ack(cb)


@router.callback_query(F.data.startswith("act:referral"))
async def act_referral(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    async with container.uow() as uow:
        cfg = container.bot_config
        enabled = bool(await cfg.value(uow, "REFERRAL_ENABLED"))
        bonus_days = int(await cfg.value(uow, "REFERRAL_BONUS_DAYS"))
        bot_username = str(await cfg.value(uow, "BOT_USERNAME") or "")
        invited = await uow.users.count(referred_by_id=db_user.id)
        withdrawals_on = bool(await cfg.value(uow, "REFERRAL_WITHDRAWAL_ENABLED"))
    if not enabled:
        await ack(cb, "Реферальная программа отключена", alert=True)
        return
    link = f"https://t.me/{bot_username}?start=ref_{db_user.referral_code}"
    text = (
        "<b>🎁 Приглашай друзей</b>\n\n"
        f"За каждого друга вы <b>оба</b> получаете <b>+{bonus_days} дн.</b> подписки.\n\n"
        "Твоя ссылка — нажми, чтобы скопировать:\n"
        f"<code>{link}</code>\n\n"
        f"👥 Уже с тобой: <b>{invited}</b>"
    )
    share = f"https://t.me/share/url?url={link}"
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb_rows = [[InlineKeyboardButton(text="📤 Поделиться", url=share)]]
    if withdrawals_on:
        from src.bot.handlers.withdraw import available_minor

        avail = await available_minor(container, db_user.id)
        text += f"\n\n💸 Доступно к выводу: <b>{avail / 100:.2f} ₽</b>"
        kb_rows.append([InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw:start")])
    kb_rows.append([InlineKeyboardButton(text="‹ Кабинет", callback_data="act:cabinet:0")])
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await render_screen(cb, container, "referral", text, markup)
    await ack(cb)


@router.callback_query(F.data.startswith("act:trial"))
async def act_trial(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    if not await ensure_channel(cb, container, scope="trial"):  # channel-lock (#1)
        return
    async with container.uow() as uow:
        cfg = container.bot_config
        if not bool(await cfg.value(uow, "TRIAL_ENABLED")):
            await ack(cb, "Пробный период недоступен", alert=True)
            return
        # FOR UPDATE: double-tap on the trial button must not grant twice.
        user = await uow.users.lock_for_update(db_user.id)
        if user is None or not user.is_trial_available:
            await ack(cb, "Пробный период уже использован", alert=True)
            return
        # A trial grant() spins up a new panel user and overwrites current_subscription_id,
        # orphaning any live (usually paid) subscription. Refuse while one is usable — the
        # trial flag stays True for buy-first users, so it alone doesn't guard this.
        if user.current_subscription_id is not None:
            existing = await uow.subscriptions.get(user.current_subscription_id)
            if existing is not None and existing.status.is_usable:
                await ack(cb, "У тебя уже есть активная подписка", alert=True)
                return
        days = int(await cfg.value(uow, "TRIAL_DURATION_DAYS"))
        traffic_gb = int(await cfg.value(uow, "TRIAL_TRAFFIC_GB"))
        devices = int(await cfg.value(uow, "TRIAL_DEVICE_LIMIT"))
        trial_price = int(await cfg.value(uow, "TRIAL_PRICE"))

        # Paid trial: charge the wallet (guarded) before provisioning.
        if trial_price > 0 and not await uow.users.debit_balance_guarded(user, trial_price):
            await ack(
                cb,
                f"Пробный стоит {trial_price / 100:.0f} ₽ — пополни баланс и повтори",
                alert=True,
            )
            return

        plan = await uow.plans.find_one(is_trial=True) or await uow.plans.find_one(name="Trial")
        if plan is not None and not plan.is_trial:
            plan.is_trial = True
        if plan is None:
            plan = Plan(
                public_code="trial",
                name="Trial",
                is_trial=True,
                is_active=False,  # not for sale — granted, not bought
                traffic_limit_bytes=traffic_gb * GIB or None,
                device_limit=devices,
            )
            await uow.plans.add(plan)

        req = PurchaseRequest(
            user_id=user.id,
            plan_id=plan.id,
            duration_days=days,
            currency=Currency.RUB,
            purchase_type=PurchaseType.NEW,
        )
        try:
            sub = await container.subscriptions.grant(
                uow, user=user, plan=plan, req=req, is_trial=True
            )
        except RemnawaveError:
            await ack(cb, "Сервис временно недоступен, попробуй позже", alert=True)
            return
        await uow.commit()
        from src.application.events import TrialGranted

        await container.event_bus.publish(TrialGranted(user_id=user.id, subscription_id=sub.id))
        url = sub.subscription_url

    from src.web.routes.admin.notifications import notification_text

    async with container.uow() as uow:  # owner-editable «trial_started» template (NOTIF-1)
        base = await notification_text(
            uow, "trial_started", name=hesc(db_user.first_name or ""), days=days
        )
    text = base or (
        "<b>⭐ Пробный период активирован</b>\n\n"
        f"Доступ открыт на <b>{days} дн.</b> Подключайся и тестируй без ограничений."
    )
    if url:
        text += f"\n\n🔌 Ссылка подписки:\n<code>{url}</code>"
    await render_screen(
        cb,
        container,
        "trial",
        text,
        simple_keyboard([("👤 Моя подписка", "act:subscription:0"), ("‹ Меню", "nav:root")]),
    )
    await ack(cb, "Готово!")


@router.callback_query(F.data.startswith("act:support"))
async def act_support(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    async with container.uow() as uow:
        cfg = container.bot_config
        mode = str(await cfg.value(uow, "SUPPORT_MODE"))
        redirect = str(await cfg.value(uow, "SUPPORT_REDIRECT_USERNAME") or "")
        support_bot = str(await cfg.value(uow, "SUPPORT_BOT_USERNAME") or "")
        miniapp_url = str(await cfg.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
    back = InlineKeyboardButton(  # never a dead end (SUP-1); support lives under the cabinet
        text="‹ Кабинет", callback_data="act:cabinet:0"
    )
    if mode == "bot" and support_bot:
        await render_screen(
            cb,
            container,
            "support",
            "<b>🆘 Поддержка</b>\n\n"
            "Пиши в наш саппорт-бот — оператор на связи и ответит прямо там.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Открыть поддержку",
                            url=f"https://t.me/{support_bot.lstrip('@')}",
                        )
                    ],
                    [back],
                ]
            ),
        )
        await ack(cb)
        return
    if mode == "redirect" and redirect:
        await render_screen(
            cb,
            container,
            "support",
            "<b>🆘 Поддержка</b>\n\nНапиши нам напрямую — отвечаем быстро, без ботов и очередей.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💬 Написать", url=f"https://t.me/{redirect.lstrip('@')}"
                        )
                    ],
                    [back],
                ]
            ),
        )
        await ack(cb)
        return
    if mode == "miniapp" and miniapp_url.startswith("https://"):
        await render_screen(
            cb,
            container,
            "support",
            "<b>🆘 Поддержка</b>\n\n"
            "Чат поддержки живёт в приложении — открывай и пиши, мы на связи.",
            InlineKeyboardMarkup(
                inline_keyboard=[[webapp_button("💬 Открыть поддержку", miniapp_url)], [back]]
            ),
        )
        await ack(cb)
        return
    # tickets mode (default): hand off to the tickets FSM
    from src.bot.handlers.tickets import begin_ticket

    await begin_ticket(cb, container, db_user)


@router.message(Command("bug"))
async def cmd_bug(message: Message, container: AppContainer, db_user: User) -> None:
    """User bug report -> DM'd to admins with the reporter's handle."""
    text = (message.text or "").removeprefix("/bug").strip()
    if not text:
        await message.answer(
            "🐞 Опиши проблему одним сообщением: <code>/bug что не работает</code>"
        )
        return
    tg = message.from_user
    who = f"@{tg.username}" if tg and tg.username else f"id{db_user.telegram_id}"
    from src.infrastructure.services.reports import send_topic_report

    await send_topic_report(container, "bugs", f"🐞 Баг-репорт от {who}:\n{text}", force_dm=True)
    await message.answer("🐞 Спасибо! Отправил разработчикам — разберёмся.")


@router.callback_query(F.data.startswith("act:devices"))
async def act_devices(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    """HWID devices of the current subscription: list + one-tap unbind."""
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
    panel_ref = sub.panel_ref if sub is not None else None
    if sub is None or not sub.status.is_usable or panel_ref is None:
        await ack(cb, "Сначала оформи подписку", alert=True)
        return
    try:
        devices = await container.remnawave_client.get_devices(panel_ref)
    except Exception:
        await ack(cb, "Панель временно недоступна, попробуй позже", alert=True)
        return
    limit = f" (лимит {sub.device_limit})" if sub.device_limit else ""
    if not devices:
        text = f"<b>📱 Устройства{limit}</b>\n\nПока ни одно устройство не подключалось."
        kb = [[InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")]]
    else:
        lines = [f"<b>📱 Устройства{limit}</b>", "", "Нажми на устройство, чтобы отвязать:"]
        kb = []
        # Encode the INDEX, not the HWID: a full HWID (up to 64 chars) + "devdel:" overruns the
        # 64-byte callback_data cap; truncating it made the panel unbind silently miss (#8).
        for i, d in enumerate(devices[:10]):
            label = " · ".join(x for x in (d.platform, d.device_model) if x) or d.hwid[:12]
            kb.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"devdel:{i}")])
        text = "\n".join(lines)
        kb.append([InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")])
    await render_screen(cb, container, "devices", text, InlineKeyboardMarkup(inline_keyboard=kb))
    await safe_answer(cb)  # devdel chains here after answering


@router.callback_query(F.data.startswith("devdel:"))
async def devdel(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    idx_s = (cb.data or "")[len("devdel:") :]
    idx = int(idx_s) if idx_s.isdigit() else -1
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
    panel_ref = sub.panel_ref if sub is not None else None
    if sub is None or panel_ref is None or idx < 0:
        await ack(cb, "Нет активной подписки", alert=True)
        return
    try:
        devices = await container.remnawave_client.get_devices(panel_ref)
        if idx >= len(devices):  # list changed since render
            await ack(cb, "Список изменился — открой заново", alert=True)
        else:
            await container.remnawave_client.delete_device(panel_ref, devices[idx].hwid)
            await ack(cb, "Устройство отвязано ✅")
    except Exception:
        await ack(cb, "Не получилось — попробуй позже", alert=True)
        return
    await act_devices(cb, container, db_user)


@router.callback_query(F.data.startswith("act:nodes"))
async def act_nodes(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    """User-facing server status: 🟢/🟠/🔴 per node with online counts."""
    from src.core.enums import ServerNodeStatus

    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "NODE_STATUS_ENABLED")):
            await ack(cb, "Раздел недоступен", alert=True)
            return
        # Only nodes the owner put up for sale — the user shouldn't see internal/parked servers.
        # That's exactly what the «в продаже» toggle in the cabinet controls.
        nodes = sorted(
            (n for n in await uow.server_nodes.list() if n.is_for_sale), key=lambda n: n.name
        )
    if not nodes:
        text = "<b>🌍 Статус серверов</b>\n\nДанные ещё собираются — загляни чуть позже."
    else:
        glyph = {
            ServerNodeStatus.ONLINE: "🟢",
            ServerNodeStatus.OFFLINE: "🔴",
            ServerNodeStatus.MAINTENANCE: "🟠",
        }
        lines = ["<b>🌍 Статус серверов</b>", ""]
        for n in nodes[:30]:
            flag = f"{n.country_code} " if n.country_code else ""
            lines.append(
                f"{glyph.get(n.status, '⚪')} {flag}{hesc(n.name)} · онлайн {n.users_online}"
            )
        text = "\n".join(lines)
    await render_screen(cb, container, "nodes", text, simple_keyboard([("‹ Меню", "nav:root")]))
    await ack(cb)


@router.callback_query(F.data.startswith("act:proxy"))
async def act_proxy(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    """MTProto proxy button: one tap connects Telegram through the owner's proxy."""
    async with container.uow() as uow:
        cfg = container.bot_config
        enabled = bool(await cfg.value(uow, "MTPROTO_PROXY_ENABLED"))
        raw = str(await cfg.value(uow, "MTPROTO_PROXY_URL") or "").strip()
    if not enabled or not raw:
        await ack(cb, "Прокси не настроен", alert=True)
        return
    if raw.startswith("tg://proxy"):
        raw = "https://t.me/proxy" + raw.removeprefix("tg://proxy")
    elif raw.startswith("t.me/"):
        raw = "https://" + raw
    text = (
        "<b>🔌 MTProto-прокси</b>\n\n"
        "Один тап — и Telegram пойдёт через наш прокси. Выручает там, где мессенджер режут.\n\n"
        "Жми кнопку ниже 👇"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔌 Подключить прокси", url=raw)],
            [InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")],
        ]
    )
    await render_screen(cb, container, "proxy", text, markup)
    await ack(cb)


@router.callback_query(F.data.startswith("act:admin"))
async def act_admin(
    cb: CallbackQuery, container: AppContainer, db_user: User, is_admin: bool = False
) -> None:
    """Constructor «Администратор» action — open the in-bot admin panel (admins only).

    Registered before ``act_unknown`` so ``act:admin`` reaches here. The admin router's own
    IsAdmin gate never sees this callback (it lives on a different router), so re-check ``is_admin``
    (the auth middleware computed it: role OR owner_ids OR ADMIN_IDS)."""
    if not is_admin:
        await send_main_menu(cb, container, db_user)  # not an admin -> just reopen the menu
        return
    from src.bot.handlers.admin.home import admin_menu

    await admin_menu(cb, container)


@router.callback_query(F.data.startswith("act:"))
async def act_unknown(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """Unknown/custom action codes fall back to the buy flow entry or menu."""
    action = (cb.data or "").split(":")[1] if ":" in (cb.data or "") else ""
    if action in ("buy", "shop", "plans"):
        from src.bot.handlers.purchase import open_buy

        await open_buy(cb, container, db_user)
        return
    log.info("unknown action", action=action)
    await send_main_menu(cb, container, db_user)
