"""Mini-app cabinet API: Telegram initData auth + subscriber self-service.

Auth: every request carries ``Authorization: tma <initData>`` (Telegram Mini Apps).
The user is resolved (or created) by the verified telegram id — the same row the bot
maintains. Endpoints mirror miniapp/CONTRACT.md and power all 8 visual themes.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.application.common.payments import PaymentContext, PaymentResultKind
from src.application.dto.pricing import PurchaseRequest
from src.application.events import UserRegistered
from src.application.services.connection import (
    build_deep_links,
    connection_apps,
    parse_enabled_apps,
)
from src.application.services.ids import generate_referral_code
from src.application.services.promo import PromoError
from src.bot.handlers.purchase import fmt_money  # single source for money labels (#7)
from src.core.constants import MAX_DEPOSIT_AMOUNT_MINOR
from src.core.enums import (
    Currency,
    Locale,
    PurchaseType,
    TransactionStatus,
    TransactionType,
    UserStatus,
)
from src.core.exceptions import (
    DomainError,
    InsufficientBalance,
    InvalidStateTransition,
    RemnawaveError,
)
from src.core.logging import get_logger
from src.core.money import Money
from src.core.security import validate_init_data
from src.infrastructure.database.models.plan import Plan
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.infrastructure.payments.crypto import decrypt_gateway_settings
from src.web.deps import get_container

log = get_logger(__name__)

router = APIRouter(prefix="/api/cabinet", tags=["cabinet"])

GIB = 1024**3

# MAX_DEPOSIT_AMOUNT_MINOR (self-serve top-up ceiling) now lives in src/core/constants.py —
# shared with the bot's own topup flow (src/bot/handlers/purchase.py) so the two surfaces
# can never drift apart.


def _gateway_methods(gateways: Iterable[Any], supported: Iterable[Any]) -> list[dict[str, str]]:
    """Online payment methods for the cabinet, expanding a form-routable gateway (Platega)
    into one entry per enabled method — id ``"platega@sbp"``, label ``"Platega · СБП"``."""
    from src.application.services.pay_forms import gateway_form_options

    out: list[dict[str, str]] = []
    for g in gateways:
        if not (
            g.is_active and g.type in supported and g.type.value not in ("manual", "telegram_stars")
        ):
            continue
        name = g.display_name or g.type.value
        options = gateway_form_options(g.type, (g.settings or {}).get("enabled_forms"))
        if options:
            out.extend(
                {"id": f"{g.type.value}@{form}", "label": f"{name} · {flabel}"}
                for form, flabel in options
            )
        else:
            out.append({"id": g.type.value, "label": name})
    return out


def _proxy_link(raw: str) -> str:
    """Normalize an MTProto proxy link to the https://t.me/proxy?... form."""
    raw = raw.strip()
    if raw.startswith("tg://proxy"):
        return "https://t.me/proxy" + raw.removeprefix("tg://proxy")
    if raw.startswith("t.me/"):
        return "https://" + raw
    return raw


async def cabinet_user(request: Request, container: AppContainer = Depends(get_container)) -> User:
    auth = request.headers.get("Authorization", "")
    # Web-cabinet users authenticate with a Bearer access JWT (email/OAuth login),
    # everyone else with Telegram Mini App initData. Same endpoints serve both.
    if auth.startswith("Bearer "):
        from src.web.routes.cabinet_auth import web_user_from_bearer

        web_user = await web_user_from_bearer(request, container)
        if web_user is None:
            raise HTTPException(401, "bad token")
        return web_user
    if not auth.startswith("tma "):
        raise HTTPException(401, "unauthorized")
    data = validate_init_data(auth.removeprefix("tma "), container.settings.bot.token)
    if data is None or "user_parsed" not in data:
        raise HTTPException(401, "bad initData")
    tg: dict[str, Any] = data["user_parsed"]
    tg_id = int(tg["id"])

    async with container.uow() as uow:
        # Race-safe: the mini-app can fire several /me calls for a brand-new user at once;
        # get_or_create makes the losers re-read instead of crashing on the duplicate
        # telegram_id (previously the #1 crash in telemetry — IntegrityError on /api/cabinet/me).
        user, created = await uow.users.get_or_create(
            User(
                telegram_id=tg_id,
                username=tg.get("username"),
                first_name=tg.get("first_name"),
                last_name=tg.get("last_name"),
                language=Locale.EN if (tg.get("language_code") or "ru")[:2] == "en" else Locale.RU,
                referral_code=generate_referral_code(),
            )
        )
        await uow.commit()
    if created:
        # First contact came through the mini-app — same event the bot middleware emits.
        await container.event_bus.publish(UserRegistered(user_id=user.id, telegram_id=tg_id))
    if user.status is UserStatus.BLOCKED:
        raise HTTPException(403, "blocked")
    return user


