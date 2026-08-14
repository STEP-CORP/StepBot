"""Gateway top-up, end to end: create -> provider webhook -> ACTUAL balance credit.

Everything else in this feature (test_topup_gateways.py, test_cabinet_topup.py) stops at the
PENDING transaction — by design, since crediting only happens once the provider confirms. This
file drives the confirmation itself through the real production pipeline (the same webhook route
+ ``process_payment`` taskiq task the reconciler and every gateway module use) and asserts on the
resulting ``user.balance_minor`` — not on a 200 status code — that the money actually lands, lands
once, and drags along the two side effects that used to be Stars-only: referral commission and
the "smart cart" auto-purchase.

Self-contained ASGI harness, same shape as test_cabinet_topup.py's ``ApiTestContainer`` (kept
independent on purpose so this file doesn't couple to it).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import uuid as uuid_mod
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.application.common.payments import (
    GatewayCapabilities,
    PaymentContext,
    PaymentResult,
    PaymentResultKind,
    WebhookRequest,
    WebhookResult,
)
from src.application.services.bot_config import BotConfigService
from src.application.services.panel_sync import PanelSyncService
from src.application.services.payment import PaymentService
from src.application.services.pricing import PricingService
from src.application.services.promo import PromoService
from src.application.services.purchase import PurchaseService
from src.application.services.referral import ReferralService
from src.application.services.remnawave import RemnawaveService
from src.application.services.subscription import SubscriptionService
from src.application.services.traffic import TrafficService
from src.core.config import get_settings
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.core.money import Money
from src.infrastructure.database.models.payment_gateway import (
    PaymentGateway as PaymentGatewayModel,
)
from src.infrastructure.database.models.referral import Referral
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.events import InProcessEventBus
from src.infrastructure.payments.base import BasePaymentGateway
from src.infrastructure.payments.crypto import SecretBox
from src.infrastructure.payments.factory import GatewayFactory
from src.infrastructure.services.notification import LogNotifier
from src.infrastructure.services.telemetry import TelemetryReporter
from tests.factories import make_plan
from tests.fakes.panel import FakeRemnawaveClient

BOT_TOKEN = "12345:E2ETESTTOKEN"


class _FakeRedis:
    """``set``/``getdel``/``delete`` — enough for the paylock AND the smart-cart (GETDEL)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None: ...


class ApiTestContainer:
    """Route-facing surface of AppContainer, wired with fakes + test sqlite — same services
    the real worker uses (PaymentService, ReferralService, PurchaseService), so a webhook
    driven through this container exercises the real fulfilment/commission/auto-purchase code,
    not a stand-in."""

    def __init__(self, session_factory: async_sessionmaker, settings: Any) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self.redis = _FakeRedis()
        self.remnawave_client = FakeRemnawaveClient()
        self.gateway_factory: Any = GatewayFactory()
        self.secret_box = SecretBox(settings.app.crypt_key)
        self.event_bus = InProcessEventBus()
        self.remnawave = RemnawaveService(self.remnawave_client)
        self.pricing = PricingService()
        self.subscriptions = SubscriptionService(self.remnawave)
        self.bot_config = BotConfigService(self.secret_box)
        self.purchase = PurchaseService(
            self.pricing, self.subscriptions, self.event_bus, config=self.bot_config
        )
        # config=self.bot_config matters: without it ReferralService falls back to its own
        # DEFAULT_COMMISSION_PERCENT (25) instead of honoring REFERRAL_PERCENT — mirrors the
        # real wiring in src/infrastructure/di/container.py so this test reflects production.
        self.referrals = ReferralService(
            self.event_bus, subscriptions=self.subscriptions, config=self.bot_config
        )
        self.payments = PaymentService(self.purchase, self.event_bus, self.referrals)
        self.promo = PromoService(self.subscriptions)
        self.notifier = LogNotifier()
        self.panel_sync = PanelSyncService(self.remnawave_client)
        self.traffic = TrafficService(self.remnawave_client)
        self.telemetry = TelemetryReporter(
            enabled=False, url="", app_version="test", install_id="test"
        )

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    async def aclose(self) -> None: ...


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, ApiTestContainer]]:
    monkeypatch.setenv("APP__JWT_SECRET", "test-jwt-secret-for-e2e")
    monkeypatch.setenv("APP__CRYPT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOT__TOKEN", BOT_TOKEN)
    monkeypatch.setenv("ADMIN__DEMO_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()

    from src.web.app import create_app

    app = create_app()
    container = ApiTestContainer(session_factory, settings)
    app.state.container = container

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, container
    get_settings.cache_clear()


def _tma_headers(tg_id: int) -> dict[str, str]:
    user = {"id": tg_id, "first_name": "Клиент", "username": f"u{tg_id}", "language_code": "ru"}
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"tma {urllib.parse.urlencode(pairs)}"}


