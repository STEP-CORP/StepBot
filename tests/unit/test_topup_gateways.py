"""Balance top-up: choose amount -> choose method (Stars or any connected gateway).

Before this, ``topup:{minor}`` went straight to a Stars invoice — the only way in. Now it
opens a method screen built with the same ``_payment_methods`` helper the purchase flow uses
(minus the "pay from balance" option, which can't fund itself), and a chosen gateway books
a plain DEPOSIT transaction instead of the SUBSCRIPTION_PAYMENT ``purchase.start()`` always
creates.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from src.application.services.bot_config import BotConfigService
from src.bot.handlers import purchase
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.infrastructure.database.models.payment_gateway import PaymentGateway
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.payments.factory import GatewayFactory
from tests.factories import make_user


class _FakeRedis:
    """Minimal ``paylock:*`` nx-set/delete, same shape as the fakes in test_audit_regressions.py."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, k: str, v: str, ex: int = 0, nx: bool = False) -> bool:
        if nx and k in self.store:
            return False
        self.store[k] = v
        return True

    async def delete(self, k: str) -> None:
        self.store.pop(k, None)


class _FakeCb:
    def __init__(self, data: str, user_id: int = 42) -> None:
        self.data = data
        self.from_user: Any = SimpleNamespace(id=user_id)
        self.message: Any = SimpleNamespace()  # only touched through the patched render_screen
        self.answers: list[tuple[str | None, dict[str, Any]]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


def _container(uow: UnitOfWork, *, bot_config: BotConfigService | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        uow=lambda: uow,
        bot_config=bot_config or BotConfigService(),
        gateway_factory=GatewayFactory(),
        secret_box=None,
        redis=_FakeRedis(),
    )


async def _seed_yookassa(uow: UnitOfWork) -> None:
    uow.session.add(
        PaymentGateway(
            type=PaymentGatewayType.YOOKASSA,
            is_active=True,
            currency=Currency.RUB,
            display_name="ЮKassa",
            settings={"shop_id": "1", "secret_key": "sk"},
        )
    )


def _button_codes(markup: Any, prefix: str) -> list[str]:
    codes = []
    for row in markup.inline_keyboard:
        for btn in row:
            cb = btn.callback_data
            if cb and cb.startswith(prefix):
                codes.append(cb.split(":", 2)[2])
    return codes


# --- (a) the method list never offers "pay from balance" -----------------------


async def test_topup_methods_never_offer_balance(uow: UnitOfWork) -> None:
    container = _container(uow)
    async with uow:
        user = await make_user(uow, balance_minor=100000)
        await _seed_yookassa(uow)
        await uow.commit()
        topup_methods = await purchase._payment_methods(
            uow, container, user, 50000, include_balance=False
        )
        purchase_methods = await purchase._payment_methods(uow, container, user, 50000)

    topup_codes = [code for _label, code in topup_methods]
    assert "bal" not in topup_codes
    assert "stars" in topup_codes
    assert "yookassa" in topup_codes
    # Sanity: the shared helper still offers balance for the regular purchase screens.
    assert "bal" in [code for _label, code in purchase_methods]


async def test_topup_amount_screen_excludes_balance_button(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_render_screen(
        cb: Any, container: Any, screen_key: str, caption: str, markup: Any = None
    ) -> None:
        captured["markup"] = markup

    monkeypatch.setattr(purchase, "render_screen", _fake_render_screen)
    async with uow:
        user = await make_user(uow)
        await _seed_yookassa(uow)
        await uow.commit()

    cb = _FakeCb("topup:50000")
    await purchase.topup_amount(cb, _container(uow), user)

    codes = _button_codes(captured["markup"], "topupm:")
    assert "bal" not in codes
    assert "stars" in codes
    assert "yookassa" in codes


# --- (b) a gateway top-up books a DEPOSIT and calls create_payment -------------


@respx.mock
async def test_topup_pay_gateway_creates_deposit_and_calls_create_payment(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    respx.post("https://api.yookassa.ru/v3/payments").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "yk-topup-1",
                "status": "pending",
                "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/yk-topup-1"},
            },
        )
    )
    async with uow:
        user = await make_user(uow)
        await _seed_yookassa(uow)
        await uow.commit()
        user_id = user.id

    captured: dict[str, Any] = {}

    async def _fake_render_screen(
        cb: Any, container: Any, screen_key: str, caption: str, markup: Any = None
    ) -> None:
        captured["markup"] = markup

    monkeypatch.setattr(purchase, "render_screen", _fake_render_screen)

    cb = _FakeCb("topupm:50000:yookassa")
    await purchase.topup_pay(cb, _container(uow), user)

    async with uow:
        deposits = [
            t
            for t in await uow.transactions.list(user_id=user_id)
            if t.type is TransactionType.DEPOSIT
        ]
    assert len(deposits) == 1
    txn = deposits[0]
    assert txn.status is TransactionStatus.PENDING  # webhook/paycheck settles it, not the bot
    assert txn.amount_minor == 50000
    assert txn.currency is Currency.RUB
    assert txn.gateway_type is PaymentGatewayType.YOOKASSA
    assert txn.external_id == "yk-topup-1"

    urls = [btn.url for row in captured["markup"].inline_keyboard for btn in row if btn.url]
    assert urls == ["https://yoomoney.ru/pay/yk-topup-1"]


