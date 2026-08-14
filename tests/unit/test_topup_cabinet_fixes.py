"""Cabinet review fixes: POST /api/cabinet/topup + the Stars branch of POST /api/cabinet/purchase.

Self-contained ASGI harness (mirrors tests/integration/test_admin_api.py's ApiTestContainer, same
as tests/unit/test_cabinet_topup.py) so this file stays independent of parallel changes elsewhere.

Covers what the review found missing:
  1. Stars invoice creation failing must cancel the just-committed PENDING transaction (both
     /topup and /purchase), not strand it forever.
  4. amount_minor above MAX_DEPOSIT_AMOUNT_MINOR is a 400, not pydantic's bare 422.
  5. ``method`` is cross-checked against the methods /me actually offers (closes the
     manual/telegram_stars gap in _pay_with_gateway / _topup_with_gateway).
  + a disabled/unknown gateway and a "gateway didn't return REDIRECT" response.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CreateInvoiceLink
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
from src.core.constants import MAX_DEPOSIT_AMOUNT_MINOR
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.core.security import hash_password
from src.infrastructure.database.models.payment_gateway import (
    PaymentGateway as PaymentGatewayModel,
)
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.events import InProcessEventBus
from src.infrastructure.payments.base import BasePaymentGateway
from src.infrastructure.payments.crypto import SecretBox
from src.infrastructure.payments.factory import GatewayFactory
from src.infrastructure.services.notification import LogNotifier
from src.infrastructure.services.telemetry import TelemetryReporter
from tests.factories import make_plan
from tests.fakes.panel import FakeRemnawaveClient

BOT_TOKEN = "12345:TESTTOKEN"
ADMIN_PASSWORD = "AdminPass123!"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None: ...


class ApiTestContainer:
    """Route-facing surface of AppContainer, wired with fakes + test sqlite."""

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
        self.purchase = PurchaseService(self.pricing, self.subscriptions, self.event_bus)
        self.referrals = ReferralService(self.event_bus)
        self.payments = PaymentService(self.purchase, self.event_bus, self.referrals)
        self.promo = PromoService()
        self.bot_config = BotConfigService(self.secret_box)
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
    monkeypatch.setenv("APP__JWT_SECRET", "test-jwt-secret-for-api")
    monkeypatch.setenv("APP__CRYPT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BOT__TOKEN", BOT_TOKEN)
    monkeypatch.setenv("ADMIN__DEMO_ENABLED", "false")
    get_settings.cache_clear()
    settings = get_settings()

    from src.web.app import create_app

    app = create_app()
    container = ApiTestContainer(session_factory, settings)
    app.state.container = container

    from src.application.services.ids import generate_referral_code
    from src.core.enums import AuthType, Role
    from src.infrastructure.database.models.user import User

    async with container.uow() as uow:
        await uow.users.add(
            User(
                username="root_admin",
                auth_type=AuthType.EMAIL,
                role=Role.OWNER,
                referral_code=generate_referral_code(),
                password_hash=hash_password(ADMIN_PASSWORD),
            )
        )
        await uow.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, container
    get_settings.cache_clear()


def _tma_headers(tg_id: int) -> dict[str, str]:
    user = {"id": tg_id, "first_name": "Тест", "username": "topup_fix", "language_code": "ru"}
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"tma {urllib.parse.urlencode(pairs)}"}


def _bad_request(message: str) -> TelegramBadRequest:
    """A realistic TelegramBadRequest, the way ``bot.create_invoice_link`` would raise it."""
    method = CreateInvoiceLink(title="t", description="d", payload="p", currency="XTR", prices=[])
    return TelegramBadRequest(method=method, message=message)


# --- stub gateways -----------------------------------------------------------------------


class _StubRedirectGateway(BasePaymentGateway):
    gateway_type = PaymentGatewayType.YOOKASSA

    @property
    def capabilities(self) -> GatewayCapabilities:
        return GatewayCapabilities(currencies=frozenset({Currency.RUB}))

    async def create_payment(self, ctx: PaymentContext) -> PaymentResult:
        return PaymentResult(
            kind=PaymentResultKind.REDIRECT,
            external_id=f"ext-{ctx.payment_id}",
            redirect_url="https://pay.example/checkout/" + str(ctx.payment_id),
        )

    async def handle_webhook(self, request: WebhookRequest) -> WebhookResult:
        raise NotImplementedError


class _StubPendingGateway(BasePaymentGateway):
    """A gateway that never opens a hosted redirect — e.g. a manual/offline confirmation."""

    gateway_type = PaymentGatewayType.YOOKASSA

    @property
    def capabilities(self) -> GatewayCapabilities:
        return GatewayCapabilities(currencies=frozenset({Currency.RUB}))

    async def create_payment(self, ctx: PaymentContext) -> PaymentResult:
        return PaymentResult(kind=PaymentResultKind.PENDING, external_id=f"ext-{ctx.payment_id}")

    async def handle_webhook(self, request: WebhookRequest) -> WebhookResult:
        raise NotImplementedError


class _StubGwFactory:
    def __init__(self, gateway: BasePaymentGateway, *, supported: set[PaymentGatewayType]) -> None:
        self._gateway = gateway
        self._supported = supported

    def create(self, gt: PaymentGatewayType, settings: dict[str, Any]) -> BasePaymentGateway:
        return self._gateway

    def supported(self) -> set[PaymentGatewayType]:
        return self._supported


async def _seed_active_gateway(
    container: ApiTestContainer, gtype: PaymentGatewayType, *, display_name: str = "ЮKassa"
) -> None:
    async with container.uow() as uow:
        await uow.payment_gateways.add(
            PaymentGatewayModel(type=gtype, is_active=True, settings={}, display_name=display_name)
        )
        await uow.commit()


# --- (1) Stars invoice failure must cancel the PENDING txn, not strand it ----------------


async def test_topup_stars_invoice_failure_cancels_pending_transaction(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    tma = _tma_headers(910000001)
    await http.get("/api/cabinet/me", headers=tma)

    async def _boom(self: Bot, **kwargs: Any) -> str:
        raise _bad_request("CURRENCY_TOTAL_AMOUNT_INVALID")

    monkeypatch.setattr(Bot, "create_invoice_link", _boom)

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "stars"}
    )
    assert res.status_code == 502, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=910000001)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1  # not stranded as a phantom extra row, not silently dropped either
        assert txs[0].type is TransactionType.DEPOSIT
        # The whole point of the fix: CANCELED, not an eternal PENDING the reconciler can
        # never pick up (list_stuck_pending requires gateway_type/external_id — Stars has
        # neither).
        assert txs[0].status is TransactionStatus.CANCELED
        assert user.balance_minor == 0


async def test_topup_stars_success_returns_invoice_link(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    tma = _tma_headers(910000002)
    await http.get("/api/cabinet/me", headers=tma)

    async def _fake_link(self: Bot, **kwargs: Any) -> str:
        return "https://t.me/$fake-invoice"

    monkeypatch.setattr(Bot, "create_invoice_link", _fake_link)

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "stars"}
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "invoice_link": "https://t.me/$fake-invoice"}

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=910000002)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1
        assert txs[0].status is TransactionStatus.PENDING
        assert txs[0].amount_minor == 50000


async def test_purchase_stars_invoice_failure_cancels_pending_transaction(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same orphan-PENDING-transaction hazard, symmetrically fixed on /purchase (#1)."""
    http, container = client
    tma = _tma_headers(910000003)
    await http.get("/api/cabinet/me", headers=tma)
    async with container.uow() as uow:
        plan, _duration = await make_plan(
            uow, price_minor=30000, days=30, code="stars-purchase-plan"
        )
        await uow.commit()
        plan_id = plan.id

    async def _boom(self: Bot, **kwargs: Any) -> str:
        raise _bad_request("CURRENCY_TOTAL_AMOUNT_INVALID")

    monkeypatch.setattr(Bot, "create_invoice_link", _boom)

    res = await http.post(
        "/api/cabinet/purchase",
        headers=tma,
        json={"plan_id": plan_id, "days": 30, "method": "stars"},
    )
    assert res.status_code == 502, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=910000003)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1
        assert txs[0].type is TransactionType.SUBSCRIPTION_PAYMENT
        assert txs[0].status is TransactionStatus.CANCELED
        # A canceled purchase must not have silently provisioned a subscription.
        assert user.current_subscription_id is None