def _sub_payload(sub: Any, *, hide_link: bool = False) -> dict[str, Any] | None:
    if sub is None:
        return None
    return {
        "status": sub.status.value,
        "is_trial": sub.is_trial,
        "plan_id": sub.plan_id,
        "plan_name": (sub.plan_snapshot or {}).get("name"),
        "start_at": sub.start_at.isoformat() if sub.start_at else None,
        "expire_at": sub.expire_at.isoformat() if sub.expire_at else None,
        "device_limit": sub.device_limit,
        "traffic": {
            "used_bytes": sub.traffic_used_bytes,
            "limit_bytes": sub.traffic_limit_bytes,
            "unlimited": not sub.traffic_limit_bytes,
        },
        # HIDE_SUBSCRIPTION_LINK hides the raw copyable URL on every surface (bot + all cabinets),
        # but keeps crypto_link so one-tap import still works (#5). Single-sourced here so /me and
        # /trial match /connection.
        "subscription_url": None if hide_link else sub.subscription_url,
        "crypto_link": sub.crypto_link,
        "autopay_enabled": sub.autopay_enabled,  # #10: contract field, was never returned
    }


@router.get("/me")
async def me(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        cfg = container.bot_config
        miniapp = await uow.miniapp.get_or_create()
        trial_enabled = bool(await cfg.value(uow, "TRIAL_ENABLED"))
        bot_username = str(await cfg.value(uow, "BOT_USERNAME") or "")
        proxy_enabled = bool(await cfg.value(uow, "MTPROTO_PROXY_ENABLED"))
        proxy_url = str(await cfg.value(uow, "MTPROTO_PROXY_URL") or "")
        sales_mode = str(await cfg.value(uow, "SALES_MODE"))
        hide_link = bool(await cfg.value(uow, "HIDE_SUBSCRIPTION_LINK"))
        show_traffic = bool(await cfg.value(uow, "SHOW_TRAFFIC_USAGE"))
        min_deposit_minor = int(await cfg.value(uow, "MIN_DEPOSIT_AMOUNT"))
        gateways = _gateway_methods(
            await uow.payment_gateways.list(), container.gateway_factory.supported()
        )
        # Operator-controlled payment-method order + Balance/Stars labels (shared with the bot).
        from src.core.payment_order import order_rank

        balance_on = bool(await cfg.value(uow, "BALANCE_ENABLED"))
        pay_balance_label = str(await cfg.value(uow, "PAYMENT_BALANCE_LABEL") or "").strip()
        pay_stars_label = str(await cfg.value(uow, "PAYMENT_STARS_LABEL") or "").strip()
        rank = order_rank(str(await cfg.value(uow, "PAYMENT_METHOD_ORDER") or ""))
        payment_order = (["balance"] if balance_on else []) + ["stars"]
        payment_order += [g["id"] for g in gateways]
        payment_order.sort(key=rank)  # stable — unlisted methods keep default order
        if sub is not None and sub.status.is_usable:
            await container.traffic.refresh_used_bytes(sub)
        await uow.commit()
    return {
        "user": {
            "id": user.id,
            "first_name": user.first_name,
            "username": user.username,
            "language": user.language.value,
            "currency": user.currency.value,
            "balance_minor": user.balance_minor,
            "referral_code": user.referral_code,
            "personal_discount_pct": user.personal_discount_pct,
            "is_trial_available": trial_enabled and user.is_trial_available,
        },
        "subscription": _sub_payload(sub, hide_link=hide_link),
        "app": {
            "template": miniapp.template,
            "title": miniapp.title,
            "greeting": miniapp.greeting,
            "accent_color": miniapp.accent_color,
            "bot_username": bot_username,
            "mtproto_proxy": _proxy_link(proxy_url) if proxy_enabled and proxy_url else None,
            "ui": miniapp.ui or {},
            "payment_methods": gateways,
            "payment_order": payment_order,
            "pay_balance_label": pay_balance_label,
            "pay_stars_label": pay_stars_label,
            "balance_enabled": balance_on,
            "hide_subscription_link": hide_link,
            "show_traffic_usage": show_traffic,  # #8: honored by bot; now exposed to cabinets too
            "sales_mode": sales_mode,
            # MIN_DEPOSIT_AMOUNT — floor the top-up UI must respect when building its amount
            # presets; the /topup endpoint re-validates it server-side regardless (#topup).
            "min_deposit_minor": max(1, min_deposit_minor),
            # Fixed ceiling (not admin-configurable, see src/core/constants.py) — lets the
            # top-up UI validate a free-typed custom amount client-side before it round-trips
            # to a 400 (#3/#4). /topup re-validates it server-side regardless.
            "max_deposit_minor": MAX_DEPOSIT_AMOUNT_MINOR,
        },
    }


async def _plan_items(container: AppContainer, user: User | None = None) -> list[dict[str, Any]]:
    """Catalogue. With a ``user`` the per-duration price is the QUOTED final (personal + promo +
    sale, cap 100%) so the browse price equals the charge (#4); ``base_price_minor`` keeps the
    list price for a strikethrough. Without a user (public/guest) it returns base prices only.
    """
    from src.application.dto.pricing import PurchaseRequest

    async with container.uow() as uow:
        rows = [p for p in await uow.plans.list_with_durations() if p.is_active and not p.is_trial]
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))
        current_plan_id: int | None = None
        if user is not None and user.current_subscription_id:
            cur = await uow.subscriptions.get(user.current_subscription_id)
            current_plan_id = cur.plan_id if cur else None
        items = []
        for p in sorted(rows, key=lambda p: p.order_index):
            ptype = sub_id = None
            if user is not None:
                ptype, sub_id = await container.purchase.resolve_purchase_type(uow, user.id, p.id)
            durations = []
            for d in p.durations:
                rub = next((pr.price_minor for pr in d.prices if pr.currency is Currency.RUB), None)
                if rub is None:
                    continue
                final = rub
                if user is not None:
                    req = PurchaseRequest(
                        user_id=user.id,
                        plan_id=p.id,
                        duration_days=d.days,
                        currency=Currency.RUB,
                        purchase_type=ptype,  # type: ignore[arg-type]
                        subscription_id=sub_id,
                    )
                    with contextlib.suppress(Exception):
                        final = (await container.pricing.quote(uow, req)).final.amount_minor
                durations.append(
                    {
                        "days": d.days,
                        "months": round(d.days / 30) or 1,
                        "price_minor": final,
                        "base_price_minor": rub,
                        "price_stars": max(1, math.ceil(final / max(1, stars_rate))),
                    }
                )
            items.append(
                {
                    "id": p.id,
                    "public_code": p.public_code,
                    "name": p.name,
                    "description": p.description,
                    "traffic_limit_bytes": p.traffic_limit_bytes,
                    "device_limit": p.device_limit,
                    "is_current": user is not None and p.id == current_plan_id,
                    "durations": durations,
                }
            )
    return items