@respx.mock
async def test_topup_pay_gateway_failure_leaves_no_orphan_transaction(uow: UnitOfWork) -> None:
    respx.post("https://api.yookassa.ru/v3/payments").mock(return_value=httpx.Response(500))
    async with uow:
        user = await make_user(uow)
        await _seed_yookassa(uow)
        await uow.commit()
        user_id = user.id

    # render_screen is intentionally left un-patched: a gateway failure must bail out before
    # ever reaching it (the real one would blow up on the bare-bones fake ``cb.message``).
    cb = _FakeCb("topupm:50000:yookassa")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    async with uow:
        assert await uow.transactions.list(user_id=user_id) == []  # rolled back, not committed


# --- (c) the forgeable amount is re-validated on the payment step too ----------


async def test_topup_pay_rejects_amount_below_min_dep(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    # 1 kopeck — far below MIN_DEPOSIT_AMOUNT (5000 by default), forged directly into the
    # callback data as if the amount-selection step had never happened.
    cb = _FakeCb("topupm:1:stars")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "Минимальное" in (cb.answers[0][0] or "")
    async with uow:
        assert await uow.transactions.list(user_id=user_id) == []  # no orphan PENDING txn


async def test_topup_pay_rejects_forged_balance_method(uow: UnitOfWork) -> None:
    """_payment_methods(include_balance=False) never offers "bal", but callback data is
    forgeable — the payment step must reject it defensively too."""
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    cb = _FakeCb("topupm:50000:bal")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True


# --- (d) BALANCE_ENABLED is re-checked on every step, not just the first -------


async def test_topup_amount_rejects_when_balance_disabled(uow: UnitOfWork) -> None:
    cfg = BotConfigService()
    async with uow:
        user = await make_user(uow)
        await cfg.set_values(uow, {"BALANCE_ENABLED": False})
        await uow.commit()

    cb = _FakeCb("topup:50000")
    await purchase.topup_amount(cb, _container(uow, bot_config=cfg), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "отключен" in (cb.answers[0][0] or "")


async def test_topup_pay_rejects_when_balance_disabled(uow: UnitOfWork) -> None:
    cfg = BotConfigService()
    async with uow:
        user = await make_user(uow)
        await cfg.set_values(uow, {"BALANCE_ENABLED": False})
        await uow.commit()
        user_id = user.id

    cb = _FakeCb("topupm:50000:stars")
    await purchase.topup_pay(cb, _container(uow, bot_config=cfg), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "отключен" in (cb.answers[0][0] or "")
    async with uow:
        assert await uow.transactions.list(user_id=user_id) == []
