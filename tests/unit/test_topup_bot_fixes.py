"""Regression tests for the bot-side top-up review findings.

Covers, in order: (a) the Stars branch of the top-up flow, which the ``_topup_with_stars``
split-out left completely uncovered; (b) the missing upper bound on both the amount-selection
and the payment steps (a forged/huge amount used to reach a real provider invoice or the
transaction's bigint column unchecked); (c) the new free-form "custom amount" entry point;
(d) the payment step now cross-checking the chosen method against the offered list instead of
trusting callback data; and (e) the amount/method/invoice screens no longer sharing one
``screen_key`` (an operator's saved override for one used to leak onto the other two through
``banners._apply_overrides``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from src.application.services.bot_config import BotConfigService
from src.bot.handlers import actions, purchase
from src.core.constants import MAX_DEPOSIT_AMOUNT_MINOR
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.infrastructure.database.models.payment_gateway import PaymentGateway
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.payments.factory import GatewayFactory
from tests.factories import make_user


class _FakeRedis:
    """Minimal ``paylock:*`` nx-set/delete, same shape as test_topup_e2e.py's fake."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, k: str, v: str, ex: int = 0, nx: bool = False) -> bool:
        if nx and k in self.store:
            return False
        self.store[k] = v
        return True

    async def delete(self, k: str) -> None:
        self.store.pop(k, None)


