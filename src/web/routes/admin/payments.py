"""Admin: transactions + net-profit stats, payment providers config (screen 10)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, String, cast, func, or_, select

from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.infrastructure.database.models.payment_gateway import PaymentGateway
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import Page, audit, day_bounds_utc, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter()

_STATUS_FILTERS: dict[str, tuple[TransactionStatus, ...]] = {
    "ok": (TransactionStatus.COMPLETED,),
    "pending": (TransactionStatus.PENDING,),
    "failed": (TransactionStatus.FAILED, TransactionStatus.CANCELED),
    "refund": (TransactionStatus.REFUNDED,),
}

# External money only — see dashboard.py; balance purchases must not double-count.
_EXTERNAL_MONEY = or_(
    Transaction.type == TransactionType.DEPOSIT,
    (Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT)
    & Transaction.gateway_type.is_not(None),
)


def _like_escape(q: str) -> str:
    """Escape LIKE wildcards in user input so «%» doesn't match everything."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tx_stmt(status: str, q: str) -> Select[Any]:
    stmt = (
        select(Transaction, User)
        .join(User, User.id == Transaction.user_id)
        .where(Transaction.is_test.is_(False))
    )
    if status in _STATUS_FILTERS:
        stmt = stmt.where(Transaction.status.in_(_STATUS_FILTERS[status]))
    if q:
        needle = f"%{_like_escape(q.lstrip('@').lower())}%"
        stmt = stmt.where(
            or_(
                func.lower(func.coalesce(User.username, "")).like(needle),
                func.lower(cast(Transaction.payment_id, String)).like(needle),
            )
        )
    return stmt