# --- (5) method must match what _gateway_methods() actually offers -----------------------


async def test_topup_rejects_method_excluded_from_offered_list(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    """A ``manual``-type gateway row that is active and "supported" must still be rejected —
    _gateway_methods() deliberately never advertises manual/telegram_stars (#5). Before the
    fix this reached gateway.create_payment() and would have returned a real redirect.
    """
    http, container = client
    tma = _tma_headers(910000004)
    await http.get("/api/cabinet/me", headers=tma)
    await _seed_active_gateway(container, PaymentGatewayType.MANUAL, display_name="Manual")
    container.gateway_factory = _StubGwFactory(
        _StubRedirectGateway({}),
        supported={PaymentGatewayType.MANUAL, PaymentGatewayType.YOOKASSA},
    )

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "manual"}
    )
    assert res.status_code == 400, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=910000004)
        assert user is not None
        assert await uow.transactions.list(user_id=user.id) == []


async def test_purchase_rejects_method_excluded_from_offered_list(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(910000005)
    await http.get("/api/cabinet/me", headers=tma)
    async with container.uow() as uow:
        plan, _duration = await make_plan(uow, price_minor=30000, days=30, code="manual-plan")
        await uow.commit()
        plan_id = plan.id
    await _seed_active_gateway(container, PaymentGatewayType.TELEGRAM_STARS, display_name="Stars")
    container.gateway_factory = _StubGwFactory(
        _StubRedirectGateway({}),
        supported={PaymentGatewayType.TELEGRAM_STARS, PaymentGatewayType.YOOKASSA},
    )

    res = await http.post(
        "/api/cabinet/purchase",
        headers=tma,
        json={"plan_id": plan_id, "days": 30, "method": "telegram_stars"},
    )
    assert res.status_code == 400, res.text


# --- gateway not offered / didn't redirect ------------------------------------------------


async def test_topup_unknown_gateway_rejected_with_400(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(910000006)
    await http.get("/api/cabinet/me", headers=tma)
    # No gateway row seeded at all — "yookassa" is a known enum member but not configured.
    container.gateway_factory = _StubGwFactory(
        _StubRedirectGateway({}), supported={PaymentGatewayType.YOOKASSA}
    )

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "yookassa"}
    )
    assert res.status_code == 400, res.text