class _FakeInvoiceMessage:
    """``cb.message`` stand-in that records ``answer_invoice()`` calls, or raises on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.invoices: list[dict[str, Any]] = []

    async def answer_invoice(self, **kwargs: Any) -> None:
        if self.fail:
            raise TelegramBadRequest(
                method=None,  # type: ignore[arg-type]
                message="Bad Request: CURRENCY_TOTAL_AMOUNT_INVALID",
            )
        self.invoices.append(kwargs)


class _FakeCb:
    def __init__(self, data: str, user_id: int = 42, *, message: Any = None) -> None:
        self.data = data
        self.from_user: Any = SimpleNamespace(id=user_id)
        self.message: Any = message if message is not None else SimpleNamespace()
        self.answers: list[tuple[str | None, dict[str, Any]]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


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


def _fsm_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=1, user_id=1))


def _patch_render_screen(monkeypatch: pytest.MonkeyPatch, module: Any = purchase) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_render_screen(
        target: Any, container: Any, screen_key: str, caption: str, markup: Any = None
    ) -> None:
        captured["target"] = target
        captured["screen_key"] = screen_key
        captured["caption"] = caption
        captured["markup"] = markup

    monkeypatch.setattr(module, "render_screen", _fake_render_screen)
    return captured


async def _deposits(uow: UnitOfWork, user_id: int) -> list[Any]:
    async with uow:
        return [
            t
            for t in await uow.transactions.list(user_id=user_id)
            if t.type is TransactionType.DEPOSIT
        ]


# --- (a) Stars top-up: invoice amount + PENDING-txn cancellation on failure -----


async def test_topup_pay_stars_sends_invoice_with_star_amount_from_rate(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    msg = _FakeInvoiceMessage()
    cb = _FakeCb("topupm:50000:stars", message=msg)
    await purchase.topup_pay(cb, _container(uow), user)

    assert len(msg.invoices) == 1
    invoice = msg.invoices[0]
    assert invoice["currency"] == "XTR"
    # 50000 kopecks / 130 kopecks-per-star (STARS_RATE_RUB default), rounded up -> 385 ★.
    assert invoice["prices"][0].amount == 385

    deposits = await _deposits(uow, user_id)
    assert len(deposits) == 1
    txn = deposits[0]
    assert txn.status is TransactionStatus.PENDING
    assert txn.amount_minor == 50000
    assert invoice["payload"] == str(txn.payment_id)
    assert cb.answers == [(None, {})]  # a plain cb.answer(), no alert


async def test_topup_pay_stars_uses_the_configured_rate(uow: UnitOfWork) -> None:
    cfg = BotConfigService()
    async with uow:
        user = await make_user(uow)
        await cfg.set_values(uow, {"STARS_RATE_RUB": 100})  # 1 star == 1 ₽
        await uow.commit()

    msg = _FakeInvoiceMessage()
    cb = _FakeCb("topupm:30000:stars", message=msg)
    await purchase.topup_pay(cb, _container(uow, bot_config=cfg), user)

    assert msg.invoices[0]["prices"][0].amount == 300


async def test_topup_pay_stars_cancels_pending_txn_on_invoice_failure(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    msg = _FakeInvoiceMessage(fail=True)
    cb = _FakeCb("topupm:50000:stars", message=msg)
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    deposits = await _deposits(uow, user_id)
    assert len(deposits) == 1
    # No orphan PENDING left haunting the history — the failed-invoice txn is closed.
    assert deposits[0].status is TransactionStatus.CANCELED


# --- (b) upper bound on both the amount and the payment step -------------------


async def test_topup_amount_rejects_amount_above_ceiling(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    cb = _FakeCb(f"topup:{MAX_DEPOSIT_AMOUNT_MINOR + 100}")
    await purchase.topup_amount(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "Максимальное" in (cb.answers[0][0] or "")


async def test_topup_amount_rejects_forged_huge_amount_without_crashing(uow: UnitOfWork) -> None:
    """A crafted callback with a 20-digit amount used to overflow the txn's bigint column and
    blow up unhandled instead of being rejected — this must now fail cleanly, no orphan row."""
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    cb = _FakeCb("topup:99999999999999999999")
    await purchase.topup_amount(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert await _deposits(uow, user_id) == []


async def test_topup_pay_rejects_amount_above_ceiling(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    cb = _FakeCb(f"topupm:{MAX_DEPOSIT_AMOUNT_MINOR + 100}:stars")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "Максимальное" in (cb.answers[0][0] or "")
    assert await _deposits(uow, user_id) == []  # no orphan PENDING txn, no real invoice


async def test_topup_pay_rejects_forged_huge_amount_without_crashing(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    cb = _FakeCb("topupm:99999999999999999999:stars")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert await _deposits(uow, user_id) == []


# --- (c) free-form "custom amount" entry point ----------------------------------


async def test_topup_custom_prompt_arms_the_fsm_state(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    # topup_custom_prompt renders a raw prompt via show_screen (not render_screen) — _FakeCb
    # isn't a real aiogram CallbackQuery, so show_screen's isinstance-based dispatch needs faking
    # here too (unlike the render_screen-based handlers, which stop before that isinstance check).
    async def _fake_show_screen(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(purchase, "show_screen", _fake_show_screen)
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    cb = _FakeCb("topup:custom")
    await purchase.topup_custom_prompt(cb, _container(uow), user, state)

    assert await state.get_state() == purchase.TopupForm.waiting_amount.state


async def test_topup_custom_amount_valid_opens_the_method_screen(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    captured = _patch_render_screen(monkeypatch)
    async with uow:
        user = await make_user(uow)
        await _seed_yookassa(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    message = _FakeMessage("3000")
    await purchase.topup_custom_amount(message, _container(uow), user, state)

    assert captured["screen_key"] == "topup_method"
    assert "3 000 ₽" in captured["caption"]
    assert await state.get_state() is None  # consumed, not left armed


async def test_topup_custom_amount_rejects_non_numeric_text(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    message = _FakeMessage("сто рублей")
    await purchase.topup_custom_amount(message, _container(uow), user, state)

    assert message.answers and "целое число" in message.answers[0]
    # Still armed — the user gets to retry without reopening the prompt.
    assert await state.get_state() == purchase.TopupForm.waiting_amount.state


async def test_topup_custom_amount_rejects_below_minimum(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    message = _FakeMessage("1")  # 100 kopecks, below the 5000 default MIN_DEPOSIT_AMOUNT
    await purchase.topup_custom_amount(message, _container(uow), user, state)

    assert message.answers and "Минимальное" in message.answers[0]
    assert await state.get_state() == purchase.TopupForm.waiting_amount.state


async def test_topup_custom_amount_rejects_above_ceiling(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    message = _FakeMessage(str(MAX_DEPOSIT_AMOUNT_MINOR // 100 + 1))
    await purchase.topup_custom_amount(message, _container(uow), user, state)

    assert message.answers and "Максимальное" in message.answers[0]
    assert await state.get_state() == purchase.TopupForm.waiting_amount.state


async def test_topup_custom_amount_rejects_negative_number(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    message = _FakeMessage("-500")
    await purchase.topup_custom_amount(message, _container(uow), user, state)

    assert message.answers and "целое число" in message.answers[0]


# --- (d) the payment step cross-checks the method against the offered list -----


async def test_topup_pay_rejects_method_not_offered(uow: UnitOfWork) -> None:
    """ "manual"/"telegram_stars" pass ``PaymentGatewayType(...)`` fine, but ``_payment_methods``
    never offers them for a top-up — the payment step must reject them itself, rather than
    relying on the gateway happening not to return REDIRECT."""
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    cb = _FakeCb("topupm:50000:manual")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "недоступен" in (cb.answers[0][0] or "")
    assert await _deposits(uow, user_id) == []


async def test_topup_pay_rejects_forged_balance_method(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    cb = _FakeCb("topupm:50000:bal")
    await purchase.topup_pay(cb, _container(uow), user)

    assert cb.answers and cb.answers[0][1].get("show_alert") is True


async def test_topup_pay_accepts_a_method_the_screen_actually_offered(uow: UnitOfWork) -> None:
    """Sanity check the allow-list isn't overzealous: "stars" (always offered) still works."""
    async with uow:
        user = await make_user(uow)
        await uow.commit()
        user_id = user.id

    cb = _FakeCb("topupm:50000:stars", message=_FakeInvoiceMessage())
    await purchase.topup_pay(cb, _container(uow), user)

    assert await _deposits(uow, user_id)  # a PENDING deposit was actually created