@router.get("/payments", response_model=Page)
async def list_payments(
    status: str = Query("all"),
    q: str = Query("", max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    container: AppContainer = Depends(get_container),
) -> Page:
    async with container.uow() as uow:
        stmt = _tx_stmt(status, q)
        total = int(
            await uow.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        )
        rows = (
            await uow.session.execute(
                stmt.order_by(Transaction.id.desc()).limit(limit).offset(offset)
            )
        ).all()
        items = [
            {
                "id": t.id,
                "tx": str(t.payment_id)[:8].upper(),
                "user": f"@{u.username}" if u.username else f"id{u.id}",
                "type": t.type.value,
                "purchase_type": t.purchase_type.value if t.purchase_type else None,
                "amount_minor": t.amount_minor,
                "currency": t.currency.value,
                "gateway": t.gateway_type.value if t.gateway_type else None,
                "status": t.status.value,
                "created_at": iso(t.created_at),
                "completed_at": iso(t.completed_at),
            }
            for t, u in rows
        ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/payments/stats")
async def payment_stats(
    tax: int | None = Query(None, ge=0, le=100),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Turnover / provider fees / tax / net profit for today (screen 10 KPIs)."""
    async with container.uow() as uow:
        if tax is None:
            tax = int(await container.bot_config.value(uow, "TAX_RATE_PERCENT"))
        start, end = day_bounds_utc(0)
        rows = (
            await uow.session.execute(
                select(
                    Transaction.gateway_type,
                    func.coalesce(func.sum(Transaction.amount_minor), 0),
                    func.count(),
                )
                .where(
                    Transaction.status == TransactionStatus.COMPLETED,
                    _EXTERNAL_MONEY,
                    Transaction.completed_at >= start,
                    Transaction.completed_at < end,
                    Transaction.is_test.is_(False),
                )
                .group_by(Transaction.gateway_type)
            )
        ).all()
        gateways = {g.type: g for g in await uow.payment_gateways.list()}

    turnover = 0
    fees = 0
    per_provider: list[dict[str, Any]] = []
    for gw_type, amount, count in rows:
        amount = int(amount)
        turnover += amount
        fee_bp = gateways[gw_type].fee_bp if gw_type in gateways else 0
        fee = amount * fee_bp // 10_000
        fees += fee
        per_provider.append(
            {
                "gateway": gw_type.value if gw_type else "unknown",
                "amount_minor": amount,
                "count": count,
                "fee_minor": fee,
            }
        )
    tax_minor = (turnover - fees) * tax // 100
    return {
        "turnover_minor": turnover,
        "fees_minor": fees,
        "tax_percent": tax,
        "tax_minor": tax_minor,
        "net_profit_minor": turnover - fees - tax_minor,
        "providers": sorted(per_provider, key=lambda p: -p["amount_minor"]),
    }


# --- providers ----------------------------------------------------------------

# Catalog: human name, payment methods, config fields the UI should render.
# ``ready`` marks gateways whose charge flow is implemented in the base today;
# the rest are UI-configurable and activate once their drop-in lands.
PROVIDER_META: dict[PaymentGatewayType, dict[str, Any]] = {
    PaymentGatewayType.TELEGRAM_STARS: {
        "title": "Telegram Stars",
        "methods": "XTR-инвойсы в боте и мини-аппе",
        "fields": [],
        "ready": True,
        "emoji": "⭐",
    },
    PaymentGatewayType.MANUAL: {
        "title": "Вручную / баланс",
        "methods": "начисление админом",
        # secret guards the public webhook-confirm route — without it the manual gateway
        # rejects every webhook (fail-closed), so completions must carry X-Admin-Secret.
        "fields": ["secret"],
        "ready": True,
        "emoji": "💼",
    },
    PaymentGatewayType.YOOKASSA: {
        "title": "YooKassa",
        "methods": "карта, СБП, SberPay",
        # recurrent_enabled: "true" включает save_payment_method -> автосписания
        # по сохранённой карте (нужен включённый рекуррент на стороне ЮKassa)
        "fields": ["shop_id", "secret_key", "return_url", "recurrent_enabled"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.CRYPTOBOT: {
        "title": "CryptoBot",
        "methods": "USDT, TON, BTC",
        "fields": ["api_token"],
        "ready": True,
        "emoji": "🤖",
    },
    PaymentGatewayType.CRYPTOMUS: {
        "title": "Cryptomus",
        "methods": "крипта, 15+ монет",
        "fields": ["merchant_uuid", "api_key"],
        "ready": True,
        "emoji": "🪙",
    },
    PaymentGatewayType.TRIBUTE: {
        "title": "Tribute",
        "methods": "карта, подписки",
        "fields": ["api_key"],
        "ready": False,
        "emoji": "💠",
    },
    PaymentGatewayType.PLATEGA: {
        "title": "Platega",
        "methods": "СБП, карта, крипта",
        # payment_method: 2=SBP QR, 3=card, 11=card acquiring, 13=crypto
        "fields": ["merchant_id", "secret", "payment_method"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.HELEKET: {
        "title": "Heleket",
        "methods": "крипта",
        "fields": ["merchant_uuid", "api_key"],
        "ready": True,
        "emoji": "🪙",
    },
    PaymentGatewayType.WATA: {
        "title": "WATA",
        "methods": "карта, СБП, TPay, SberPay",
        "fields": ["api_token"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.FREEKASSA: {
        "title": "FreeKassa",
        "methods": "карта, СБП, кошельки",
        "fields": ["shop_id", "secret1", "secret2"],
        "ready": True,
        "emoji": "🧾",
    },
    PaymentGatewayType.PAYPALYCH: {
        "title": "PayPalych",
        "methods": "карта, СБП",
        "fields": ["api_token", "shop_id", "signature_token"],
        "ready": True,
        "emoji": "💳",
    },
    PaymentGatewayType.CLOUDPAYMENTS: {
        "title": "CloudPayments",
        "methods": "карта (виджет/заказы)",
        "fields": ["public_id", "api_secret"],
        "ready": True,
        "emoji": "☁️",
    },
    PaymentGatewayType.LAVA: {
        "title": "Lava",
        "methods": "карта, СБП",
        "fields": ["shop_id", "secret_key", "webhook_secret"],
        "ready": True,
        "emoji": "🌋",
    },
    PaymentGatewayType.MULENPAY: {
        "title": "MulenPay",
        "methods": "карта, СБП",
        "fields": ["api_key", "shop_id", "secret_key"],
        "ready": True,
        "emoji": "💳",
    },
    PaymentGatewayType.KASSA_AI: {
        "title": "KassaAI",
        "methods": "карта, СБП, кошельки",
        "fields": ["shop_id", "api_key", "secret2", "payment_system_id"],
        "ready": True,
        "emoji": "🤖",
    },
    PaymentGatewayType.ROLLYPAY: {
        "title": "RollyPay",
        "methods": "карта, СБП, крипта",
        "fields": ["api_key", "signing_secret"],
        "ready": True,
        "emoji": "🎢",
    },
    PaymentGatewayType.ROBOKASSA: {
        "title": "Robokassa",
        "methods": "карта, СБП, кошельки",
        "fields": ["merchant_login", "password1", "password2", "is_test"],
        "ready": True,
        "emoji": "🤖",
    },
    PaymentGatewayType.YOOMONEY: {
        "title": "ЮMoney (кошелёк)",
        "methods": "карта, кошелёк ЮMoney",
        "fields": ["wallet", "notification_secret"],
        "ready": True,
        "emoji": "👛",
    },
    PaymentGatewayType.CISPAY: {
        "title": "cisPay",
        "methods": "карта, СБП",
        "fields": ["shop_id", "api_key", "payment_method"],
        "ready": True,
        "emoji": "💠",
    },
    PaymentGatewayType.RIOPAY: {
        "title": "RioPay",
        "methods": "карта, СБП",
        "fields": ["api_token", "webhook_secret"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.SEVERPAY: {
        "title": "SeverPay",
        "methods": "карта, СБП",
        "fields": ["token", "mid"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.PAYPEAR: {
        "title": "PayPear",
        "methods": "карта, СБП",
        "fields": ["api_key"],
        "ready": False,
        "emoji": "🏦",
    },
    PaymentGatewayType.AURAPAY: {
        "title": "AuraPay",
        "methods": "карта, СБП",
        "fields": ["api_key", "shop_id", "webhook_secret", "service"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.ANTILOPAY: {
        "title": "Antilopay",
        "methods": "СБП, карта, SberPay",
        "fields": ["secret_id", "project_id", "private_key", "public_key", "prefer_method"],
        "ready": True,
        "emoji": "🏦",
    },
    PaymentGatewayType.OVERPAY: {
        "title": "Overpay",
        "methods": "карта, крипта",
        "fields": ["api_key", "secret"],
        "ready": False,
        "emoji": "🏦",
    },
}


# Payment forms each provider can offer + brand color for the UI monogram.
PROVIDER_EXTRAS: dict[PaymentGatewayType, dict[str, Any]] = {
    PaymentGatewayType.TELEGRAM_STARS: {"forms": ["stars"], "brand": "#F7971D"},
    PaymentGatewayType.MANUAL: {"forms": ["balance"], "brand": "#6E6E6E"},
    PaymentGatewayType.YOOKASSA: {"forms": ["card", "sbp"], "brand": "#8B3FFD"},
    PaymentGatewayType.CRYPTOBOT: {"forms": ["crypto"], "brand": "#2AABEE"},
    PaymentGatewayType.CRYPTOMUS: {"forms": ["crypto"], "brand": "#3A86FF"},
    PaymentGatewayType.TRIBUTE: {"forms": ["card"], "brand": "#F5C518"},
    PaymentGatewayType.PLATEGA: {"forms": ["card", "sbp", "crypto"], "brand": "#00A86B"},
    PaymentGatewayType.ROBOKASSA: {"forms": ["card", "sbp", "wallet"], "brand": "#E4003A"},
    PaymentGatewayType.YOOMONEY: {"forms": ["card", "wallet"], "brand": "#8B3FFD"},
    PaymentGatewayType.HELEKET: {"forms": ["crypto"], "brand": "#7B61FF"},
    PaymentGatewayType.WATA: {"forms": ["card", "sbp", "tpay", "sberpay"], "brand": "#0FA3B1"},
    PaymentGatewayType.FREEKASSA: {"forms": ["card", "sbp", "wallet"], "brand": "#FF6B00"},
    PaymentGatewayType.PAYPALYCH: {"forms": ["card", "sbp"], "brand": "#4C6FFF"},
    PaymentGatewayType.CLOUDPAYMENTS: {"forms": ["card"], "brand": "#2F9BFF"},
    PaymentGatewayType.MULENPAY: {"forms": ["card", "sbp"], "brand": "#9C27B0"},
    PaymentGatewayType.LAVA: {"forms": ["card", "sbp"], "brand": "#FF4D00"},
    PaymentGatewayType.KASSA_AI: {"forms": ["card", "sbp"], "brand": "#00BFA5"},
    PaymentGatewayType.CISPAY: {"forms": ["card", "sbp"], "brand": "#2F80ED"},
    PaymentGatewayType.RIOPAY: {"forms": ["card", "sbp"], "brand": "#E91E63"},
    PaymentGatewayType.ANTILOPAY: {"forms": ["card", "sbp", "sberpay"], "brand": "#00B341"},
    PaymentGatewayType.SEVERPAY: {"forms": ["card", "sbp"], "brand": "#3F51B5"},
    PaymentGatewayType.PAYPEAR: {"forms": ["card", "sbp"], "brand": "#8BC34A"},
    PaymentGatewayType.AURAPAY: {"forms": ["card", "sbp"], "brand": "#FF9800"},
    PaymentGatewayType.OVERPAY: {"forms": ["card", "crypto"], "brand": "#607D8B"},
    PaymentGatewayType.ROLLYPAY: {"forms": ["sbp", "crypto"], "brand": "#00BCD4"},
}


def _provider_row(g: PaymentGateway | None, gtype: PaymentGatewayType) -> dict[str, Any]:
    # Secrets are never echoed: only which config keys are present.
    meta = PROVIDER_META.get(
        gtype,
        {"title": gtype.value, "methods": "", "fields": [], "ready": False, "emoji": "🏦"},
    )
    return {
        "id": g.id if g else None,
        "type": gtype.value,
        "title": meta["title"],
        "emoji": meta["emoji"],
        "methods": meta["methods"],
        "fields": meta["fields"],
        "ready": meta["ready"],
        "display_name": (g.display_name if g else None) or meta["title"],
        "is_active": g.is_active if g else False,
        "currency": g.currency.value if g else "RUB",
        "fee_bp": g.fee_bp if g else 0,
        "configured_keys": sorted(k for k in (g.settings if g else {}) if k != "enabled_forms"),
        "forms": PROVIDER_EXTRAS.get(gtype, {}).get("forms", []),
        "enabled_forms": (g.settings.get("enabled_forms") if g else None)
        or PROVIDER_EXTRAS.get(gtype, {}).get("forms", []),
        "brand": PROVIDER_EXTRAS.get(gtype, {}).get("brand", "#666"),
    }


class RefundIn(BaseModel):
    revoke_subscription: bool = True
    comment: str | None = Field(None, max_length=256)


@router.post("/payments/{txn_id}/refund")
async def refund_payment(
    txn_id: int,
    body: RefundIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Refund a completed payment: via the provider API when it supports refunds,
    otherwise record-only (the admin sends the money manually). Optionally revokes
    the subscription the payment provisioned (panel-first disable)."""
    from src.core.enums import SubscriptionStatus, TransactionStatus
    from src.core.money import Money
    from src.infrastructure.payments.crypto import decrypt_gateway_settings

    warnings: list[str] = []
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "REFUND_ENABLED")):
            raise HTTPException(403, "Возвраты отключены в настройках (REFUND_ENABLED)")
        # FOR UPDATE serializes concurrent refunds (double-click / two admins): the 2nd blocks,
        # then sees REFUNDED and 409s, so the provider refund fires at most once per payment.
        txn = await uow.session.get(Transaction, txn_id, with_for_update=True)
        if txn is None:
            raise HTTPException(404, "transaction not found")
        if txn.status is not TransactionStatus.COMPLETED:
            raise HTTPException(409, "only completed payments can be refunded")

        refunded_via = "manual"
        if txn.gateway_type is not None and txn.external_id:
            row = await uow.payment_gateways.get_active(txn.gateway_type)
            if row is not None and txn.gateway_type in container.gateway_factory.supported():
                gateway = container.gateway_factory.create(
                    txn.gateway_type,
                    decrypt_gateway_settings(container.secret_box, dict(row.settings)),
                )
                if gateway.capabilities.supports_refund:
                    try:
                        ok = await gateway.refund(
                            txn.external_id, Money(txn.amount_minor, txn.currency)
                        )
                    except Exception as exc:
                        raise HTTPException(502, f"провайдер отказал: {exc}") from exc
                    if not ok:
                        raise HTTPException(502, "провайдер отклонил возврат — см. логи")
                    refunded_via = txn.gateway_type.value
                else:
                    warnings.append("нет API-возврата — деньги нужно вернуть вручную")

        moved = await uow.transactions.transition_status(
            txn.payment_id, TransactionStatus.REFUNDED, (TransactionStatus.COMPLETED,)
        )
        if not moved:
            raise HTTPException(409, "transaction already processed")

        user = await uow.users.get(txn.user_id)
        # a refunded top-up takes the money back off the wallet (never below zero)
        if (
            txn.type is TransactionType.DEPOSIT
            and user is not None
            and not await uow.users.debit_balance_guarded(user, txn.amount_minor)
        ):
            warnings.append("на балансе меньше суммы возврата — баланс не списан")
        elif (
            txn.type is TransactionType.SUBSCRIPTION_PAYMENT
            and txn.gateway_type is None
            and txn.currency is not Currency.XTR  # not Stars — those aren't wallet money
            and user is not None
            and txn.amount_minor > 0
        ):
            # Paid from the wallet balance (no external provider) — return it to the wallet.
            # Without this the sub was revoked and the user DM'd «возвращено», but nothing was
            # actually refunded anywhere.
            await uow.users.increment_balance(user, txn.amount_minor)
            refunded_via = "balance"

        revoked = False
        disable_retry_sub_id: int | None = None
        sub_id = (txn.pricing or {}).get("subscription_id")
        if body.revoke_subscription and sub_id:
            sub = await uow.subscriptions.get(int(sub_id))
            if sub is not None and sub.status.is_usable:
                panel_ref = sub.panel_ref
                if panel_ref is not None:
                    try:
                        await container.remnawave_client.disable_user(panel_ref)
                    except Exception:
                        # Panel down: we still flip the local status to DISABLED below, but the
                        # panel user stays enabled — without a retry the refunded user keeps VPN
                        # access forever (resync only re-drives USABLE subs, not this one). Hand
                        # the disable to the panel-write retry queue to re-apply once panel is up.
                        warnings.append("панель недоступна — выключим подписку в фоне, как ответит")
                        disable_retry_sub_id = int(sub_id)
                sub.status = SubscriptionStatus.DISABLED
                revoked = True

        await audit(
            uow,
            identity,
            "payment.refund",
            f"txn:{txn_id}",
            via=refunded_via,
            amount_minor=txn.amount_minor,
            revoked=revoked,
        )
        await uow.commit()
        if disable_retry_sub_id is not None:
            # import here: the web app must not import the taskiq broker at module load
            from src.infrastructure.taskiq.tasks import panel_write_retry

            await panel_write_retry.kiq(disable_retry_sub_id)
        telegram_id = user.telegram_id if user else None
        amount = txn.amount_minor
        currency = txn.currency.value

    if telegram_id is not None:
        from src.infrastructure.services.reports import fmt_amount
        from src.web.routes.admin.notifications import notification_text

        async with container.uow() as uow:  # owner-editable «refund» template (NOTIF-1)
            note = await notification_text(uow, "refund", amount=fmt_amount(amount, currency))
        if note:
            if body.comment:
                note += "\n" + body.comment
            await container.notifier.notify_user(telegram_id, note)
    return {"ok": True, "via": refunded_via, "revoked": revoked, "warnings": warnings}


@router.get("/providers")
async def list_providers(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        rows = {g.type: g for g in await uow.payment_gateways.list()}
    # One row per known type: configured gateways merged over the catalog.
    items = [_provider_row(rows.get(gtype), gtype) for gtype in PROVIDER_META]
    # Ready + active first, then ready, then the rest alphabetically.
    items.sort(key=lambda p: (not p["is_active"], not p["ready"], p["title"].lower()))
    return {"items": items}


class ProviderIn(BaseModel):
    type: PaymentGatewayType
    display_name: str | None = Field(None, max_length=64)
    is_active: bool | None = None
    fee_bp: int | None = Field(None, ge=0, le=10_000)
    enabled_forms: list[str] | None = None
    # Provider credentials (api key, merchant id, webhook secret...). Merged into
    # existing settings; secret-looking keys are Fernet-encrypted at rest.
    settings: dict[str, str] | None = None


@router.post("/providers")
async def upsert_provider(
    body: ProviderIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        gw = await uow.payment_gateways.find_one(type=body.type)
        if gw is None:
            gw = PaymentGateway(type=body.type, display_name=body.display_name)
            await uow.payment_gateways.add(gw)
        if body.display_name is not None:
            gw.display_name = body.display_name
        if body.is_active is not None:
            gw.is_active = body.is_active
        if body.fee_bp is not None:
            gw.fee_bp = body.fee_bp
        if body.enabled_forms is not None:
            allowed = set(PROVIDER_EXTRAS.get(body.type, {}).get("forms", []))
            gw.settings = dict(gw.settings) | {
                "enabled_forms": [f for f in body.enabled_forms if f in allowed]
            }
        if body.settings:
            from src.infrastructure.payments.crypto import is_secret_key

            merged = dict(gw.settings)
            for k, v in body.settings.items():
                if v == "":
                    merged.pop(k, None)
                    continue
                if is_secret_key(k) and container.secret_box is not None:
                    merged[k] = container.secret_box.encrypt(v)
                else:
                    merged[k] = v
            gw.settings = merged
        await audit(
            uow,
            identity,
            "provider.upsert",
            f"provider:{body.type.value}",
            is_active=gw.is_active,
            fee_bp=gw.fee_bp,
        )
        await uow.commit()
        return _provider_row(gw, gw.type)


@router.post("/providers/{gateway_type}/test")
async def test_provider(
    gateway_type: str, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    """Probe provider API/balance. Manual/Stars need no external API — always ok."""
    try:
        gtype = PaymentGatewayType(gateway_type)
    except ValueError as exc:
        raise HTTPException(404, "unknown provider") from exc
    async with container.uow() as uow:
        gw = await uow.payment_gateways.find_one(type=gtype)
    if gw is None:
        raise HTTPException(404, "provider not configured")
    if gtype in (PaymentGatewayType.MANUAL, PaymentGatewayType.TELEGRAM_STARS):
        return {"ok": True, "balance": None, "detail": "no external API required"}

    from src.infrastructure.payments.crypto import decrypt_gateway_settings

    settings = decrypt_gateway_settings(container.secret_box, dict(gw.settings))
    import httpx

    if gtype is PaymentGatewayType.YOOKASSA:
        shop_id = str(settings.get("shop_id") or "")
        secret = str(settings.get("secret_key") or "")
        if not shop_id or not secret:
            return {"ok": False, "balance": None, "detail": "заполни shop_id и secret_key"}
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.get("https://api.yookassa.ru/v3/me", auth=(shop_id, secret))
        if res.status_code == 200:
            acc = res.json()
            return {
                "ok": True,
                "balance": None,
                "detail": f"магазин {acc.get('account_id')} · статус {acc.get('status')}",
            }
        return {"ok": False, "balance": None, "detail": f"YooKassa ответила {res.status_code}"}

    if gtype is PaymentGatewayType.CRYPTOBOT:
        token = str(settings.get("api_token") or "")
        if not token:
            return {"ok": False, "balance": None, "detail": "заполни api_token"}
        async with httpx.AsyncClient(timeout=15) as http:
            me = await http.get(
                "https://pay.crypt.bot/api/getMe", headers={"Crypto-Pay-API-Token": token}
            )
            bal = await http.get(
                "https://pay.crypt.bot/api/getBalance", headers={"Crypto-Pay-API-Token": token}
            )
        if me.status_code == 200 and me.json().get("ok"):
            balances = bal.json().get("result", []) if bal.status_code == 200 else []
            usdt = next(
                (b.get("available") for b in balances if b.get("currency_code") == "USDT"), None
            )
            name = me.json()["result"].get("name")
            return {"ok": True, "balance": usdt, "detail": f"приложение {name} · USDT {usdt or 0}"}
        return {"ok": False, "balance": None, "detail": f"CryptoBot ответил {me.status_code}"}

    if gtype is PaymentGatewayType.PLATEGA:
        mid = str(settings.get("merchant_id") or "")
        sec = str(settings.get("secret") or "")
        if not mid or not sec:
            return {"ok": False, "balance": None, "detail": "заполни merchant_id и secret"}
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.get(
                "https://app.platega.io/transaction/00000000-0000-0000-0000-000000000000",
                headers={"X-MerchantId": mid, "X-Secret": sec},
            )
        if res.status_code in (401, 403):
            return {"ok": False, "balance": None, "detail": "Platega не принял ключи (401/403)"}
        return {"ok": True, "balance": None, "detail": "ключи приняты Platega"}

    if gtype in (PaymentGatewayType.CRYPTOMUS, PaymentGatewayType.HELEKET):
        import base64 as _b64
        import hashlib as _hl
        import json as _json

        merchant = str(settings.get("merchant_uuid") or "")
        key = str(settings.get("api_key") or "")
        if not merchant or not key:
            return {"ok": False, "balance": None, "detail": "заполни merchant_uuid и api_key"}
        base = (
            "https://api.cryptomus.com/v1"
            if gtype is PaymentGatewayType.CRYPTOMUS
            else "https://api.heleket.com/v1"
        )
        body = _json.dumps({}, separators=(",", ":")).encode()
        sign = _hl.md5(_b64.b64encode(body) + key.encode()).hexdigest()
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.post(
                f"{base}/balance",
                content=body,
                headers={"merchant": merchant, "sign": sign, "Content-Type": "application/json"},
            )
        if res.status_code == 200 and (res.json() or {}).get("result") is not None:
            return {"ok": True, "balance": None, "detail": "ключи приняты, баланс доступен"}
        return {"ok": False, "balance": None, "detail": f"ответ {res.status_code} — проверь ключи"}

    if gtype is PaymentGatewayType.WATA:
        token = str(settings.get("api_token") or "")
        if not token:
            return {"ok": False, "balance": None, "detail": "заполни api_token"}
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.get(
                "https://api.wata.pro/api/h2h/public-key",
                headers={"Authorization": f"Bearer {token}"},
            )
        if res.status_code == 200:
            return {"ok": True, "balance": None, "detail": "токен принят WATA"}
        return {"ok": False, "balance": None, "detail": f"WATA ответила {res.status_code}"}

    if gtype in (PaymentGatewayType.ROBOKASSA, PaymentGatewayType.YOOMONEY):
        required = (
            ("merchant_login", "password1", "password2")
            if gtype is PaymentGatewayType.ROBOKASSA
            else ("wallet", "notification_secret")
        )
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {"ok": False, "balance": None, "detail": f"заполни {', '.join(missing)}"}
        return {
            "ok": True,
            "balance": None,
            "detail": "ключи заполнены; проверочного API нет — сверится первым платежом",
        }

    # Real balance probes land with each gateway implementation (single-file drop-ins).
    return {"ok": False, "balance": None, "detail": "gateway implementation not installed"}