async def test_topup_gateway_non_redirect_result_rejected(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(910000007)
    await http.get("/api/cabinet/me", headers=tma)
    await _seed_active_gateway(container, PaymentGatewayType.YOOKASSA)
    container.gateway_factory = _StubGwFactory(
        _StubPendingGateway({}), supported={PaymentGatewayType.YOOKASSA}
    )

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "yookassa"}
    )
    assert res.status_code == 502, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=910000007)
        assert user is not None
        # The whole write happens in one uncommitted uow — a mid-flight 502 rolls it back,
        # no orphan DEPOSIT row left behind.
        assert await uow.transactions.list(user_id=user.id) == []


# --- (4) the ceiling is a 400, like every other error on this endpoint -------------------


async def test_topup_above_max_rejected_with_400_not_422(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(910000008)
    await http.get("/api/cabinet/me", headers=tma)
    await _seed_active_gateway(container, PaymentGatewayType.YOOKASSA)
    container.gateway_factory = _StubGwFactory(
        _StubRedirectGateway({}), supported={PaymentGatewayType.YOOKASSA}
    )

    res = await http.post(
        "/api/cabinet/topup",
        headers=tma,
        json={"amount_minor": MAX_DEPOSIT_AMOUNT_MINOR + 1, "method": "yookassa"},
    )
    assert res.status_code == 400, res.text  # not FastAPI/pydantic's bare 422

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=910000008)
        assert user is not None
        assert await uow.transactions.list(user_id=user.id) == []


async def test_topup_at_max_boundary_accepted(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(910000009)
    await http.get("/api/cabinet/me", headers=tma)
    await _seed_active_gateway(container, PaymentGatewayType.YOOKASSA)
    container.gateway_factory = _StubGwFactory(
        _StubRedirectGateway({}), supported={PaymentGatewayType.YOOKASSA}
    )

    res = await http.post(
        "/api/cabinet/topup",
        headers=tma,
        json={"amount_minor": MAX_DEPOSIT_AMOUNT_MINOR, "method": "yookassa"},
    )
    assert res.status_code == 200, res.text


# --- /me exposes the ceiling so the client can validate a custom amount (#3) --------------


async def test_me_exposes_max_deposit_minor(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _container = client
    tma = _tma_headers(910000010)
    me = (await http.get("/api/cabinet/me", headers=tma)).json()
    assert me["app"]["max_deposit_minor"] == MAX_DEPOSIT_AMOUNT_MINOR
