"""Cabinet review fix: Stars invoice creation must be guarded by ``TelegramAPIError``, not the
narrower ``TelegramBadRequest`` — a network error, a flood-control retry-after, or a revoked/
unauthorized bot token during ``bot.create_invoice_link`` all raise a *different* aiogram
exception (``TelegramNetworkError`` / ``TelegramRetryAfter`` / ``TelegramUnauthorizedError``,
respectively), every one of which is a ``TelegramAPIError`` but NOT a ``TelegramBadRequest``.

Before the fix those slipped past the narrower ``except`` in both POST /api/cabinet/topup and
the Stars branch of POST /api/cabinet/purchase, hit the app's generic 500 handler, and left the
just-committed PENDING transaction stranded forever (the reconciler's ``list_stuck_pending()``
can never pick it up — a Stars row has neither ``gateway_type`` nor ``external_id``).

Self-contained ASGI harness, same shape as tests/unit/test_topup_cabinet_fixes.py (not edited
here) so this file stays independent of parallel changes elsewhere.
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
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.methods import CreateInvoiceLink
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker

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
from src.core.enums import TransactionStatus, TransactionType
from src.core.security import hash_password
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.events import InProcessEventBus
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
    user = {"id": tg_id, "first_name": "Тест", "username": "topup_neterr", "language_code": "ru"}
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAE",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": f"tma {urllib.parse.urlencode(pairs)}"}


def _network_error(message: str) -> TelegramNetworkError:
    """A realistic TelegramNetworkError — what aiogram raises when the POST to Telegram's API
    itself fails (timeout, DNS, connection reset…), wrapping the raw aiohttp/asyncio error.
    Not a TelegramBadRequest — Telegram never even answered."""
    method = CreateInvoiceLink(title="t", description="d", payload="p", currency="XTR", prices=[])
    return TelegramNetworkError(method=method, message=message)


def _retry_after(seconds: int) -> TelegramRetryAfter:
    """Flood control — also not a TelegramBadRequest."""
    method = CreateInvoiceLink(title="t", description="d", payload="p", currency="XTR", prices=[])
    return TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=seconds)


def _unauthorized(message: str) -> TelegramUnauthorizedError:
    """Bot token revoked/invalid — also not a TelegramBadRequest."""
    method = CreateInvoiceLink(title="t", description="d", payload="p", currency="XTR", prices=[])
    return TelegramUnauthorizedError(method=method, message=message)


# --- /topup: any invoice-creation error, not just TelegramBadRequest, cancels the txn ------


async def test_topup_network_error_cancels_pending_and_returns_clean_error(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    tma = _tma_headers(920000001)
    await http.get("/api/cabinet/me", headers=tma)

    async def _boom(self: Bot, **kwargs: Any) -> str:
        raise _network_error("Connection reset by peer")

    monkeypatch.setattr(Bot, "create_invoice_link", _boom)

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "stars"}
    )
    # A meaningful, documented error — never the generic 500 the unhandled-exception handler
    # would have produced for an exception type the old `except TelegramBadRequest` missed.
    assert res.status_code == 502, res.text
    assert res.json()["detail"] != "внутренняя ошибка сервера"

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=920000001)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1  # not stranded as a phantom extra row, not silently dropped either
        assert txs[0].type is TransactionType.DEPOSIT
        # The whole point of the fix: CANCELED, not an eternal PENDING the reconciler can
        # never pick up (list_stuck_pending requires gateway_type/external_id — Stars has
        # neither).
        assert txs[0].status is TransactionStatus.CANCELED
        assert user.balance_minor == 0


async def test_topup_retry_after_cancels_pending_transaction(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flood control (TelegramRetryAfter) — a live risk under any burst of top-up taps."""
    http, container = client
    tma = _tma_headers(920000002)
    await http.get("/api/cabinet/me", headers=tma)

    async def _boom(self: Bot, **kwargs: Any) -> str:
        raise _retry_after(30)

    monkeypatch.setattr(Bot, "create_invoice_link", _boom)

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "stars"}
    )
    assert res.status_code == 502, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=920000002)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1
        assert txs[0].status is TransactionStatus.CANCELED


async def test_topup_unauthorized_bot_cancels_pending_transaction(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoked/invalid bot token (TelegramUnauthorizedError) must not strand the txn either."""
    http, container = client
    tma = _tma_headers(920000003)
    await http.get("/api/cabinet/me", headers=tma)

    async def _boom(self: Bot, **kwargs: Any) -> str:
        raise _unauthorized("Unauthorized")

    monkeypatch.setattr(Bot, "create_invoice_link", _boom)

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "stars"}
    )
    assert res.status_code == 502, res.text

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=920000003)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1
        assert txs[0].status is TransactionStatus.CANCELED


# --- /purchase: the same fix, symmetrically, on the Stars branch of checkout ---------------


async def test_purchase_network_error_cancels_pending_and_returns_clean_error(
    client: tuple[httpx.AsyncClient, ApiTestContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, container = client
    tma = _tma_headers(920000004)
    await http.get("/api/cabinet/me", headers=tma)
    async with container.uow() as uow:
        plan, _duration = await make_plan(
            uow, price_minor=30000, days=30, code="neterr-purchase-plan"
        )
        await uow.commit()
        plan_id = plan.id

    async def _boom(self: Bot, **kwargs: Any) -> str:
        raise _network_error("Connection reset by peer")

    monkeypatch.setattr(Bot, "create_invoice_link", _boom)

    res = await http.post(
        "/api/cabinet/purchase",
        headers=tma,
        json={"plan_id": plan_id, "days": 30, "method": "stars"},
    )
    assert res.status_code == 502, res.text
    assert res.json()["detail"] != "внутренняя ошибка сервера"

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=920000004)
        assert user is not None
        txs = await uow.transactions.list(user_id=user.id)
        assert len(txs) == 1
        assert txs[0].type is TransactionType.SUBSCRIPTION_PAYMENT
        assert txs[0].status is TransactionStatus.CANCELED
        # A canceled purchase must not have silently provisioned a subscription.
        assert user.current_subscription_id is None


# --- wallet disabled: /topup must refuse cleanly, not accept money nothing will credit -----


async def test_topup_rejected_with_400_when_wallet_disabled(
    client: tuple[httpx.AsyncClient, ApiTestContainer],
) -> None:
    http, container = client
    tma = _tma_headers(920000005)
    await http.get("/api/cabinet/me", headers=tma)
    async with container.uow() as uow:
        await container.bot_config.set_values(uow, {"BALANCE_ENABLED": False})
        await uow.commit()

    res = await http.post(
        "/api/cabinet/topup", headers=tma, json={"amount_minor": 50000, "method": "stars"}
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "balance top-ups are disabled"

    async with container.uow() as uow:
        user = await uow.users.find_one(telegram_id=920000005)
        assert user is not None
        # No PENDING (or any) transaction left behind by the refused request.
        assert await uow.transactions.list(user_id=user.id) == []
