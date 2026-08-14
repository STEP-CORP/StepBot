"""Purchase flow: plan -> duration -> pay with balance or Telegram Stars.

Balance: start() -> deduct -> CAS to COMPLETED -> fulfill, all in one transaction
(panel-first inside fulfill; any failure rolls the whole purchase back).
Stars: start() -> XTR invoice with payload=payment_id -> successful_payment ->
PaymentService.process (the same idempotent path webhooks use).
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import replace
from html import escape as hesc
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from src.application.dto.pricing import PurchaseRequest
from src.bot.banners import render_screen
from src.bot.gate import ensure_channel
from src.bot.keyboards import simple_keyboard
from src.bot.screen import ack, show_screen
from src.core.constants import MAX_DEPOSIT_AMOUNT_MINOR
from src.core.enums import Currency, PurchaseType, TransactionStatus, TransactionType
from src.core.exceptions import (
    DomainError,
    InsufficientBalance,
    InvalidStateTransition,
    RemnawaveError,
)
from src.core.logging import get_logger
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer

if TYPE_CHECKING:
    from src.infrastructure.database.uow import UnitOfWork

log = get_logger(__name__)

router = Router(name="purchase")

GIB = 1024**3


def fmt_money(minor: int) -> str:
    v = minor / 100
    return f"{v:,.0f} ₽".replace(",", " ") if v == int(v) else f"{v:,.2f} ₽".replace(",", " ")


async def open_buy(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    """Buy-flow entry: SALES_MODE routes to the plan catalogue or the constructor."""
    async with container.uow() as uow:
        mode = str(await container.bot_config.value(uow, "SALES_MODE"))
    if mode == "constructor":
        await show_constructor(cb, container, db_user)
    else:
        await show_plans(cb, container, db_user)


async def show_plans(cb: CallbackQuery | Message, container: AppContainer, db_user: User) -> None:
    if not await ensure_channel(cb, container, scope="buy"):  # channel-lock (#1)
        return
    async with container.uow() as uow:
        plans = [p for p in await uow.plans.list_with_durations() if p.is_active and not p.is_trial]
    if not plans:
        await ack(cb, "Тарифы ещё не настроены", alert=True)
        return
    rows = []
    for p in sorted(plans, key=lambda p: p.order_index):
        cheapest = min(
            (pr.price_minor for d in p.durations for pr in d.prices),
            default=0,
        )
        traffic = f"{(p.traffic_limit_bytes or 0) / GIB:.0f} ГБ" if p.traffic_limit_bytes else "∞"
        rows.append((f"{p.name} · {traffic} · от {fmt_money(cheapest)}", f"plan:{p.id}"))
    rows.append(("‹ Меню", "nav:root"))
    caption = (
        "<b>🛒 Выбери тариф</b>\n\n"
        "Цена и трафик — прямо в кнопках.\n"
        "Жми подходящий, срок выберешь на следующем шаге."
    )
    await render_screen(cb, container, "buy", caption, simple_keyboard(rows))
    await ack(cb)


@router.callback_query(F.data.startswith("check:sub"))
async def check_sub(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """'Я подписался' — re-check membership, then resume the action the user was gated on."""
    parts = (cb.data or "check:sub").split(":")
    scope = parts[2] if len(parts) >= 3 else "buy"
    if not await ensure_channel(cb, container, scope=scope):
        return
    if scope == "trial":
        from src.bot.handlers.actions import act_trial

        await act_trial(cb, container, db_user)
    else:
        await open_buy(cb, container, db_user)


def _duration_label(days: int) -> str:
    """'7 дн.' / '1 мес' / '1 год' — never labels a sub-monthly period as '1 мес' (DUR-1)."""
    if days >= 365 and days % 365 == 0:
        years = days // 365
        return "1 год" if years == 1 else f"{years} г."
    if days >= 30 and days % 30 == 0:
        return f"{days // 30} мес"
    return f"{days} дн."


@router.callback_query(F.data.startswith("plan:"))
async def show_durations(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():  # crafted/stale payload — back to the menu
        await open_buy(cb, container, db_user)
        return
    plan_id = int(parts[1])
    async with container.uow() as uow:
        plan = await uow.plans.get_with_durations(plan_id)
    if plan is None or not plan.durations:
        # Also lands here from «Продлить» when the subscription is a constructor one
        # (the hidden plan has no durations) — open_buy routes back to the constructor.
        await open_buy(cb, container, db_user)
        return
    # Quote the discounted final per duration so the browse price equals the charge (#4) — the
    # same pricing.quote the payment screen uses; keeps bot list == bot pay == cabinet == charge.
    ptype, sub_id = await _resolve_purchase_type(container, db_user, plan.id)
    rows = []
    async with container.uow() as uow:
        for d in plan.durations:
            rub = next((p.price_minor for p in d.prices if p.currency is Currency.RUB), None)
            if rub is None:
                continue
            req = replace(
                _purchase_request(plan.id, d.days, db_user),
                purchase_type=ptype,
                subscription_id=sub_id,
            )
            price = rub
            with contextlib.suppress(Exception):
                price = (await container.pricing.quote(uow, req)).final.amount_minor
            label = f"{_duration_label(d.days)} · {fmt_money(price)}"
            rows.append((label, f"dur:{plan.id}:{d.days}"))
    rows.append(("‹ Назад", "act:buy:0"))
    await render_screen(
        cb,
        container,
        "durations",
        f"<b>🛒 {hesc(plan.name)}</b>\n{hesc(plan.description or '')}\n\n"
        "Выбери срок — чем длиннее, тем дешевле месяц.",
        simple_keyboard(rows),
    )
    await cb.answer()


async def _payment_methods(
    uow: UnitOfWork,
    container: AppContainer,
    db_user: User,
    price_minor: int,
    *,
    include_balance: bool = True,
) -> list[tuple[str, str]]:
    """(label, method_code) pairs for a payment screen, respecting the config toggles.

    Order + the Balance/Stars labels are operator-controlled (PAYMENT_METHOD_ORDER /
    PAYMENT_BALANCE_LABEL / PAYMENT_STARS_LABEL) and shared with the mini-app.

    ``include_balance=False`` drops the "pay from balance" entry even when BALANCE_ENABLED is
    on — used by the top-up screen itself, where paying from balance to fund the balance makes
    no sense.
    """
    from src.core.payment_order import order_rank

    cfg = container.bot_config
    stars_rate = int(await cfg.value(uow, "STARS_RATE_RUB"))
    balance_enabled = bool(await cfg.value(uow, "BALANCE_ENABLED"))
    bal_label = str(await cfg.value(uow, "PAYMENT_BALANCE_LABEL") or "").strip() or "С баланса"
    stars_label = str(await cfg.value(uow, "PAYMENT_STARS_LABEL") or "").strip() or "Telegram Stars"
    rank = order_rank(str(await cfg.value(uow, "PAYMENT_METHOD_ORDER") or ""))

    # (label, method_code, order_id) — order_id is the stable name used in PAYMENT_METHOD_ORDER.
    entries: list[tuple[str, str, str]] = []
    if balance_enabled and include_balance:
        ok = "✅" if db_user.balance_minor >= price_minor else "❌"
        entries.append((f"{ok} {bal_label} ({fmt_money(db_user.balance_minor)})", "bal", "balance"))
    stars = max(1, math.ceil(price_minor / max(1, stars_rate)))
    entries.append((f"⭐ {stars_label} · {stars} ★", "stars", "stars"))
    from src.application.services.pay_forms import gateway_form_options

    for g in await uow.payment_gateways.list():
        if (
            g.is_active
            and g.type in container.gateway_factory.supported()
            and g.type.value not in ("manual", "telegram_stars")
        ):
            name = g.display_name or g.type.value
            # A form-routable gateway with several enabled methods (e.g. Platega СБП + Карта)
            # becomes one button per method; everything else stays a single gateway button.
            options = gateway_form_options(g.type, (g.settings or {}).get("enabled_forms"))
            if options:
                for form, flabel in options:
                    code = f"{g.type.value}@{form}"
                    entries.append((f"💳 {name} · {flabel}", code, g.type.value))
            else:
                entries.append((f"💳 {name}", g.type.value, g.type.value))
    entries.sort(key=lambda e: rank(e[2]))  # stable — unlisted methods keep default order
    return [(label, code) for label, code, _order_id in entries]


@router.callback_query(F.data.startswith("dur:"))
async def choose_payment(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await open_buy(cb, container, db_user)
        return
    _, plan_id, days = parts
    ptype, sub_id = await _resolve_purchase_type(container, db_user, int(plan_id))
    async with container.uow() as uow:
        req = _purchase_request(int(plan_id), int(days), db_user)
        req = replace(req, purchase_type=ptype, subscription_id=sub_id)
        try:
            quote = await container.pricing.quote(uow, req)
        except DomainError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        methods = await _payment_methods(uow, container, db_user, quote.final.amount_minor)
    price = quote.final.amount_minor
    rows = [(label, f"pay:{plan_id}:{days}:{code}") for label, code in methods]
    if not quote.discount_pct:
        rows.append(("🎟 У меня промокод", "act:promocode"))
    rows.append(("‹ Назад", f"plan:{plan_id}"))
    discount = f" (−{quote.discount_pct}%)" if quote.discount_pct else ""
    bonus = quote.components.get("change_bonus_days", 0)
    if bonus > 0:
        discount += f"\n+{bonus} дн. от остатка текущего тарифа"
    await render_screen(
        cb,
        container,
        "payment",
        f"<b>💳 Способ оплаты</b>\n\nК оплате: <b>{fmt_money(price)}</b>{discount}\n\n"
        "Выбери, чем платишь.",
        simple_keyboard(rows),
    )
    await cb.answer()


def _purchase_request(plan_id: int, days: int, user: User) -> PurchaseRequest:
    renew_sub_id: int | None = None
    purchase_type = PurchaseType.NEW
    # RENEW when the user's current subscription is on this very plan and usable.
    return PurchaseRequest(
        user_id=user.id,
        plan_id=plan_id,
        duration_days=days,
        currency=Currency.RUB,
        purchase_type=purchase_type,
        subscription_id=renew_sub_id,
    )


async def _resolve_purchase_type(
    container: AppContainer, user: User, plan_id: int
) -> tuple[PurchaseType, int | None]:
    async with container.uow() as uow:
        return await container.purchase.resolve_purchase_type(uow, user.id, plan_id)


@router.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 4 or not (parts[1].isdigit() and parts[2].isdigit()):
        await open_buy(cb, container, db_user)
        return
    _, plan_id_s, days_s, method = parts
    plan_id, days = int(plan_id_s), int(days_s)
    ptype, sub_id = await _resolve_purchase_type(container, db_user, plan_id)
    req = PurchaseRequest(
        user_id=db_user.id,
        plan_id=plan_id,
        duration_days=days,
        currency=Currency.RUB,
        purchase_type=ptype,
        subscription_id=sub_id,
    )
    await _start_payment(cb, container, req, method)


async def _start_payment(
    cb: CallbackQuery, container: AppContainer, req: PurchaseRequest, method: str
) -> None:
    """Dispatch a built PurchaseRequest to the chosen payment method (plans + constructor).

    Guarded by a short per-user lock: aiogram runs updates concurrently, so a double-tap
    would double-debit the wallet or spawn duplicate invoices. Released on completion,
    expires by TTL if the handler dies mid-flight.
    """
    # TTL must exceed worst-case provisioning (panel retries: PANEL_RETRY_ATTEMPTS x timeout, up
    # to ~60s) - else a slow first tap releases the lock before it commits and a second tap
    # re-enters and double-debits the wallet. The `finally` releases it on the normal path.
    if not await container.redis.set(f"paylock:{req.user_id}", "1", nx=True, ex=90):
        await cb.answer("Платёж уже обрабатывается — секунду…", show_alert=True)
        return
    try:
        await _start_payment_locked(cb, container, req, method)
    finally:
        with contextlib.suppress(Exception):
            await container.redis.delete(f"paylock:{req.user_id}")


async def _start_payment_locked(
    cb: CallbackQuery, container: AppContainer, req: PurchaseRequest, method: str
) -> None:
    if method == "bal":
        await _pay_with_balance(cb, container, req)
        return

    if method != "stars":
        await _pay_with_gateway(cb, container, req, method)
        return

    # Stars: create the pending transaction, then send an XTR invoice.
    async with container.uow() as uow:
        try:
            txn, quote = await container.purchase.start(uow, req)
        except DomainError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))
        title = str((txn.plan_snapshot or {}).get("name") or "VPN")
        await uow.commit()
        payment_id = str(txn.payment_id)
        amount_minor = quote.final.amount_minor
        is_free = quote.is_free

    if is_free:
        # 100% discount: start() already fulfilled the purchase — no invoice to send.
        await _show_activated(cb, container, req.user_id)
        return

    stars = max(1, math.ceil(amount_minor / max(1, stars_rate)))
    if cb.message is not None:
        try:
            await cb.message.answer_invoice(  # type: ignore[union-attr,unused-ignore]
                title=f"{title} · {req.duration_days} дн.",
                description="Оплата VPN-подписки",
                payload=payment_id,
                currency="XTR",
                prices=[LabeledPrice(label="VPN", amount=stars)],
            )
        except TelegramBadRequest as exc:
            # Invoice refused — close the orphan PENDING txn instead of stranding it.
            from uuid import UUID

            log.warning("stars invoice failed", amount_minor=amount_minor, error=str(exc))
            async with container.uow() as uow:
                await uow.transactions.transition_status(
                    UUID(payment_id), TransactionStatus.CANCELED, (TransactionStatus.PENDING,)
                )
                await uow.commit()
            await cb.answer("Не удалось создать счёт — попробуй другой способ", show_alert=True)
            return
    await cb.answer()


# --- constructor mode (SALES_MODE=constructor) ---------------------------------


def _period_label(days: int) -> str:
    return f"{days} дн" if days < 30 else f"{round(days / 30)} мес"


def _pack_label(gb: int, price_minor: int) -> str:
    traffic = f"{gb} ГБ" if gb else "∞ трафик"
    return f"{traffic} · +{fmt_money(price_minor)}" if price_minor else traffic


async def _constructor_request(
    container: AppContainer, uow: UnitOfWork, user: User, period_id: int, pack_id: int
) -> PurchaseRequest:
    device_limit = int(await container.bot_config.value(uow, "DEFAULT_DEVICE_LIMIT"))
    return await container.purchase.build_constructor_request(
        uow, user_id=user.id, period_id=period_id, pack_id=pack_id, device_limit=device_limit
    )


async def show_constructor(
    cb: CallbackQuery | Message, container: AppContainer, db_user: User
) -> None:
    if not await ensure_channel(cb, container, scope="buy"):  # channel-lock (#1)
        return
    async with container.uow() as uow:
        periods = [p for p in await uow.constructor_periods.list() if p.is_active]
    if not periods:
        await ack(cb, "Конструктор ещё не настроен", alert=True)
        return
    rows = [
        (f"{_period_label(p.days)} · {fmt_money(p.price_minor)}", f"cper:{p.id}")
        for p in sorted(periods, key=lambda p: p.days)
    ]
    rows.append(("‹ Меню", "nav:root"))
    await render_screen(
        cb,
        container,
        "buy",
        "<b>🛒 Конструктор</b>\n\nСобери свою подписку под себя.\nШаг 1 — срок:",
        simple_keyboard(rows),
    )
    await ack(cb)


@router.callback_query(F.data.startswith("cper:"))
async def constructor_packs(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await show_constructor(cb, container, db_user)
        return
    period_id = int(parts[1])
    async with container.uow() as uow:
        period = await uow.constructor_periods.get(period_id)
        packs = [t for t in await uow.traffic_packs.list() if t.is_active]
    if period is None or not period.is_active or not packs:
        await show_constructor(cb, container, db_user)
        return
    rows = [
        (_pack_label(t.gb, t.price_minor), f"cpack:{period.id}:{t.id}")
        for t in sorted(packs, key=lambda t: (t.gb == 0, t.gb))
    ]
    rows.append(("‹ Назад", "act:buy:0"))
    await render_screen(
        cb,
        container,
        "durations",
        f"<b>🛒 Твоя подписка</b>\n\n"
        f"Срок: <b>{_period_label(period.days)} · {fmt_money(period.price_minor)}</b>\n"
        "Шаг 2 — трафик:",
        simple_keyboard(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cpack:"))
async def constructor_payment(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await show_constructor(cb, container, db_user)
        return
    _, period_id, pack_id = parts
    async with container.uow() as uow:
        try:
            req = await _constructor_request(container, uow, db_user, int(period_id), int(pack_id))
            quote = await container.pricing.quote(uow, req)
        except DomainError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        methods = await _payment_methods(uow, container, db_user, quote.final.amount_minor)
        traffic_gb = (req.traffic_limit_bytes or 0) // GIB
    rows = [(label, f"cpay:{period_id}:{pack_id}:{code}") for label, code in methods]
    rows.append(("‹ Назад", f"cper:{period_id}"))
    discount = f" (−{quote.discount_pct}%)" if quote.discount_pct else ""
    bonus = quote.components.get("change_bonus_days", 0)
    if bonus > 0:
        discount += f"\n+{bonus} дн. от остатка текущего тарифа"
    summary = f"{_period_label(req.duration_days)} · " + (f"{traffic_gb} ГБ" if traffic_gb else "∞")
    await render_screen(
        cb,
        container,
        "payment",
        f"<b>💳 Способ оплаты</b>\n\nТвоя подписка: <b>{summary}</b>\n"
        f"К оплате: <b>{fmt_money(quote.final.amount_minor)}</b>{discount}\n\n"
        "Выбери, чем платишь.",
        simple_keyboard(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cpay:"))
async def constructor_pay(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 4 or not (parts[1].isdigit() and parts[2].isdigit()):
        await show_constructor(cb, container, db_user)
        return
    _, period_id, pack_id, method = parts
    async with container.uow() as uow:
        try:
            req = await _constructor_request(container, uow, db_user, int(period_id), int(pack_id))
        except DomainError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        await uow.commit()  # ensure_constructor_plan may have created the hidden plan
    await _start_payment(cb, container, req, method)


async def _pay_with_gateway(
    cb: CallbackQuery, container: AppContainer, req: PurchaseRequest, method: str
) -> None:
    """Hosted payment: pending tx -> provider invoice -> «Оплатить» button.

    The provider webhook drives fulfilment through the standard pipeline.
    """
    from src.application.common.payments import PaymentContext, PaymentResultKind
    from src.application.services.pay_forms import split_method
    from src.core.enums import PaymentGatewayType
    from src.core.money import Money
    from src.infrastructure.payments.crypto import decrypt_gateway_settings

    gateway_value, form = split_method(method)  # "platega@sbp" -> ("platega", "sbp")
    try:
        gtype = PaymentGatewayType(gateway_value)
    except ValueError:
        await cb.answer("Неизвестный способ оплаты", show_alert=True)
        return
    async with container.uow() as uow:
        row = await uow.payment_gateways.get_active(gtype)
        if row is None or gtype not in container.gateway_factory.supported():
            await cb.answer("Способ оплаты выключен", show_alert=True)
            return
        settings = decrypt_gateway_settings(container.secret_box, dict(row.settings))
        try:
            txn, quote = await container.purchase.start(uow, req)
        except DomainError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        if quote.is_free:
            # start() fulfilled the free purchase (panel user already created) — commit
            # NOW; a zero-amount provider invoice would fail and roll the grant back.
            await uow.commit()
            await _show_activated(cb, container, req.user_id)
            return
        title = str((txn.plan_snapshot or {}).get("name") or "VPN")
        gateway = container.gateway_factory.create(gtype, settings)
        try:
            result = await gateway.create_payment(
                PaymentContext(
                    payment_id=txn.payment_id,
                    amount=Money(quote.final.amount_minor, txn.currency),
                    description=f"{title} · {req.duration_days} дн.",
                    user_id=req.user_id,
                    telegram_id=cb.from_user.id if cb.from_user else None,
                    metadata={"form": form} if form else {},
                )
            )
        except Exception as exc:
            log.error("gateway create failed", gateway=method, error=str(exc))
            await cb.answer("Платёжка временно недоступна, попробуй другой способ", show_alert=True)
            return
        if result.kind is not PaymentResultKind.REDIRECT or not result.redirect_url:
            await cb.answer("Платёжка не вернула ссылку на оплату", show_alert=True)
            return
        txn.gateway_type = gtype
        txn.external_id = result.external_id
        txn.gateway_display_name = row.display_name or gtype.value
        await uow.commit()
        pay_url = result.redirect_url
        label = row.display_name or gtype.value

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить · {label}", url=pay_url)],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату", callback_data=f"paycheck:{txn.payment_id}"
                )
            ],
            [InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")],
        ]
    )
    await render_screen(
        cb,
        container,
        "payment",
        "<b>💳 Счёт создан</b>\n\n"
        "Оплати по кнопке ниже — подписка активируется автоматически сразу после оплаты ⚡\n"
        "Если оплатил, а подписки нет — жми «Проверить оплату».",
        markup,
    )
    await cb.answer()


async def _show_activated(cb: CallbackQuery, container: AppContainer, user_id: int) -> None:
    """Success screen after an already-committed fulfilment (free path / balance)."""
    async with container.uow() as uow:
        user = await uow.users.get(user_id)
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user and user.current_subscription_id
            else None
        )
        url = sub.subscription_url if sub else None
    text = "<b>✅ Подписка активирована!</b>"
    if url:
        text += f"\n\n🔌 Ссылка подписки:\n<code>{url}</code>"
    await render_screen(
        cb,
        container,
        "subscription",
        text,
        simple_keyboard([("👤 Моя подписка", "act:subscription:0"), ("‹ Меню", "nav:root")]),
    )
    await cb.answer("Готово!")


async def _pay_with_balance(
    cb: CallbackQuery, container: AppContainer, req: PurchaseRequest
) -> None:
    insufficient = False
    async with container.uow() as uow:
        try:
            await container.purchase.checkout_from_balance(uow, req)  # shared with the mini-app
        except RemnawaveError as exc:
            log.error("provision failed", error=str(exc))
            await cb.answer("Оплата не списана: сервис выдачи временно недоступен", show_alert=True)
            return  # no commit -> full rollback
        except InsufficientBalance:
            insufficient = True
            auto = bool(await container.bot_config.value(uow, "AUTO_PURCHASE_AFTER_TOPUP"))
            ttl = int(await container.bot_config.value(uow, "CART_TTL_SECONDS"))
        except InvalidStateTransition:
            await cb.answer("Платёж уже обработан", show_alert=True)
            return
        except DomainError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        else:
            await uow.commit()

    if insufficient:
        # Show the actual numbers — «не хватает средств» without the shortfall forces the
        # user to guess how much to top up (and guess wrong, stranding the auto-purchase).
        async with container.uow() as uow:
            user = await uow.users.get(req.user_id)
            balance = user.balance_minor if user else 0
            price: int | None = None
            with contextlib.suppress(Exception):
                price = (await container.pricing.quote(uow, req)).final.amount_minor
        lines = ["<b>💳 Не хватает средств</b>", ""]
        if price is not None:
            lines.append(f"Цена: <b>{fmt_money(price)}</b> · На балансе: {fmt_money(balance)}")
            if price > balance:
                lines.append(f"Не хватает: <b>{fmt_money(price - balance)}</b>")
        if auto:
            # Stash the intent — the deposit path auto-completes it after a top-up.
            from src.infrastructure.services.cart import save_cart

            await save_cart(container.redis, req, ttl)
            hours = max(1, ttl // 3600)
            lines.append("")
            lines.append(
                "Пополни баланс — и подписка оформится сама сразу после зачисления ⚡\n"
                f"Резерв покупки действует ещё ~{hours} ч."
            )
        else:
            lines.append("")
            lines.append("Пополни баланс и вернись к оплате.")
        await render_screen(
            cb,
            container,
            "balance",
            "\n".join(lines),
            simple_keyboard([("💳 Пополнить", "topup:menu"), ("‹ Меню", "nav:root")]),
        )
        await cb.answer()
        return

    await _show_activated(cb, container, req.user_id)


@router.callback_query(F.data == "traffic:menu")
async def traffic_menu(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """Buy extra gigabytes for the current (limited) subscription."""
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        packs = [p for p in await uow.traffic_packs.list() if p.is_active and p.gb > 0]
    if sub is None or not sub.status.is_usable:
        await cb.answer("Сначала оформи подписку", show_alert=True)
        return
    if sub.traffic_limit_bytes <= 0:
        await cb.answer("У тебя безлимитный трафик 🎉", show_alert=True)
        return
    if not packs:
        await cb.answer("Пакеты трафика не настроены", show_alert=True)
        return
    rows = [
        (f"+{p.gb} ГБ · {fmt_money(p.price_minor)}", f"tpack:{p.id}")
        for p in sorted(packs, key=lambda p: p.order_index)
    ]
    rows.append(("‹ Назад", "act:subscription:0"))
    await render_screen(
        cb,
        container,
        "traffic",
        "<b>📈 Докупить трафик</b>\n\n"
        "Гигабайты добавятся к лимиту текущей подписки сразу после оплаты — срок не меняется.",
        simple_keyboard(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("tpack:"))
async def traffic_pack_pay(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await traffic_menu(cb, container, db_user)
        return
    pack_id = int(parts[1])
    async with container.uow() as uow:
        pack = await uow.traffic_packs.get(pack_id)
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
        balance_enabled = bool(await container.bot_config.value(uow, "BALANCE_ENABLED"))
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))
        online_gateways = [
            (g.type.value, g.display_name or g.type.value)
            for g in await uow.payment_gateways.list()
            if g.is_active
            and g.type in container.gateway_factory.supported()
            and g.type.value not in ("manual", "telegram_stars")
        ]
    if pack is None or sub is None or sub.plan_id is None:
        await cb.answer("Пакет недоступен", show_alert=True)
        return
    # Pricing applies the user's personal/purchase discount to traffic packs too, so quote the
    # real price — otherwise the shown total is full price but a smaller amount is charged (TRAF-1).
    async with container.uow() as uow:
        quote = await container.pricing.quote(
            uow,
            PurchaseRequest(
                user_id=db_user.id,
                plan_id=sub.plan_id,
                duration_days=0,
                currency=Currency.RUB,
                purchase_type=PurchaseType.TRAFFIC_TOPUP,
                subscription_id=sub.id,
                traffic_pack_id=pack_id,
            ),
        )
    price = quote.final.amount_minor
    stars = max(1, math.ceil(price / max(1, stars_rate)))
    rows = []
    if balance_enabled:
        ok = "✅" if db_user.balance_minor >= price else "❌"
        rows.append((f"{ok} С баланса ({fmt_money(db_user.balance_minor)})", f"tpay:{pack_id}:bal"))
    rows.append((f"⭐ Telegram Stars · {stars} ★", f"tpay:{pack_id}:stars"))
    for gtype, label in online_gateways:
        rows.append((f"💳 {label}", f"tpay:{pack_id}:{gtype}"))
    rows.append(("‹ Назад", "traffic:menu"))
    await render_screen(
        cb,
        container,
        "traffic",
        f"<b>📈 +{pack.gb} ГБ</b>\n\nК оплате: <b>{fmt_money(price)}</b>\nВыбери способ оплаты:",
        simple_keyboard(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("tpay:"))
async def traffic_pay(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        await traffic_menu(cb, container, db_user)
        return
    _, pack_id_s, method = parts
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(db_user.current_subscription_id)
            if db_user.current_subscription_id
            else None
        )
    if sub is None or sub.plan_id is None:
        await cb.answer("Нет активной подписки", show_alert=True)
        return
    req = PurchaseRequest(
        user_id=db_user.id,
        plan_id=sub.plan_id,
        duration_days=0,
        currency=Currency.RUB,
        purchase_type=PurchaseType.TRAFFIC_TOPUP,
        subscription_id=sub.id,
        traffic_pack_id=int(pack_id_s),
    )
    await _start_payment(cb, container, req, method)


# --- balance top-up (Stars or any connected gateway) ---------------------------

_TOPUP_PRESETS_RUB = (100, 250, 500, 1000)


class TopupForm(StatesGroup):
    waiting_amount = State()


@router.callback_query(F.data == "topup:menu")
async def topup_menu(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            # Old messages / custom menus keep the button alive after the owner turns
            # the wallet off — refuse here, or money lands in a wallet nothing accepts.
            await cb.answer("Пополнение баланса отключено", show_alert=True)
            return
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
    amounts_minor = [r * 100 for r in _TOPUP_PRESETS_RUB if r * 100 >= min_dep] or [min_dep]
    rows = [(fmt_money(minor), f"topup:{minor}") for minor in amounts_minor]
    rows.append(("✏️ Своя сумма", "topup:custom"))
    rows.append(("‹ Назад", "act:balance:0"))
    await render_screen(
        cb,
        container,
        "topup",
        "<b>💳 Пополнение баланса</b>\n\n"
        "Выбери сумму — способ оплаты (Stars, карта, СБП…) выберешь на следующем шаге.",
        simple_keyboard(rows),
    )
    await cb.answer()


@router.callback_query(F.data == "topup:custom")
async def topup_custom_prompt(
    cb: CallbackQuery, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    """ "Своя сумма" -> arm the FSM and ask for a plain-text amount in rubles.

    ``purchase.router`` runs ``ClearStaleForm`` on every callback, so any earlier stray form is
    already gone by the time this handler sets its own state — and tapping any other button on
    this router (including the Cancel button below) clears this one the same way.
    """
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            await cb.answer("Пополнение баланса отключено", show_alert=True)
            return
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
    await state.set_state(TopupForm.waiting_amount)
    await show_screen(
        cb,
        "<b>✏️ Своя сумма</b>\n\n"
        f"Пришли сумму пополнения в рублях, целым числом — от {fmt_money(max(1, min_dep))} "
        f"до {fmt_money(MAX_DEPOSIT_AMOUNT_MINOR)}.",
        simple_keyboard([("‹ Отмена", "topup:menu")]),
    )
    await cb.answer()


@router.message(TopupForm.waiting_amount, F.text & ~F.text.startswith("/"))
async def topup_custom_amount(
    message: Message, container: AppContainer, db_user: User, state: FSMContext
) -> None:
    """Free-text amount entry. Commands are excluded from the filter above (a stray ``/support``
    while this form is armed must reach the ticket router, not get read as an amount) and a
    self-clean guard below caps how long a truly non-numeric reply keeps the form armed — without
    either, the form (in Redis, surviving restarts) swallows every later message the user sends,
    forever, until they tap an inline button (see ``purchase.router`` vs ``tickets.py``'s
    catch-all, which refuses to fire while any state is active).
    """
    from src.bot.handlers.reply_menu import maybe_dispatch_menu_button

    # A bottom-bar tap (reply mode) reaches here before reply_menu — don't take it as an amount.
    if await maybe_dispatch_menu_button(message, container, db_user, state):
        return
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            await state.clear()
            await message.answer("Пополнение баланса отключено")
            return
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
    raw = (message.text or "").strip().replace(" ", "")
    # isdecimal(), not isdigit(): isdigit() is also true for digit-*like* characters int() can't
    # parse (e.g. "²", a superscript) — that used to raise ValueError below and crash the handler
    # into the global error reporter on every retry. isdecimal() matches exactly what int() accepts.
    if not raw.isdecimal():
        data = await state.get_data()
        misses = int(data.get("misses", 0)) + 1
        if misses >= 2:
            # Two non-amount replies in a row — this is very likely a stray message (a question,
            # small talk) rather than a typo retry, so drop the form instead of waiting forever:
            # same self-cleaning promo.py/withdraw.py already do on any input to their own forms.
            # One retry is still allowed (unlike promo/withdraw) so a plain typo doesn't force the
            # user back through "✏️ Своя сумма" to try again.
            await state.clear()
            await message.answer(
                "Нужно целое число рублей, например <code>3000</code>. "
                "Нажми «✏️ Своя сумма» ещё раз, если хочешь пополнить баланс.",
                parse_mode="HTML",
            )
            return
        await state.update_data(misses=misses)
        await message.answer(
            "Нужно целое число рублей, например <code>3000</code>. Пришли ещё раз:",
            parse_mode="HTML",
        )
        return
    amount_minor = int(raw) * 100
    if amount_minor < max(1, min_dep):
        await message.answer(f"Минимальное пополнение — {fmt_money(min_dep)}. Пришли другую сумму:")
        return
    if amount_minor > MAX_DEPOSIT_AMOUNT_MINOR:
        await message.answer(
            f"Максимальное пополнение — {fmt_money(MAX_DEPOSIT_AMOUNT_MINOR)}. Пришли другую сумму:"
        )
        return
    await state.clear()
    await _show_topup_methods(message, container, db_user, amount_minor)


async def _show_topup_methods(
    target: CallbackQuery | Message, container: AppContainer, db_user: User, amount_minor: int
) -> None:
    """The payment-method screen for a top-up amount — shared by a preset-button tap and the
    custom-amount form, so both land on the exact same screen/validation.

    Never offers "pay from balance" — that would fund the wallet from itself.
    """
    async with container.uow() as uow:
        methods = await _payment_methods(
            uow, container, db_user, amount_minor, include_balance=False
        )
    rows = [(label, f"topupm:{amount_minor}:{code}") for label, code in methods]
    # The amount also lives in a button, not just the caption above: an operator's SCREEN_TEXTS
    # override for "topup_method" (banners._apply_overrides) replaces the WHOLE caption, and could
    # easily drop the "amount due" line. This row is a dynamic button — screen_buttons.SAFE_SCREENS
    # only registers "topup:menu" (the back button) as an editable static button for this screen —
    # so apply_screen_buttons always carries it through byte-for-byte; the amount can't disappear
    # even from a screen whose text an operator fully rewrote. Tapping it is a harmless no-op.
    rows.insert(0, (f"💰 К зачислению: {fmt_money(amount_minor)}", "topup:amt"))
    rows.append(("‹ Назад", "topup:menu"))
    await render_screen(
        target,
        container,
        "topup_method",
        f"<b>💳 Пополнение баланса</b>\n\nК зачислению: <b>{fmt_money(amount_minor)}</b>\n\n"
        "Выбери способ оплаты.",
        simple_keyboard(rows),
    )


@router.callback_query(F.data == "topup:amt")
async def topup_amount_pill(cb: CallbackQuery) -> None:
    """The amount readout button above — purely informational, just swallow the tap."""
    await cb.answer()


@router.callback_query(F.data.startswith("topup:"))
async def topup_amount(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """Amount picked -> show the payment-method screen (mirrors ``choose_payment``)."""
    parts = (cb.data or "").split(":")
    if len(parts) < 2 or not parts[1].isdecimal():
        await cb.answer("Некорректная сумма", show_alert=True)
        return
    amount_minor = int(parts[1])
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            await cb.answer("Пополнение баланса отключено", show_alert=True)
            return
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
    # Callback data is forgeable — re-validate against the config/ceiling, not the button. No
    # upper bound here used to mean a crafted amount could reach a real provider invoice for
    # millions, or overflow the transaction's bigint column further down the pipeline.
    if amount_minor < max(1, min_dep):
        await cb.answer(f"Минимальное пополнение — {fmt_money(min_dep)}", show_alert=True)
        return
    if amount_minor > MAX_DEPOSIT_AMOUNT_MINOR:
        await cb.answer(
            f"Максимальное пополнение — {fmt_money(MAX_DEPOSIT_AMOUNT_MINOR)}", show_alert=True
        )
        return
    await _show_topup_methods(cb, container, db_user, amount_minor)
    await cb.answer()


@router.callback_query(F.data.startswith("topupm:"))
async def topup_pay(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """Method picked -> Stars invoice or a hosted gateway link, per ``pay``/``_start_payment``."""
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdecimal():
        await topup_menu(cb, container, db_user)
        return
    amount_minor, method = int(parts[1]), parts[2]
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            await cb.answer("Пополнение баланса отключено", show_alert=True)
            return
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
        # Re-validate again — this callback data carries the same forgeable amount as
        # topup_amount's (and the same overflow risk on the uncapped side).
        if amount_minor < max(1, min_dep):
            await cb.answer(f"Минимальное пополнение — {fmt_money(min_dep)}", show_alert=True)
            return
        if amount_minor > MAX_DEPOSIT_AMOUNT_MINOR:
            await cb.answer(
                f"Максимальное пополнение — {fmt_money(MAX_DEPOSIT_AMOUNT_MINOR)}", show_alert=True
            )
            return
        # The method must be one _payment_methods actually offers for this amount/user — a
        # crafted "manual"/"telegram_stars"/anything-not-listed code must not reach the gateway
        # lookup in _topup_with_gateway just because it happens not to return REDIRECT.
        offered = {
            code
            for _label, code in await _payment_methods(
                uow, container, db_user, amount_minor, include_balance=False
            )
        }
    if method not in offered:
        await cb.answer("Этот способ оплаты сейчас недоступен", show_alert=True)
        return
    await _start_topup(cb, container, db_user, amount_minor, method)


async def _start_topup(
    cb: CallbackQuery, container: AppContainer, db_user: User, amount_minor: int, method: str
) -> None:
    """Same double-tap guard as ``_start_payment`` — one lock per user covers both a purchase
    and a top-up, since either one can be mid-flight when the other tap lands."""
    if not await container.redis.set(f"paylock:{db_user.id}", "1", nx=True, ex=90):
        await cb.answer("Платёж уже обрабатывается — секунду…", show_alert=True)
        return
    try:
        if method == "stars":
            await _topup_with_stars(cb, container, db_user, amount_minor)
        else:
            await _topup_with_gateway(cb, container, db_user, amount_minor, method)
    finally:
        with contextlib.suppress(Exception):
            await container.redis.delete(f"paylock:{db_user.id}")


async def _topup_with_stars(
    cb: CallbackQuery, container: AppContainer, db_user: User, amount_minor: int
) -> None:
    async with container.uow() as uow:
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))
        txn = Transaction(
            user_id=db_user.id,
            type=TransactionType.DEPOSIT,
            status=TransactionStatus.PENDING,
            amount_minor=amount_minor,
            currency=Currency.RUB,
        )
        await uow.transactions.add(txn)
        await uow.commit()
        payment_id = str(txn.payment_id)
    stars = max(1, math.ceil(amount_minor / max(1, stars_rate)))
    if cb.message is not None:
        try:
            await cb.message.answer_invoice(  # type: ignore[union-attr,unused-ignore]
                title="Пополнение баланса",
                description=f"Пополнение на {fmt_money(amount_minor)}",
                payload=payment_id,
                currency="XTR",
                prices=[LabeledPrice(label="Баланс", amount=stars)],
            )
        except TelegramBadRequest as exc:
            # Invoice refused (amount out of Telegram's limits etc.) — close the orphan
            # PENDING txn so it doesn't haunt the history as an eternal «⏳ Пополнение».
            log.warning("topup invoice failed", amount_minor=amount_minor, error=str(exc))
            async with container.uow() as uow:
                await uow.transactions.transition_status(
                    txn.payment_id, TransactionStatus.CANCELED, (TransactionStatus.PENDING,)
                )
                await uow.commit()
            await cb.answer("Не удалось создать счёт — попробуй другую сумму", show_alert=True)
            return
    await cb.answer()


async def _topup_with_gateway(
    cb: CallbackQuery, container: AppContainer, db_user: User, amount_minor: int, method: str
) -> None:
    """Hosted top-up: pending DEPOSIT tx -> provider invoice -> «Оплатить» button.

    Mirrors ``_pay_with_gateway`` but skips ``purchase.start()`` — that always books a
    SUBSCRIPTION_PAYMENT transaction. A top-up is a plain DEPOSIT; PaymentService._fulfill
    already credits any DEPOSIT regardless of which gateway settled it, so nothing else changes.
    """
    from src.application.common.payments import PaymentContext, PaymentResultKind
    from src.application.services.pay_forms import split_method
    from src.core.enums import PaymentGatewayType
    from src.core.money import Money
    from src.infrastructure.payments.crypto import decrypt_gateway_settings

    gateway_value, form = split_method(method)  # "platega@sbp" -> ("platega", "sbp")
    try:
        gtype = PaymentGatewayType(gateway_value)
    except ValueError:
        await cb.answer("Неизвестный способ оплаты", show_alert=True)
        return
    async with container.uow() as uow:
        row = await uow.payment_gateways.get_active(gtype)
        if row is None or gtype not in container.gateway_factory.supported():
            await cb.answer("Способ оплаты выключен", show_alert=True)
            return
        settings = decrypt_gateway_settings(container.secret_box, dict(row.settings))
        gateway = container.gateway_factory.create(gtype, settings)
        txn = Transaction(
            user_id=db_user.id,
            type=TransactionType.DEPOSIT,
            status=TransactionStatus.PENDING,
            amount_minor=amount_minor,
            currency=Currency.RUB,
        )
        await uow.transactions.add(txn)  # flushes -> txn.payment_id assigned below
        try:
            result = await gateway.create_payment(
                PaymentContext(
                    payment_id=txn.payment_id,
                    amount=Money(amount_minor, txn.currency),
                    description="Пополнение баланса",
                    user_id=db_user.id,
                    telegram_id=cb.from_user.id if cb.from_user else None,
                    metadata={"form": form} if form else {},
                )
            )
        except Exception as exc:
            log.error("gateway create failed", gateway=method, error=str(exc))
            await cb.answer("Платёжка временно недоступна, попробуй другой способ", show_alert=True)
            return  # no commit -> the flushed PENDING txn rolls back with the session
        if result.kind is not PaymentResultKind.REDIRECT or not result.redirect_url:
            await cb.answer("Платёжка не вернула ссылку на оплату", show_alert=True)
            return  # no commit -> rollback, same as above
        txn.gateway_type = gtype
        txn.external_id = result.external_id
        txn.gateway_display_name = row.display_name or gtype.value
        await uow.commit()
        pay_url = result.redirect_url
        label = row.display_name or gtype.value

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить · {label}", url=pay_url)],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату", callback_data=f"paycheck:{txn.payment_id}"
                )
            ],
            [InlineKeyboardButton(text="‹ Меню", callback_data="nav:root")],
        ]
    )
    await render_screen(
        cb,
        container,
        "topup_invoice",
        "<b>💳 Счёт создан</b>\n\n"
        "Оплати по кнопке ниже — баланс пополнится автоматически сразу после оплаты ⚡\n"
        "Если оплатил, а баланс не изменился — жми «Проверить оплату».",
        markup,
    )
    await cb.answer()


# --- on-demand payment check ----------------------------------------------------


@router.callback_query(F.data.startswith("paycheck:"))
async def check_payment(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    """«Проверить оплату»: on-demand provider poll through the same idempotent pipeline
    the reconciler uses — a lost webhook becomes one tap instead of a minutes-long wait."""
    from uuid import UUID

    from src.infrastructure.payments.crypto import decrypt_gateway_settings

    parts = (cb.data or "").split(":")
    try:
        payment_id = UUID(parts[1])
    except (IndexError, ValueError):
        await cb.answer("Счёт не найден", show_alert=True)
        return
    async with container.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
    if txn is None or txn.user_id != db_user.id:
        await cb.answer("Счёт не найден", show_alert=True)
        return
    if txn.status is TransactionStatus.COMPLETED:
        await cb.answer("Оплата уже зачислена ✅", show_alert=True)
        return
    if txn.status is not TransactionStatus.PENDING:
        await cb.answer("Счёт закрыт (отменён или не прошёл) — создай новый.", show_alert=True)
        return
    # Gentle with the provider API: one live poll per invoice per 10 seconds.
    if not await container.redis.set(f"paycheck:{payment_id}", "1", nx=True, ex=10):
        await cb.answer("Уже проверяю — попробуй через пару секунд", show_alert=True)
        return
    gtype, external_id = txn.gateway_type, txn.external_id
    if gtype is None or external_id is None:
        await cb.answer(
            "Оплата ещё не поступила. Если оплатил — подожди минуту и проверь снова.",
            show_alert=True,
        )
        return
    async with container.uow() as uow:
        row = await uow.payment_gateways.get_active(gtype)
    if row is None or gtype not in container.gateway_factory.supported():
        await cb.answer("Способ оплаты сейчас недоступен — напиши в поддержку", show_alert=True)
        return
    gateway = container.gateway_factory.create(
        gtype, decrypt_gateway_settings(container.secret_box, dict(row.settings))
    )
    if not gateway.can_poll_status():
        await cb.answer(
            "Этот способ подтверждается провайдером — зачислится автоматически.",
            show_alert=True,
        )
        return
    try:
        result = await gateway.fetch_status(str(external_id))
    except Exception as exc:
        log.warning("paycheck poll failed", payment_id=str(payment_id), error=str(exc))
        await cb.answer("Не удалось проверить — попробуй через минуту", show_alert=True)
        return
    if result is None or result.status is TransactionStatus.PENDING:
        await cb.answer(
            "Провайдер пока не видит оплату. Если оплатил — подожди минуту и проверь снова.",
            show_alert=True,
        )
        return
    amount_minor = (
        result.amount.amount_minor
        if result.amount is not None and result.amount.currency == txn.currency
        else None
    )
    try:
        async with container.uow() as uow:
            await container.payments.process(
                uow, payment_id=payment_id, status=result.status, amount_minor=amount_minor
            )
            await uow.commit()
    except (DomainError, RemnawaveError) as exc:
        log.error("paycheck fulfilment failed", payment_id=str(payment_id), error=str(exc))
        await cb.answer(
            "Оплата найдена! Зачисление завершится автоматически в течение пары минут.",
            show_alert=True,
        )
        return
    async with container.uow() as uow:
        settled = await uow.transactions.get_by_payment_id(payment_id)
    if settled is not None and settled.status is TransactionStatus.COMPLETED:
        if settled.type is TransactionType.DEPOSIT:
            from src.infrastructure.taskiq.tasks import _try_auto_purchase

            await cb.answer("✅ Оплата зачислена на баланс!", show_alert=True)
            await _try_auto_purchase(container, payment_id)
        else:
            await _show_activated(cb, container, txn.user_id)
        return
    await cb.answer("Платёж не прошёл (отменён или отклонён провайдером).", show_alert=True)


# --- Telegram Stars settlement -------------------------------------------------


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, container: AppContainer) -> None:
    """Last gate before Telegram charges the stars: refuse invoices whose transaction is
    missing or already terminal — otherwise an old invoice message stays payable forever
    and a second payment burns real stars against a completed txn."""
    from uuid import UUID

    try:
        payment_id = UUID(query.invoice_payload)
    except ValueError:
        await query.answer(ok=False, error_message="Счёт устарел — открой оплату заново.")
        return
    async with container.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
    if txn is None:
        await query.answer(ok=False, error_message="Счёт не найден — открой оплату заново.")
        return
    if txn.status is not TransactionStatus.PENDING:
        await query.answer(
            ok=False,
            error_message="Этот счёт уже обработан. Если оплата не зачислена — напиши в поддержку.",
        )
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, container: AppContainer, db_user: User) -> None:
    import contextlib
    from uuid import UUID

    sp = message.successful_payment
    assert sp is not None
    try:
        payment_id = UUID(sp.invoice_payload)
    except ValueError:
        # Stars were charged against a payload we can't map — a human must sort it out.
        log.error("bad invoice payload", payload=sp.invoice_payload)
        await message.answer(
            "Оплата получена, но счёт не распознан — напиши в поддержку, мы разберёмся."
        )
        with contextlib.suppress(Exception):
            await container.notifier.notify_admins(
                f"⚠️ Stars-платёж с нечитаемым payload {sp.invoice_payload!r} "
                f"(charge_id={sp.telegram_payment_charge_id}, tg_id={db_user.telegram_id})",
                topic="payments",
            )
        return

    # Keep the provider charge reference BEFORE fulfilment: if everything below dies, the
    # txn still carries the id support needs for a stars refund (refundStarPayment).
    with contextlib.suppress(Exception):
        async with container.uow() as uow:
            ref = await uow.transactions.get_by_payment_id(payment_id)
            if ref is not None and not ref.external_id and sp.telegram_payment_charge_id:
                ref.external_id = sp.telegram_payment_charge_id
                await uow.commit()

    moved = False
    try:
        async with container.uow() as uow:
            moved = await container.payments.process(
                uow, payment_id=payment_id, status=TransactionStatus.COMPLETED
            )
            await uow.commit()
    except (DomainError, RemnawaveError) as exc:
        log.error("stars fulfilment failed", error=str(exc))
        # Durable fallback: the worker task retries with in-task backoff and is CAS-idempotent,
        # so «разбираемся» is true — the payment settles without a human as soon as the
        # panel/DB blip passes.
        deferred = False
        with contextlib.suppress(Exception):
            from src.infrastructure.taskiq.tasks import process_payment

            await process_payment.kiq(str(payment_id), TransactionStatus.COMPLETED.value)
            deferred = True
        with contextlib.suppress(Exception):
            await container.notifier.notify_admins(
                f"⚠️ Stars-платёж {payment_id}: выдача упала ({exc}), "
                + ("повтор поставлен в очередь." if deferred else "поставить повтор НЕ удалось!"),
                topic="payments",
            )
        await message.answer(
            "Оплата получена, зачисление завершится автоматически в течение пары минут. "
            "Если ничего не изменится — напиши в поддержку."
            if deferred
            else "Оплата получена, но выдача задерживается — мы уже разбираемся."
        )
        return

    async with container.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
        # The txn owner, not the payer: a forwarded invoice can be paid by another chat,
        # and the credit lands on the owner's wallet — show the owner's numbers.
        user = await uow.users.get(txn.user_id if txn is not None else db_user.id)
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user and user.current_subscription_id
            else None
        )

    if not moved:
        # pre_checkout guards this, but two near-simultaneous checkouts can both pass it:
        # the stars WERE charged while the txn was already terminal — never claim success.
        log.warning("stars payment against terminal txn", payment_id=str(payment_id))
        await message.answer(
            "Этот счёт уже был обработан ранее. Если звёзды списались повторно — "
            "напиши в поддержку, мы вернём платёж."
        )
        with contextlib.suppress(Exception):
            await container.notifier.notify_admins(
                f"⚠️ Повторная Stars-оплата счёта {payment_id} "
                f"(charge_id={sp.telegram_payment_charge_id}, tg_id={db_user.telegram_id}) — "
                "нужен возврат звёзд.",
                topic="payments",
            )
        return

    if txn is not None and txn.type is TransactionType.DEPOSIT:
        # Owner-editable template (its toggle only swaps the text — an in-chat payment
        # always gets SOME acknowledgement, silence here looks like lost money).
        from src.web.routes.admin.notifications import notification_text

        balance = fmt_money(user.balance_minor) if user else "—"
        async with container.uow() as uow:
            text = await notification_text(
                uow,
                "balance_topup",
                # Escaped: a Telegram first_name with <, > or & would make the HTML template
                # invalid, Telegram would 400 the message, and the buyer would see a generic
                # error instead of the confirmation — after the money already moved.
                name=hesc((user.first_name if user else "") or ""),
                amount=fmt_money(txn.amount_minor),
                balance=balance,
            )
        await message.answer(
            text or f"✅ <b>Баланс пополнен.</b>\nТекущий баланс: {balance}", parse_mode="HTML"
        )
        # Stars is the in-bot top-up path too, so it must complete a stashed «smart cart»
        # purchase just like the out-of-band webhook path does (PAY-1). No-ops without a cart.
        from src.infrastructure.taskiq.tasks import _try_auto_purchase

        await _try_auto_purchase(container, payment_id)
        return

    # Owner-editable template per purchase kind — the same mapping the webhook path uses,
    # so a renewal says «продлена», not a generic "activated" (toggle only swaps the text).
    from src.web.routes.admin.notifications import notification_text

    event = "purchase"
    if txn is not None and txn.purchase_type is PurchaseType.RENEW:
        event = "renewal"
    elif txn is not None and txn.purchase_type is PurchaseType.CHANGE:
        event = "plan_changed"
    elif txn is not None and txn.purchase_type is PurchaseType.TRAFFIC_TOPUP:
        event = "traffic_topup"
    plan_name = str((sub.plan_snapshot or {}).get("name") or "") if sub else ""
    expire = sub.expire_at.strftime("%d.%m.%Y") if sub and sub.expire_at else ""
    async with container.uow() as uow:
        text = await notification_text(
            uow,
            event,
            # Escape user- and owner-supplied values interpolated into an HTML template:
            # a name/plan with <, > or & would make Telegram 400 the confirmation after
            # the charge already settled (see balance_topup above).
            name=hesc((user.first_name if user else "") or ""),
            plan=hesc(plan_name),
            expire=expire,
        )
    text = text or "✅ <b>Оплата получена — подписка активирована!</b>"
    if sub is not None and sub.subscription_url and event != "traffic_topup":
        text += f"\n\nСсылка подписки:\n<code>{sub.subscription_url}</code>"
    await message.answer(text, parse_mode="HTML")
