"""cisPay gateway: create (kopecks + headers), HMAC-SHA256 webhook, status poll."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
import respx

from src.application.common.payments import PaymentContext, WebhookRequest
from src.core.enums import Currency, TransactionStatus
from src.core.exceptions import PaymentError, WebhookVerificationError
from src.core.money import Money
from src.infrastructure.payments.gateways.cispay import CispayGateway

SETTINGS = {"shop_id": "c56d9539-7814-4112-9c44-59e55728a3bd", "api_key": "cis_sec_test"}


def _ctx(amount_minor: int = 150000) -> PaymentContext:
    return PaymentContext(
        payment_id=uuid.uuid4(),
        amount=Money(amount_minor, Currency.RUB),
        description="VPN",
        user_id=7,
        telegram_id=42,
        return_url="https://shop.example/ok",
    )


@respx.mock
async def test_create_sends_kopecks_and_auth_headers() -> None:
    gw = CispayGateway(SETTINGS)
    ctx = _ctx()
    route = respx.post("https://api.cispay.app/payments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "7fa12a88-294b-4b11-bbfe-e69c3dbeaab9",
                "order_id": str(ctx.payment_id),
                "status": "PENDING",
                "amount": 150000,
                "payment_url": "https://cispay.app/pay/7fa12a88",
            },
        )
    )
    result = await gw.create_payment(ctx)
    assert result.external_id == "7fa12a88-294b-4b11-bbfe-e69c3dbeaab9"
    assert result.redirect_url == "https://cispay.app/pay/7fa12a88"

    req = route.calls.last.request
    assert req.headers["X-Shop-ID"] == SETTINGS["shop_id"]
    assert req.headers["X-Api-Key"] == SETTINGS["api_key"]
    sent = json.loads(req.content)
    assert sent["amount"] == 150000  # копейки как есть, без пересчёта
    assert sent["order_id"] == str(ctx.payment_id)
    assert sent["payment_method"] == "CARD"
    assert sent["customer_id"] == "42"  # обязателен для СБП
    assert sent["redirect_success_url"] == "https://shop.example/ok"


@respx.mock
async def test_sbp_method_from_settings() -> None:
    gw = CispayGateway({**SETTINGS, "payment_method": "sbp"})
    route = respx.post("https://api.cispay.app/payments").mock(
        return_value=httpx.Response(201, json={"id": "p1", "payment_url": "https://cispay.app/p1"})
    )
    await gw.create_payment(_ctx())
    assert json.loads(route.calls.last.request.content)["payment_method"] == "SBP"


@respx.mock
async def test_create_error_raises() -> None:
    gw = CispayGateway(SETTINGS)
    respx.post("https://api.cispay.app/payments").mock(
        return_value=httpx.Response(402, json={"detail": "no funds"})
    )
    with pytest.raises(PaymentError):
        await gw.create_payment(_ctx())


async def test_webhook_paid_and_bad_signature() -> None:
    gw = CispayGateway(SETTINGS)
    pid = uuid.uuid4()
    body = json.dumps(
        {
            "id": "7fa12a88",
            "order_id": str(pid),
            "status": "PAID",
            "amount": 150000,
            "currency": "RUB",
            "charged_amount": 150000,
            "merchant_revenue": 144000,
        }
    ).encode()
    sig = hmac.new(SETTINGS["api_key"].encode(), body, hashlib.sha256).hexdigest()

    ok = await gw.handle_webhook(WebhookRequest(body=body, headers={"X-Signature": sig}))
    assert ok.status is TransactionStatus.COMPLETED
    assert ok.payment_id == pid
    assert ok.external_id == "7fa12a88"
    # сверка суммы идёт по amount, а не по выручке мерчанта (иначе оплата «недоплачена»)
    assert ok.amount == Money(150000, Currency.RUB)

    with pytest.raises(WebhookVerificationError):
        await gw.handle_webhook(WebhookRequest(body=body, headers={"X-Signature": "0" * 64}))
    with pytest.raises(WebhookVerificationError):
        await gw.handle_webhook(WebhookRequest(body=body, headers={}))


async def test_webhook_statuses_map() -> None:
    gw = CispayGateway(SETTINGS)
    for raw, expected in (
        ("FAILED", TransactionStatus.CANCELED),
        ("EXPIRED", TransactionStatus.CANCELED),
        ("PENDING", TransactionStatus.PENDING),
        ("REFUNDED", TransactionStatus.REFUNDED),
    ):
        body = json.dumps({"id": "x", "order_id": str(uuid.uuid4()), "status": raw}).encode()
        sig = hmac.new(SETTINGS["api_key"].encode(), body, hashlib.sha256).hexdigest()
        res = await gw.handle_webhook(WebhookRequest(body=body, headers={"X-Signature": sig}))
        assert res.status is expected, raw


@respx.mock
async def test_fetch_status_recovers_lost_webhook() -> None:
    gw = CispayGateway(SETTINGS)
    pid = uuid.uuid4()
    respx.get("https://api.cispay.app/payments/status").mock(
        return_value=httpx.Response(
            200, json={"id": "p1", "order_id": str(pid), "status": "PAID", "amount": 150000}
        )
    )
    res = await gw.fetch_status("p1")
    assert res is not None and res.status is TransactionStatus.COMPLETED and res.payment_id == pid


@respx.mock
async def test_fetch_status_pending_returns_none() -> None:
    gw = CispayGateway(SETTINGS)
    respx.get("https://api.cispay.app/payments/status").mock(
        return_value=httpx.Response(200, json={"id": "p1", "status": "PENDING"})
    )
    assert await gw.fetch_status("p1") is None


async def test_missing_credentials_refuse() -> None:
    with pytest.raises(PaymentError):
        await CispayGateway({}).create_payment(_ctx())