# --- a stub gateway: create_payment opens a hosted link, handle_webhook confirms it ---------


class _StubGateway(BasePaymentGateway):
    """Records every ``create_payment`` call by payment_id so ``handle_webhook`` can reply
    with the matching id — mirrors a real provider's create -> callback round trip without
    a network call."""

    gateway_type = PaymentGatewayType.YOOKASSA

    def __init__(self, settings: dict[str, Any]) -> None:
        super().__init__(settings)
        self.opened: dict[str, int] = {}  # str(payment_id) -> amount_minor

    @property
    def capabilities(self) -> GatewayCapabilities:
        return GatewayCapabilities(currencies=frozenset({Currency.RUB}))

    async def create_payment(self, ctx: PaymentContext) -> PaymentResult:
        self.opened[str(ctx.payment_id)] = ctx.amount.amount_minor
        return PaymentResult(
            kind=PaymentResultKind.REDIRECT,
            external_id=f"ext-{ctx.payment_id}",
            redirect_url=f"https://pay.example/checkout/{ctx.payment_id}",
        )

    async def handle_webhook(self, request: WebhookRequest) -> WebhookResult:
        """The stub "provider" callback body is just ``{"payment_id": "...", "status": "..."}``
        — real verification is covered elsewhere (test_more_gateways.py); this test is about
        what happens to the balance AFTER verification succeeds."""
        payload = json.loads(request.body)
        pid = payload["payment_id"]
        amount_minor = self.opened.get(pid)
        return WebhookResult(
            status=TransactionStatus(payload["status"]),
            payment_id=uuid_mod.UUID(pid),
            external_id=f"ext-{pid}",
            amount=Money(amount_minor, Currency.RUB) if amount_minor is not None else None,
        )


class _BrokenGateway(BasePaymentGateway):
    """create_payment always blows up — simulates the provider being down when the client
    hits POST /api/cabinet/topup (network error, 500 from the acquirer, etc)."""

    gateway_type = PaymentGatewayType.YOOKASSA

    @property
    def capabilities(self) -> GatewayCapabilities:
        return GatewayCapabilities(currencies=frozenset({Currency.RUB}))

    async def create_payment(self, ctx: PaymentContext) -> PaymentResult:
        raise ConnectionError("acquirer unreachable")

    async def handle_webhook(self, request: WebhookRequest) -> WebhookResult:
        raise NotImplementedError


class _StubFactory:
    def __init__(self, gateway: BasePaymentGateway) -> None:
        self._gateway = gateway

    def create(self, gt: PaymentGatewayType, settings: dict[str, Any]) -> BasePaymentGateway:
        return self._gateway

    def supported(self) -> set[PaymentGatewayType]:
        return {PaymentGatewayType.YOOKASSA}


async def _seed_active_yookassa(container: ApiTestContainer) -> None:
    async with container.uow() as uow:
        await uow.payment_gateways.add(
            PaymentGatewayModel(
                type=PaymentGatewayType.YOOKASSA, is_active=True, settings={}, display_name="ЮKassa"
            )
        )
        await uow.commit()


def _wire_synchronous_worker(monkeypatch: pytest.MonkeyPatch, container: ApiTestContainer) -> None:
    """Make the webhook route's ``process_payment.kiq(...)`` run the REAL taskiq task body
    in-process against ``container``, instead of enqueueing to a Redis broker that doesn't
    exist in this test. This is the same object patched in
    test_admin_api.py::_post_webhook_with_dead_broker — patching ``.kiq`` on it affects every
    module that imported ``process_payment`` (it's one object), so the route under test sees it.
    """
    import src.web.routes.payments as payments_route
    from src.infrastructure.taskiq import tasks as taskiq_tasks

    monkeypatch.setattr(taskiq_tasks, "get_container", lambda: container)

    async def _run_inline(
        payment_id: str,
        status: str,
        *,
        saved_method_enc: str | None = None,
        saved_method_title: str | None = None,
        amount_minor: int | None = None,
    ) -> None:
        await taskiq_tasks.process_payment(
            payment_id,
            status,
            saved_method_enc=saved_method_enc,
            saved_method_title=saved_method_title,
            amount_minor=amount_minor,
        )

    monkeypatch.setattr(payments_route.process_payment, "kiq", _run_inline)