# --- (e) three distinct screen keys instead of one shared "topup" --------------


def test_topup_screens_are_registered_as_three_distinct_keys() -> None:
    from src.bot.screen_buttons import SCREENS_BY_KEY
    from src.bot.screen_text import default_text_for, placeholders_for

    for key in ("topup", "topup_method", "topup_invoice"):
        assert key in SCREENS_BY_KEY
        assert default_text_for(key).strip()
        assert placeholders_for(key)

    keys = {SCREENS_BY_KEY[k].key for k in ("topup", "topup_method", "topup_invoice")}
    assert len(keys) == 3
    # The invoice screen only registers the static back-to-menu button; the «Оплатить» url and
    # «Проверить оплату» buttons carry a per-invoice url/payment_id and must stay dynamic.
    invoice_keys = {b.key for b in SCREENS_BY_KEY["topup_invoice"].buttons}
    assert invoice_keys == {"nav:root"}


@respx.mock
async def test_topup_amount_method_invoice_screens_use_three_different_keys(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    """End-to-end through the real handlers: before the fix all three rendered under "topup" -
    an operator's saved SCREEN_TEXTS/SCREEN_BUTTONS override for one leaked onto the other two
    (``banners._apply_overrides`` replaces the whole caption/keyboard per screen_key)."""
    respx.post("https://api.yookassa.ru/v3/payments").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "yk-1",
                "status": "pending",
                "confirmation": {"confirmation_url": "https://yoomoney.ru/pay/yk-1"},
            },
        )
    )
    captured = _patch_render_screen(monkeypatch)
    async with uow:
        user = await make_user(uow)
        await _seed_yookassa(uow)
        await uow.commit()

    await purchase.topup_menu(_FakeCb("topup:menu"), _container(uow), user)
    amount_key = captured["screen_key"]

    await purchase.topup_amount(_FakeCb("topup:50000"), _container(uow), user)
    method_key = captured["screen_key"]

    await purchase.topup_pay(_FakeCb("topupm:50000:yookassa"), _container(uow), user)
    invoice_key = captured["screen_key"]

    assert (amount_key, method_key, invoice_key) == ("topup", "topup_method", "topup_invoice")


# --- (f) the top-up button no longer over-promises "Stars only" ----------------


def test_balance_screen_topup_button_is_not_stars_only() -> None:
    from src.bot.screen_buttons import SCREENS_BY_KEY

    labels = [b.label for b in SCREENS_BY_KEY["balance"].buttons]
    assert "⭐ Пополнить" not in labels
    assert "💳 Пополнить" in labels


async def test_act_balance_topup_button_is_not_stars_only(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    captured = _patch_render_screen(monkeypatch, module=actions)
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    await actions.act_balance(_FakeCb("act:balance:0"), _container(uow), user)

    labels = [b.text for row in captured["markup"].inline_keyboard for b in row]
    assert "⭐ Пополнить" not in labels
    assert "💳 Пополнить" in labels