@router.get("/plans")
async def plans(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    return {"currency": "RUB", "items": await _plan_items(container, user)}


@router.get("/public/plans")
async def public_plans(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Unauthenticated tariff + online-method list — the web/guest checkout needs it
    before login. Gated by WEB_CABINET_ENABLED so it isn't exposed unless the web
    storefront is on."""
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "WEB_CABINET_ENABLED")):
            raise HTTPException(403, "web cabinet is disabled")
        methods = _gateway_methods(
            await uow.payment_gateways.list(), container.gateway_factory.supported()
        )
    return {"currency": "RUB", "items": await _plan_items(container), "payment_methods": methods}


@router.get("/public/landing")
async def public_landing(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Everything the public marketing site (served at ``/``) needs, unauthenticated:
    the chosen theme + branding, hero/features/FAQ copy, the tariff list, and where the
    «Личный кабинет» button points (the web auth window or the Telegram bot)."""
    async with container.uow() as uow:
        cfg = container.bot_config
        miniapp = await uow.miniapp.get_or_create()
        landing = dict((miniapp.ui or {}).get("landing") or {})
        web_enabled = bool(await cfg.value(uow, "WEB_CABINET_ENABLED"))
        bot_username = str(await cfg.value(uow, "BOT_USERNAME") or "")
        # The web cabinet is always mounted at /web on this same origin. Use the relative
        # path (not CABINET_URL, which may be a bare domain now fronted by this landing —
        # that would loop the «Личный кабинет» button back here).
        cabinet_url = "/web/"
        await uow.commit()
    # No web cabinet → the CTA can only go to the bot, whatever the admin picked.
    target = landing.get("cta_target") or "web"
    if target == "web" and not web_enabled:
        target = "bot"
    return {
        "enabled": landing.get("enabled", True),
        "template": miniapp.template,
        "title": miniapp.title,
        "greeting": miniapp.greeting,
        "accent_color": miniapp.accent_color,
        "headline": landing.get("headline") or "",
        "subheadline": landing.get("subheadline") or "",
        "features": landing.get("features") or [],
        "faq": landing.get("faq") or [],
        "cta_target": target,
        "bot_username": bot_username,
        "cabinet_url": cabinet_url,
        "currency": "RUB",
        "plans": await _plan_items(container),
    }


@router.get("/constructor")
async def constructor(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Constructor-mode price list: active periods + traffic packs (SALES_MODE=constructor)."""
    async with container.uow() as uow:
        periods = [p for p in await uow.constructor_periods.list() if p.is_active]
        packs = [t for t in await uow.traffic_packs.list() if t.is_active]
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))
    return {
        "currency": "RUB",
        "stars_rate": max(1, stars_rate),
        "periods": [
            {
                "id": p.id,
                "days": p.days,
                "months": round(p.days / 30) or 1,
                "price_minor": p.price_minor,
            }
            for p in sorted(periods, key=lambda p: p.days)
        ],
        "traffic_packs": [
            {"id": t.id, "gb": t.gb, "price_minor": t.price_minor}
            for t in sorted(packs, key=lambda t: (t.gb == 0, t.gb))
        ],
    }


@router.get("/referral")
async def referral(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        cfg = container.bot_config
        bot_username = str(await cfg.value(uow, "BOT_USERNAME") or "")
        bonus_days = int(await cfg.value(uow, "REFERRAL_BONUS_DAYS"))
        percent = int(await cfg.value(uow, "REFERRAL_PERCENT"))
        invited = await uow.users.count(referred_by_id=user.id)
        earnings_minor = await uow.referral_earnings.total_minor(user.id)
    return {
        "code": user.referral_code,
        "link": f"https://t.me/{bot_username}?start=ref_{user.referral_code}",
        "bonus_days": bonus_days,
        "commission_percent": percent,
        "invited_count": invited,
        "earnings_minor": earnings_minor,
    }


@router.get("/payments")
async def payments_history(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        txs = await uow.transactions.list_recent(user.id, limit=20)
    return {
        "items": [
            {
                "id": t.id,
                "type": t.type.value,
                "status": t.status.value,
                "amount_minor": t.amount_minor,
                "currency": t.currency.value,
                "method": t.payment_method or (t.gateway_type.value if t.gateway_type else None),
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in sorted(txs, key=lambda t: t.id, reverse=True)
        ]
    }


class PurchaseIn(BaseModel):
    # Plans mode: plan_id (or public_code) + days. Constructor mode: period_id + pack_id instead.
    plan_id: int | None = None
    public_code: str | None = Field(None, max_length=64)  # #6: template clients send this
    days: int | None = Field(None, ge=1, le=3650)
    period_id: int | None = None
    pack_id: int | None = None
    method: str = Field("balance", max_length=32)  # balance | stars | <gateway type>


@router.post("/purchase")
async def purchase(
    body: PurchaseIn,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    # Accept public_code as an alias for plan_id so the documented template contract works (#6).
    plan_id = body.plan_id
    if plan_id is None and body.public_code:
        async with container.uow() as uow:
            plan = await uow.plans.find_one(public_code=body.public_code)
        plan_id = plan.id if plan is not None else None

    if body.period_id is not None and body.pack_id is not None:
        async with container.uow() as uow:
            device_limit = int(await container.bot_config.value(uow, "DEFAULT_DEVICE_LIMIT"))
            try:
                req = await container.purchase.build_constructor_request(
                    uow,
                    user_id=user.id,
                    period_id=body.period_id,
                    pack_id=body.pack_id,
                    device_limit=device_limit,
                )
            except DomainError as exc:
                raise HTTPException(400, str(exc)) from exc
            await uow.commit()  # the hidden constructor plan may have just been created
    elif plan_id is not None and body.days is not None:
        async with container.uow() as uow:
            ptype, sub_id = await container.purchase.resolve_purchase_type(uow, user.id, plan_id)
        req = PurchaseRequest(
            user_id=user.id,
            plan_id=plan_id,
            duration_days=body.days,
            currency=Currency.RUB,
            purchase_type=ptype,
            subscription_id=sub_id,
        )
    else:
        raise HTTPException(400, "plan_id/public_code + days or period_id + pack_id required")

    if body.method == "balance":
        async with container.uow() as uow:
            if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
                raise HTTPException(400, "balance payments are disabled")
            try:
                # Shared checkout path — identical to the bot's balance purchase.
                await container.purchase.checkout_from_balance(uow, req)
            except InsufficientBalance as exc:
                raise HTTPException(402, "insufficient balance") from exc
            except InvalidStateTransition as exc:
                raise HTTPException(409, "already processed") from exc
            except RemnawaveError as exc:
                log.error("cabinet provision failed", error=str(exc))
                raise HTTPException(502, "provisioning temporarily unavailable") from exc
            except DomainError as exc:
                raise HTTPException(400, str(exc)) from exc
            await uow.commit()
        return {"ok": True, "paid_with": "balance"}

    if body.method not in ("balance", "stars"):
        return await _pay_with_gateway(container, user, req, body.method)

    # Stars: pending tx + invoice link opened via Telegram.WebApp.openInvoice.
    async with container.uow() as uow:
        try:
            txn, quote = await container.purchase.start(uow, req)
        except RemnawaveError as exc:
            # A 100%-discount purchase fulfils inline and hits the panel; a panel blip must map
            # to 502 (retryable), never a 400 that leaks the raw panel response body to the user.
            log.error("cabinet free provision failed", error=str(exc))
            raise HTTPException(502, "provisioning temporarily unavailable") from exc
        except DomainError as exc:
            raise HTTPException(400, str(exc)) from exc
        if quote.is_free:
            await uow.commit()  # start() already fulfilled the 100%-discount purchase
            return {"ok": True, "paid_with": "free"}
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))
        title = str((txn.plan_snapshot or {}).get("name") or "VPN")
        await uow.commit()
        payment_id = str(txn.payment_id)
        stars = max(1, math.ceil(quote.final.amount_minor / max(1, stars_rate)))

    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import LabeledPrice

    bot = Bot(token=container.settings.bot.token)
    try:
        try:
            link = await bot.create_invoice_link(
                title=f"{title} · {req.duration_days} дн.",
                description="Оплата VPN-подписки",
                payload=payment_id,
                currency="XTR",
                prices=[LabeledPrice(label="VPN", amount=stars)],
            )
        except TelegramAPIError as exc:
            # Same orphan-PENDING-transaction hazard as /topup below — the txn was already
            # committed above (start() needs it to exist before quoting), so a refused invoice
            # must cancel it rather than leave a permanent «⏳» in the user's history (#1).
            # TelegramAPIError (not just TelegramBadRequest) — create_invoice_link can just as
            # well fail on a network blip, a flood-control retry-after, or the bot token being
            # revoked; aiogram wraps all three (plus a plain bad-request) under this one base,
            # so a narrower catch left the transaction stranded PENDING forever on any of them.
            log.warning(
                "cabinet purchase invoice failed",
                amount_minor=quote.final.amount_minor,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            async with container.uow() as uow:
                await uow.transactions.transition_status(
                    UUID(payment_id), TransactionStatus.CANCELED, (TransactionStatus.PENDING,)
                )
                await uow.commit()
            raise HTTPException(
                502, "failed to create the Stars invoice — try again or another payment method"
            ) from exc
    finally:
        await bot.session.close()
    return {"ok": True, "invoice_link": link}


async def _pay_with_gateway(
    container: AppContainer, user: User, req: PurchaseRequest, method: str
) -> dict[str, Any]:
    """Hosted payment: pending tx -> provider invoice -> redirect URL.

    The provider webhook completes the transaction through the standard idempotent
    pipeline; nothing is fulfilled here.
    """
    from src.application.services.pay_forms import split_method
    from src.core.enums import PaymentGatewayType

    gateway_value, form = split_method(method)  # "platega@sbp" -> ("platega", "sbp")
    try:
        gtype = PaymentGatewayType(gateway_value)
    except ValueError as exc:
        raise HTTPException(400, "unknown payment method") from exc

    async with container.uow() as uow:
        gateways = await uow.payment_gateways.list()
        offered = {
            m["id"] for m in _gateway_methods(gateways, container.gateway_factory.supported())
        }
        if method not in offered:
            # Cross-check against exactly what /me's payment_methods (built by this same
            # _gateway_methods()) advertise — "manual"/"telegram_stars" pass the type+is_active
            # checks below but are deliberately excluded from the offered list (#5); previously
            # only "the gateway never returns REDIRECT" happened to save us here.
            raise HTTPException(400, "payment method is not enabled")
        row = await uow.payment_gateways.get_active(gtype)
        if row is None or gtype not in container.gateway_factory.supported():
            raise HTTPException(400, "payment method is not enabled")
        settings = decrypt_gateway_settings(container.secret_box, dict(row.settings))
        try:
            txn, quote = await container.purchase.start(uow, req)
        except RemnawaveError as exc:
            log.error("cabinet free provision failed", error=str(exc))
            raise HTTPException(502, "provisioning temporarily unavailable") from exc
        except DomainError as exc:
            raise HTTPException(400, str(exc)) from exc
        if quote.is_free:
            await uow.commit()
            return {"ok": True, "paid_with": "free"}
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        title = str((txn.plan_snapshot or {}).get("name") or "VPN")
        gateway = container.gateway_factory.create(gtype, settings)
        try:
            result = await gateway.create_payment(
                PaymentContext(
                    payment_id=txn.payment_id,
                    amount=Money(quote.final.amount_minor, txn.currency),
                    description=f"{title} · {req.duration_days} дн.",
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    return_url=miniapp_url or None,
                    metadata={"form": form} if form else {},
                )
            )
        except Exception as exc:
            log.error("gateway create_payment failed", gateway=method, error=str(exc))
            raise HTTPException(502, f"panel error: provider {method} failed") from exc
        if result.kind is not PaymentResultKind.REDIRECT or not result.redirect_url:
            raise HTTPException(502, f"provider {method} returned no payment url")
        txn.gateway_type = gtype
        txn.external_id = result.external_id
        txn.gateway_display_name = row.display_name or gtype.value
        await uow.commit()
        return {"ok": True, "redirect_url": result.redirect_url}


class TopupIn(BaseModel):
    # Upper bound checked in the handler (not here) so it shares the 400 code path with the
    # rest of this endpoint's errors instead of a bare pydantic 422 (#topup-max-code).
    amount_minor: int = Field(..., gt=0)
    method: str = Field(..., max_length=32)  # stars | <gateway type>[@form] — never "balance"


@router.post("/topup")
async def topup(
    body: TopupIn,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Wallet top-up: pending DEPOSIT tx -> Stars invoice or gateway redirect.

    Unlike /purchase this never touches ``PurchaseService.start()`` (always
    SUBSCRIPTION_PAYMENT) — the transaction is built here as a plain DEPOSIT, exactly like the
    bot's ``topup_amount`` handler. Fulfilment (crediting the balance) is generic: any COMPLETED
    DEPOSIT transaction is credited by ``PaymentService._fulfill`` regardless of its source.
    """
    if body.method == "balance":
        raise HTTPException(400, "cannot top up the balance from the balance")

    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "BALANCE_ENABLED")):
            # Old UI / cached responses could still show a top-up button after the owner turns
            # the wallet off — refuse here, or the money lands in a wallet nothing credits.
            raise HTTPException(400, "balance top-ups are disabled")
        min_dep = int(await container.bot_config.value(uow, "MIN_DEPOSIT_AMOUNT"))
        stars_rate = int(await container.bot_config.value(uow, "STARS_RATE_RUB"))

    # amount_minor is client-supplied and forgeable — re-validate the floor AND ceiling
    # server-side, never trust the UI preset the request claims to have used. Both bounds
    # raise the same 400 (not pydantic's 422 for the upper one) — consistent with every other
    # error on this endpoint and with the documented contract (#4).
    if body.amount_minor < max(1, min_dep):
        raise HTTPException(400, "amount below the minimum top-up")
    if body.amount_minor > MAX_DEPOSIT_AMOUNT_MINOR:
        raise HTTPException(400, "amount above the maximum top-up")

    txn = Transaction(
        user_id=user.id,
        type=TransactionType.DEPOSIT,
        status=TransactionStatus.PENDING,
        amount_minor=body.amount_minor,
        currency=Currency.RUB,
    )

    if body.method != "stars":
        return await _topup_with_gateway(container, user, txn, body.method)

    # Stars: pending tx + invoice link opened via Telegram.WebApp.openInvoice.
    async with container.uow() as uow:
        await uow.transactions.add(txn)
        await uow.commit()
        payment_id = str(txn.payment_id)
    stars = max(1, math.ceil(body.amount_minor / max(1, stars_rate)))

    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import LabeledPrice

    bot = Bot(token=container.settings.bot.token)
    try:
        try:
            link = await bot.create_invoice_link(
                title="Пополнение баланса",
                description=f"Пополнение на {fmt_money(body.amount_minor)}",
                payload=payment_id,
                currency="XTR",
                prices=[LabeledPrice(label="Баланс", amount=stars)],
            )
        except TelegramAPIError as exc:
            # Invoice refused (amount out of Telegram's limits, bot blocked, network hiccup…) —
            # cancel the orphan PENDING txn instead of stranding it as an eternal «⏳ Пополнение»
            # that list_stuck_pending() can never pick up (no gateway_type/external_id on a Stars
            # row). Mirrors the bot's own _topup_with_stars (src/bot/handlers/purchase.py) (#1).
            # TelegramAPIError (not just TelegramBadRequest) — aiogram wraps network errors and
            # flood-control retry-after under this same base, and a revoked/unauthorized bot
            # token raises TelegramUnauthorizedError, not TelegramBadRequest; the narrower catch
            # let every one of those slip past `except` and 500 out with the txn stuck PENDING.
            log.warning(
                "cabinet topup invoice failed",
                amount_minor=body.amount_minor,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            async with container.uow() as uow:
                await uow.transactions.transition_status(
                    UUID(payment_id), TransactionStatus.CANCELED, (TransactionStatus.PENDING,)
                )
                await uow.commit()
            raise HTTPException(
                502, "failed to create the Stars invoice — try again or another amount"
            ) from exc
    finally:
        await bot.session.close()
    return {"ok": True, "invoice_link": link}


async def _topup_with_gateway(
    container: AppContainer, user: User, txn: Transaction, method: str
) -> dict[str, Any]:
    """Hosted top-up: pending DEPOSIT tx -> provider invoice -> redirect URL.

    Mirrors ``_pay_with_gateway`` above; the provider webhook completes the transaction
    through the standard idempotent pipeline, crediting the balance — nothing is fulfilled
    here.
    """
    from src.application.services.pay_forms import split_method
    from src.core.enums import PaymentGatewayType

    gateway_value, form = split_method(method)  # "platega@sbp" -> ("platega", "sbp")
    try:
        gtype = PaymentGatewayType(gateway_value)
    except ValueError as exc:
        raise HTTPException(400, "unknown payment method") from exc

    async with container.uow() as uow:
        gateways = await uow.payment_gateways.list()
        offered = {
            m["id"] for m in _gateway_methods(gateways, container.gateway_factory.supported())
        }
        if method not in offered:
            # See the matching guard in _pay_with_gateway above (#5).
            raise HTTPException(400, "payment method is not enabled")
        row = await uow.payment_gateways.get_active(gtype)
        if row is None or gtype not in container.gateway_factory.supported():
            raise HTTPException(400, "payment method is not enabled")
        settings = decrypt_gateway_settings(container.secret_box, dict(row.settings))
        await uow.transactions.add(txn)
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        gateway = container.gateway_factory.create(gtype, settings)
        try:
            result = await gateway.create_payment(
                PaymentContext(
                    payment_id=txn.payment_id,
                    amount=Money(txn.amount_minor, txn.currency),
                    description=f"Пополнение баланса · {fmt_money(txn.amount_minor)}",
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    return_url=miniapp_url or None,
                    metadata={"form": form} if form else {},
                )
            )
        except Exception as exc:
            log.error("gateway create_payment failed", gateway=method, error=str(exc))
            raise HTTPException(502, f"panel error: provider {method} failed") from exc
        if result.kind is not PaymentResultKind.REDIRECT or not result.redirect_url:
            raise HTTPException(502, f"provider {method} returned no payment url")
        txn.gateway_type = gtype
        txn.external_id = result.external_id
        txn.gateway_display_name = row.display_name or gtype.value
        await uow.commit()
        return {"ok": True, "redirect_url": result.redirect_url}


class PromoIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=64)


@router.post("/promocode")
async def apply_promocode(
    body: PromoIn,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        u = await uow.users.get(user.id)
        assert u is not None
        try:
            reward = await container.promo.apply(uow, u, body.code.strip().upper())
        except PromoError as exc:
            return {"ok": False, "reward_type": None, "message": str(exc)}
        await uow.commit()
    return {"ok": True, "reward_type": reward.value, "message": None}  # #11: consistent shape


class SupportIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


def _support_msg(m: Any) -> dict[str, Any]:
    from src.core.enums import TicketAuthor

    text = m.text[2:].strip() if m.text.startswith("🤖 ") else m.text
    return {
        "from": "you" if m.author is TicketAuthor.USER else "support",
        "text": text,
        "at": m.created_at.isoformat(),
    }


@router.get("/support")
async def support_history(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Current support conversation (for the mini-app chat + polling operator replies)."""
    from src.core.enums import TicketStatus

    async with container.uow() as uow:
        tickets = await uow.tickets.list(user_id=user.id)
        active = next((t for t in tickets if t.status is not TicketStatus.CLOSED), None)
        if active is None:
            return {"ticket_id": None, "status": None, "messages": []}
        rows = await uow.ticket_messages.list(ticket_id=active.id)
        msgs = sorted(rows, key=lambda m: m.id)  # base DAO list() has no ORDER BY
        return {
            "ticket_id": active.id,
            "status": active.status.value,
            "messages": [_support_msg(m) for m in msgs],
        }


@router.post("/support")
async def support_send(
    body: SupportIn,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Post a support message; the AI answers/escalates (same engine as the bot tickets)."""
    from src.application.events import TicketOpened
    from src.core.enums import TicketAuthor, TicketStatus
    from src.infrastructure.database.base import utcnow
    from src.infrastructure.database.models.ticket import Ticket, TicketMessage

    text = (body.text or "").strip()[:4096]
    if not text:
        raise HTTPException(400, "empty message")
    async with container.uow() as uow:
        tickets = await uow.tickets.list(user_id=user.id)
        active = next((t for t in tickets if t.status is not TicketStatus.CLOSED), None)
        created = active is None
        if active is None:
            active = Ticket(user_id=user.id, subject=text[:64])
            await uow.tickets.add(active)
        await uow.ticket_messages.add(
            TicketMessage(ticket_id=active.id, author=TicketAuthor.USER, text=text)
        )
        active.status = TicketStatus.OPEN
        active.updated_at = utcnow()
        await uow.commit()
        ticket_id = active.id
    if created:
        await container.event_bus.publish(
            TicketOpened(
                ticket_id=ticket_id,
                user_id=user.id,
                telegram_id=user.telegram_id or 0,
                username=user.username,
                subject=text[:64],
            )
        )
    outcome, ai_text = await container.ai_support.handle_ticket(user, ticket_id)
    return {"ok": True, "ticket_id": ticket_id, "ai_outcome": outcome, "ai_reply": ai_text}


@router.post("/trial")
async def activate_trial(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    async with container.uow() as uow:
        cfg = container.bot_config
        if not bool(await cfg.value(uow, "TRIAL_ENABLED")):
            raise HTTPException(400, "trial disabled")
        # FOR UPDATE: two concurrent /trial calls must not both pass the check.
        u = await uow.users.lock_for_update(user.id)
        if u is None or not u.is_trial_available:
            raise HTTPException(400, "trial already used")
        # A trial grant() creates a NEW panel user and overwrites current_subscription_id, so
        # activating it over a live (usually paid) subscription orphans that subscription and
        # hides its remaining days. Refuse while a usable subscription exists (buy-first users
        # keep is_trial_available=True, so the flag alone doesn't guard this).
        if u.current_subscription_id is not None:
            existing = await uow.subscriptions.get(u.current_subscription_id)
            if existing is not None and existing.status.is_usable:
                raise HTTPException(409, "already have an active subscription")
        days = int(await cfg.value(uow, "TRIAL_DURATION_DAYS"))
        traffic_gb = int(await cfg.value(uow, "TRIAL_TRAFFIC_GB"))
        devices = int(await cfg.value(uow, "TRIAL_DEVICE_LIMIT"))
        hide_link = bool(await cfg.value(uow, "HIDE_SUBSCRIPTION_LINK"))
        # Paid trial: charge the wallet before provisioning, exactly like the bot (#3) — grant()
        # itself never charges, so without this the mini-app/web gave a configured paid trial free.
        trial_price = int(await cfg.value(uow, "TRIAL_PRICE"))
        if trial_price > 0 and not await uow.users.debit_balance_guarded(u, trial_price):
            raise HTTPException(402, "insufficient balance for the paid trial")
        plan = await uow.plans.find_one(is_trial=True) or await uow.plans.find_one(name="Trial")
        if plan is not None and not plan.is_trial:
            plan.is_trial = True
        if plan is None:
            plan = Plan(
                public_code="trial",
                name="Trial",
                is_trial=True,
                is_active=False,
                traffic_limit_bytes=traffic_gb * GIB or None,
                device_limit=devices,
            )
            await uow.plans.add(plan)
        req = PurchaseRequest(
            user_id=u.id,
            plan_id=plan.id,
            duration_days=days,
            currency=Currency.RUB,
            purchase_type=PurchaseType.NEW,
        )
        try:
            sub = await container.subscriptions.grant(
                uow, user=u, plan=plan, req=req, is_trial=True
            )
        except RemnawaveError as exc:
            raise HTTPException(502, "provisioning temporarily unavailable") from exc
        await uow.commit()
        from src.application.events import TrialGranted

        await container.event_bus.publish(TrialGranted(user_id=u.id, subscription_id=sub.id))
        return {"ok": True, "days": days, "subscription": _sub_payload(sub, hide_link=hide_link)}


@router.get("/connection")
async def connection(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Step-2 data for the Connect tab: personal subscription URL + deep links."""
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        hide_link = bool(await container.bot_config.value(uow, "HIDE_SUBSCRIPTION_LINK"))
        enabled = parse_enabled_apps(str(await container.bot_config.value(uow, "CONNECTION_APPS")))
    if sub is None or not sub.status.is_usable or not sub.subscription_url:
        raise HTTPException(404, "no active subscription")
    url = sub.subscription_url
    apps = connection_apps(url, sub.crypto_link, enabled)
    return {
        # When the owner hides the raw link, the app still imports via deep links; it just
        # doesn't render the copyable URL box (HIDE-1). Deep links stay so import keeps working.
        "subscription_url": None if hide_link else url,
        "expires_at": sub.expire_at.isoformat() if sub.expire_at else None,
        # apps = only the owner-enabled clients (ordered); deep_links kept for back-compat.
        "apps": apps,
        "deep_links": {a["key"]: a["deep_link"] for a in apps},
        "hide_link": hide_link,
    }


@router.post("/subscription/reset-link")
@router.post("/subscription/reset-devices")  # #7: contract/client name — same reset, kicks devices
async def reset_link(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Self-serve: rotate the subscription URL on the panel (revoke) and drop stale sessions.

    Rate-limited to once per 10 min per user so tap-spam can't churn the panel; the old link
    stops working immediately, so the client must show and re-import the returned new URL.
    """
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
    panel_ref = sub.panel_ref if sub is not None else None
    if sub is None or not sub.status.is_usable or panel_ref is None:
        raise HTTPException(404, "no active subscription")
    if not await container.redis.set(f"resetlink:{user.id}", "1", nx=True, ex=600):
        raise HTTPException(429, "link was just reset — try again in a few minutes")
    try:
        revoked = await container.remnawave_client.revoke_subscription(panel_ref)
    except Exception as exc:
        # Release the throttle so a transient panel hiccup doesn't lock the user out of retrying
        # for 10 minutes while their leaked link is still live.
        await container.redis.delete(f"resetlink:{user.id}")
        raise HTTPException(502, "panel temporarily unavailable") from exc
    new_url = revoked.subscription_url or sub.subscription_url
    async with container.uow() as uow:
        fresh = await uow.subscriptions.get(sub.id)
        if fresh is not None:
            fresh.subscription_url = new_url
            fresh.crypto_link = None  # the old happ link is stale after rotation
            await uow.commit()
    with contextlib.suppress(Exception):  # best-effort: link is already rotated
        await container.remnawave_client.drop_connections(panel_ref)
    return {
        "ok": True,  # #7: reset-devices contract shape; the url is a useful superset
        "subscription_url": new_url,
        "deep_links": build_deep_links(new_url or "", None),
    }


@router.get("/traffic")
async def traffic(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Usage-graph data: current used/limit + the last 30 daily cumulative readings.

    The client reverses the series and diffs consecutive days for per-day usage (a drop
    means the monthly traffic reset kicked in).
    """
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
        if sub is None:
            return {"used_bytes": 0, "limit_bytes": 0, "unlimited": True, "series": []}
        if sub.status.is_usable:
            await container.traffic.refresh_used_bytes(sub)
            await uow.commit()
        rows = await uow.traffic.series(sub.id, limit=30)
    return {
        "used_bytes": sub.traffic_used_bytes,
        "limit_bytes": sub.traffic_limit_bytes,
        "unlimited": not sub.traffic_limit_bytes,
        "series": [{"day": r.day, "used_bytes": r.used_bytes} for r in reversed(rows)],
    }


@router.get("/config")
async def app_config(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Public theming config (no auth) so the shell can paint before initData checks."""
    from src.infrastructure.services.oauth import PROVIDER_NAMES

    async with container.uow() as uow:
        miniapp = await uow.miniapp.get_or_create()
        cfg = container.bot_config
        # Which social logins are actually set up — the login page hides the rest instead of
        # showing a button that 400s with "provider not configured".
        oauth_providers = [
            name
            for name in PROVIDER_NAMES
            if str(await cfg.value(uow, f"OAUTH_{name.upper()}_CLIENT_ID") or "")
            and str(await cfg.value(uow, f"OAUTH_{name.upper()}_CLIENT_SECRET") or "")
        ]
        await uow.commit()
    return {
        "template": miniapp.template,
        "title": miniapp.title,
        "greeting": miniapp.greeting,
        "accent_color": miniapp.accent_color,
        "published_at": miniapp.published_at.isoformat() if miniapp.published_at else None,
        "oauth_providers": oauth_providers,
    }


@router.get("/devices")
async def list_devices(
    user: User = Depends(cabinet_user), container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """HWID devices bound to the current subscription's panel user."""
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
    panel_ref = sub.panel_ref if sub is not None else None
    if sub is None or panel_ref is None:
        return {"items": [], "device_limit": None}
    try:
        devices = await container.remnawave_client.get_devices(panel_ref)
    except Exception as exc:
        raise HTTPException(502, "panel temporarily unavailable") from exc
    return {
        "items": [
            {
                "hwid": d.hwid,
                "platform": d.platform,
                "model": d.device_model,
                "created_at": d.created_at,
            }
            for d in devices
        ],
        "device_limit": sub.device_limit,
    }


@router.delete("/devices/{hwid}")
async def delete_device(
    hwid: str,
    user: User = Depends(cabinet_user),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user.current_subscription_id
            else None
        )
    panel_ref = sub.panel_ref if sub is not None else None
    if sub is None or panel_ref is None:
        raise HTTPException(400, "no active subscription")
    try:
        await container.remnawave_client.delete_device(panel_ref, hwid[:64])
    except Exception as exc:
        raise HTTPException(502, "panel temporarily unavailable") from exc
    return {"ok": True}