async def _create_gateway_topup(http: httpx.AsyncClient, tg_id: int, amount_minor: int) -> str:
    """Client path: POST /api/cabinet/topup with a gateway method -> pending DEPOSIT + redirect
    link, exactly what the mini-app's ``submitTopup`` does. Returns the txn's payment_id."""
    res = await http.post(
        "/api/cabinet/topup",
        headers=_tma_headers(tg_id),
        json={"amount_minor": amount_minor, "method": "yookassa"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["redirect_url"].startswith("https://pay.example/checkout/")
    return body["redirect_url"].rsplit("/", 1)[-1]


async def _post_webhook(
    http: httpx.AsyncClient, payment_id: str, status: str = "completed"
) -> httpx.Response:
    return await http.post(
        "/api/v1/payments/yookassa",
        content=json.dumps({"payment_id": payment_id, "status": status}).encode(),
    )


# --- (1) the money actually lands, exactly once ----------------------------------------------


async def test_gateway_topup_webhook_credits_balance_and_is_idempotent(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    tg_id = 910000111
    await http.get("/api/cabinet/me", headers=_tma_headers(tg_id))  # upsert the user
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubFactory(_StubGateway({}))
    _wire_synchronous_worker(monkeypatch, container)

    async with container.uow() as uow:
        before = await uow.users.find_one(telegram_id=tg_id)
        assert before is not None and before.balance_minor == 0

    payment_id = await _create_gateway_topup(http, tg_id, amount_minor=50000)  # 500 ₽

    # Balance untouched until the provider confirms — same invariant test_cabinet_topup.py checks.
    async with container.uow() as uow:
        mid_flight = await uow.users.find_one(telegram_id=tg_id)
        assert mid_flight is not None and mid_flight.balance_minor == 0

    res = await _post_webhook(http, payment_id)
    assert res.status_code == 200, res.text
    assert res.json() == {"accepted": True}

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=tg_id)
        assert user is not None
        assert user.balance_minor == 50000  # 0 -> 500 ₽, the actual number, not just "ok"
        assert user.has_made_first_topup is True
        txns = await uow.transactions.list(user_id=user.id)
        deposits = [t for t in txns if t.type is TransactionType.DEPOSIT]
        assert len(deposits) == 1
        assert deposits[0].status is TransactionStatus.COMPLETED
        assert deposits[0].amount_minor == 50000

    # Provider redelivers the SAME webhook (their retry policy, or an operator resend) —
    # must NOT double-credit.
    res2 = await _post_webhook(http, payment_id)
    assert res2.status_code == 200, res2.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=tg_id)
        assert user is not None
        assert user.balance_minor == 50000  # still 500 ₽, not 1000 ₽
        deposits = [
            t
            for t in await uow.transactions.list(user_id=user.id)
            if t.type is TransactionType.DEPOSIT
        ]
        assert len(deposits) == 1  # no second transaction was created either


# --- (2) referral commission fires for a gateway top-up, same as it does for Stars -----------


async def test_gateway_topup_pays_referral_commission(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    referrer_tg, payer_tg = 910000222, 910000333
    await http.get("/api/cabinet/me", headers=_tma_headers(referrer_tg))
    await http.get("/api/cabinet/me", headers=_tma_headers(payer_tg))
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubFactory(_StubGateway({}))
    _wire_synchronous_worker(monkeypatch, container)

    async with container.uow() as uow:
        referrer = await uow.users.find_one(telegram_id=referrer_tg)
        payer = await uow.users.find_one(telegram_id=payer_tg)
        assert referrer is not None and payer is not None
        uow.session.add(Referral(referrer_id=referrer.id, referred_id=payer.id))
        await uow.commit()
        referrer_id = referrer.id

    payment_id = await _create_gateway_topup(http, payer_tg, amount_minor=100000)  # 1000 ₽
    res = await _post_webhook(http, payment_id)
    assert res.status_code == 200, res.text

    async with container.uow() as uow:
        referrer_after = await uow.users.get(referrer_id)
        assert referrer_after is not None
        # REFERRAL_PERCENT defaults to 10% (config_registry.py) — 1000 ₽ top-up -> 100 ₽ payout.
        assert referrer_after.balance_minor == 10000
        earnings = await uow.referral_earnings.total_minor(referrer_id)
        assert earnings == 10000

    # Replaying the same webhook must not pay the referrer twice either.
    await _post_webhook(http, payment_id)
    async with container.uow() as uow:
        referrer_after = await uow.users.get(referrer_id)
        assert referrer_after is not None
        assert referrer_after.balance_minor == 10000


# --- (3) AUTO_PURCHASE_AFTER_TOPUP completes the stashed cart from a gateway top-up too -------


async def test_gateway_topup_completes_stashed_auto_purchase(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.application.dto.pricing import PurchaseRequest
    from src.infrastructure.services.cart import save_cart

    http, container = client
    tg_id = 910000444
    await http.get("/api/cabinet/me", headers=_tma_headers(tg_id))
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubFactory(_StubGateway({}))
    _wire_synchronous_worker(monkeypatch, container)

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=tg_id)
        assert user is not None
        plan, _ = await make_plan(uow, price_minor=30000, days=30)  # 300 ₽ plan
        await uow.commit()
        user_id, plan_id = user.id, plan.id
        # AUTO_PURCHASE_AFTER_TOPUP defaults to True (config_registry.py) — left un-set on purpose,
        # this exercises the shipped default, not a test-only override.
        auto_on = bool(await container.bot_config.value(uow, "AUTO_PURCHASE_AFTER_TOPUP"))
        assert auto_on is True

    req = PurchaseRequest(user_id=user_id, plan_id=plan_id, duration_days=30, currency=Currency.RUB)
    await save_cart(container.redis, req, ttl_seconds=3600)
    assert container.redis.store.get(f"cart:{user_id}") is not None  # the stash actually exists

    async with container.uow() as uow:
        assert await uow.subscriptions.active_for_user(user_id) == []  # nothing yet
    assert len(container.remnawave_client.users) == 0  # no panel user provisioned yet

    payment_id = await _create_gateway_topup(http, tg_id, amount_minor=50000)  # 500 ₽ top-up
    res = await _post_webhook(http, payment_id)
    assert res.status_code == 200, res.text

    async with container.uow() as uow:
        active = await uow.subscriptions.active_for_user(user_id)
        assert len(active) == 1  # the stashed purchase actually went through, not just "no error"
        user = await uow.users.get(user_id)
        assert user is not None
        # 500 ₽ credited, 300 ₽ debited by the auto-purchase -> 200 ₽ left, not the full 500 ₽.
        assert user.balance_minor == 20000
    assert len(container.remnawave_client.users) == 1  # a real panel user got provisioned
    assert container.redis.store.get(f"cart:{user_id}") is None  # the stash was consumed


# --- (4) boundary: the gateway create step fails -> no orphan PENDING transaction -------------


async def test_gateway_topup_api_failure_leaves_no_orphan_transaction(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """test_topup_gateways.py already covers this for the bot handler
    (``test_topup_pay_gateway_failure_leaves_no_orphan_transaction``) — the mini-app's own
    POST /api/cabinet/topup route has the identical rollback-on-exception shape but no test of
    its own yet. A provider outage here must not leave a PENDING deposit nobody will ever
    settle (it would sit forever, or worse, get "fixed" by a manual credit)."""
    http, container = client
    tg_id = 910000555
    await http.get("/api/cabinet/me", headers=_tma_headers(tg_id))
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubFactory(_BrokenGateway({}))

    res = await http.post(
        "/api/cabinet/topup",
        headers=_tma_headers(tg_id),
        json={"amount_minor": 50000, "method": "yookassa"},
    )
    assert res.status_code == 502, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=tg_id)
        assert user is not None
        assert await uow.transactions.list(user_id=user.id) == []  # nothing left behind
        assert user.balance_minor == 0
