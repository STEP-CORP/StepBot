"""POST /api/cabinet/topup: mini-app wallet top-up (Stars / online gateway).

Self-contained ASGI harness (mirrors tests/integration/test_admin_api.py's ApiTestContainer)
so this file stays independent of parallel changes to the bot's own top-up flow.
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


async def _login(http: httpx.AsyncClient) -> dict[str, str]:
    res = await http.post(
        "/api/admin/auth/login",
        json={"username": "root_admin", "password": ADMIN_PASSWORD},
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def _tma_headers(tg_id: int = 900000111) -> dict[str, str]:
    user = {"id": tg_id, "first_name": "Тест", "username": "topup_e2e", "language_code": "ru"}
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"tma {urllib.parse.urlencode(pairs)}"}


# --- a stub gateway: no network, always opens a hosted redirect --------------------------


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


class _StubGwFactory:
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


# --- tests ---------------------------------------------------------------------------------


async def test_topup_via_gateway_creates_deposit_and_redirect(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers()
    await http.get("/api/cabinet/me", headers=tma)  # upsert the user
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubGwFactory(_StubRedirectGateway({}))

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "yookassa"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["redirect_url"].startswith("https://pay.example/checkout/")

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=900000111)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1
        txn = txs[0]
        assert txn.type is TransactionType.DEPOSIT  # not SUBSCRIPTION_PAYMENT
        assert txn.status is TransactionStatus.PENDING
        assert txn.amount_minor == 50000
        assert txn.currency is Currency.RUB
        assert txn.gateway_type is PaymentGatewayType.YOOKASSA
        assert txn.external_id is not None
        # the balance is untouched until the provider webhook completes the transaction
        assert user.balance_minor == 0


async def test_topup_rejects_balance_as_a_method(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(tg_id=900000222)
    await http.get("/api/cabinet/me", headers=tma)

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "balance"}
    )
    assert res.status_code == 400

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=900000222)
        assert user is not None
        assert await uow.transactions.list(user_id=user.id) == []


async def test_topup_below_minimum_rejected(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    tma = _tma_headers(tg_id=900000333)
    await http.get("/api/cabinet/me", headers=tma)
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubGwFactory(_StubRedirectGateway({}))

    # server-side floor (MIN_DEPOSIT_AMOUNT), not merely a UI preset the client could forge
    res = await http.patch(
        "/api/admin/settings", headers=auth, json={"changes": {"MIN_DEPOSIT_AMOUNT": 10000}}
    )
    assert res.status_code == 200

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 5000, "method": "yookassa"}
    )
    assert res.status_code == 400

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=900000333)
        assert user is not None
        assert await uow.transactions.list(user_id=user.id) == []


async def test_topup_rejected_when_balance_disabled(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    auth = await _login(http)
    tma = _tma_headers(tg_id=900000444)
    await http.get("/api/cabinet/me", headers=tma)
    await _seed_active_yookassa(container)
    container.gateway_factory = _StubGwFactory(_StubRedirectGateway({}))

    res = await http.patch(
        "/api/admin/settings", headers=auth, json={"changes": {"BALANCE_ENABLED": False}}
    )
    assert res.status_code == 200

    # /me should stop advertising the top-up affordance too
    me = (await http.get("/api/cabinet/me", headers=tma)).json()
    assert me["app"]["balance_enabled"] is False

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "yookassa"}
    )
    assert res.status_code == 400

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=900000444)
        assert user is not None
        assert await uow.transactions.list(user_id=user.id) == []


async def test_me_exposes_min_deposit_minor(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, _container = client
    auth = await _login(http)
    tma = _tma_headers(tg_id=900000555)
    await http.patch(
        "/api/admin/settings", headers=auth, json={"changes": {"MIN_DEPOSIT_AMOUNT": 12345}}
    )
    me = (await http.get("/api/cabinet/me", headers=tma)).json()
    assert me["app"]["min_deposit_minor"] == 12345
