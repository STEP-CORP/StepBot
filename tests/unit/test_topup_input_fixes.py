"""Regression tests for three custom-amount top-up review findings (self-contained, mirrors
tests/unit/test_topup_gateways.py's fakes so this file stays independent of parallel changes):

  1. ``TopupForm.waiting_amount`` used to stay armed forever on any non-amount text (a stray
     question, small talk, or a command) and swallowed every later message — commands are now
     excluded from the handler's filter, and two non-amount replies in a row self-clean the form
     (one retry is still allowed, matching the handler's pre-existing "Пришли ещё раз" UX and
     keeping test_topup_bot_fixes.py's single-miss assertion intact).
  2. ``isdigit()`` accepted unicode look-alikes (e.g. "²") that ``int()`` can't parse, crashing the
     handler — replaced with ``isdecimal()`` in the custom-amount input and both topup callbacks.
  3. An operator's SCREEN_TEXTS override for "topup_method" replaces the whole caption and could
     hide "К зачислению: ...". The amount now also lives in a dedicated button that
     screen_buttons.SAFE_SCREENS never lists for this screen, so it always survives untouched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from src.application.services.bot_config import BotConfigService
from src.bot import banners
from src.bot.handlers import purchase
from src.core.enums import Currency, PaymentGatewayType
from src.infrastructure.database.models.payment_gateway import PaymentGateway
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.payments.factory import GatewayFactory
from tests.factories import make_user


class _FakeRedis:
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
        self.message: Any = SimpleNamespace()
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


def _fsm_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=1, user_id=1))


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


# --- (1) the form self-cleans and stops swallowing stray messages --------------


async def test_topup_custom_amount_second_consecutive_miss_clears_state(uow: UnitOfWork) -> None:
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)

    first = _FakeMessage("когда вернёшь доступ?")
    await purchase.topup_custom_amount(first, _container(uow), user, state)
    assert first.answers and "целое число" in first.answers[0]
    # One miss still gives the user a chance to correct a typo, unchanged from before.
    assert await state.get_state() == purchase.TopupForm.waiting_amount.state

    second = _FakeMessage("ладно, напишу в поддержку")
    await purchase.topup_custom_amount(second, _container(uow), user, state)
    assert second.answers and "Своя сумма" in second.answers[0]
    # Second stray reply in a row -> the form drops itself instead of waiting forever.
    assert await state.get_state() is None


async def test_topup_custom_amount_valid_amount_after_one_miss_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    """A single typo must not cost the user their retry (only two misses in a row self-clean)."""
    captured: dict[str, Any] = {}

    async def _fake_render_screen(
        target: Any, container: Any, screen_key: str, caption: str, markup: Any = None
    ) -> None:
        captured["screen_key"] = screen_key

    monkeypatch.setattr(purchase, "render_screen", _fake_render_screen)
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    typo = _FakeMessage("сто рублей плиз")
    await purchase.topup_custom_amount(typo, _container(uow), user, state)
    assert await state.get_state() == purchase.TopupForm.waiting_amount.state

    await purchase.topup_custom_amount(_FakeMessage("3000"), _container(uow), user, state)
    assert captured["screen_key"] == "topup_method"
    assert await state.get_state() is None


def test_topup_custom_amount_filter_excludes_commands() -> None:
    """The router-level filter (not the handler body) must reject commands outright, so a
    "/support"/"/bug" sent while the form is armed reaches tickets.py's Command("support") /
    the catch-all instead of being read as an amount attempt."""
    handler = next(
        h for h in purchase.router.message.handlers if h.callback is purchase.topup_custom_amount
    )
    magics = [f.magic for f in handler.filters if f.magic is not None]
    assert magics, "topup_custom_amount must carry a magic-filter text guard"
    text_filter = magics[0]

    assert not text_filter.resolve(SimpleNamespace(text="/support"))
    assert not text_filter.resolve(SimpleNamespace(text="/bug оплата не прошла"))
    assert text_filter.resolve(SimpleNamespace(text="3000"))


# --- (2) isdecimal(), not isdigit(): unicode digit look-alikes never reach int() ---


async def test_topup_custom_amount_rejects_unicode_digit_without_crashing(uow: UnitOfWork) -> None:
    """'²'.isdigit() is True but int('²') raises ValueError — this used to blow up the handler
    (and, in the real bot, spam admins via the global error handler) on every such message."""
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    state = _fsm_state()
    await state.set_state(purchase.TopupForm.waiting_amount)
    message = _FakeMessage("²")

    await purchase.topup_custom_amount(message, _container(uow), user, state)  # must not raise

    assert message.answers and "целое число" in message.answers[0]


async def test_topup_amount_callback_rejects_unicode_digit_without_crashing(
    uow: UnitOfWork,
) -> None:
    """Same class of bug via a forged callback: "topup:²" used to reach int() and crash."""
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    cb = _FakeCb("topup:²")
    await purchase.topup_amount(cb, _container(uow), user)  # must not raise

    assert cb.answers and cb.answers[0][1].get("show_alert") is True
    assert "Некорректная" in (cb.answers[0][0] or "")


async def test_topup_pay_callback_rejects_unicode_digit_without_crashing(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    """Same for the payment-method step: "topupm:²:stars" used to reach int() and crash."""

    async def _fake_render_screen(
        target: Any, container: Any, screen_key: str, caption: str, markup: Any = None
    ) -> None:
        return None

    monkeypatch.setattr(purchase, "render_screen", _fake_render_screen)
    async with uow:
        user = await make_user(uow)
        await uow.commit()

    cb = _FakeCb("topupm:²:stars")
    await purchase.topup_pay(cb, _container(uow), user)  # must not raise -> bounces to the menu

    assert cb.answers  # topup_menu (fallback) always acks


# --- (3) the amount survives even when an operator's screen text hides it ------


async def test_topup_method_amount_button_survives_full_custom_screen_text(
    monkeypatch: pytest.MonkeyPatch, uow: UnitOfWork
) -> None:
    """A saved SCREEN_TEXTS override for "topup_method" replaces the WHOLE caption and can easily
    drop "К зачислению: ...". The amount must still be visible: it's baked into a dynamic button
    that screen_buttons.SAFE_SCREENS never registers for this screen (only "topup:menu" is), so
    apply_screen_buttons always carries it through byte-for-byte regardless of any text/button
    override an operator saves.
    """
    captured: dict[str, Any] = {}

    async def _fake_show_media_screen(target: Any, photo: Any, caption: str, markup: Any) -> None:
        captured["caption"] = caption
        captured["markup"] = markup

    monkeypatch.setattr(banners, "show_media_screen", _fake_show_media_screen)

    cfg = BotConfigService()
    async with uow:
        user = await make_user(uow)
        await _seed_yookassa(uow)
        await cfg.set_values(
            uow,
            {
                "BANNER_ENABLED": False,
                # A plausible operator rewrite that never mentions the amount at all.
                "SCREEN_TEXTS": json.dumps({"topup_method": "<b>Оплата</b>\n\nВыбери способ."}),
            },
        )
        await uow.commit()

    cb = _FakeCb("topup:50000")
    await purchase._show_topup_methods(cb, _container(uow, bot_config=cfg), user, 50000)

    # Sanity: the override really did replace the caption and drop the amount from it.
    assert captured["caption"] == "<b>Оплата</b>\n\nВыбери способ."
    assert "500" not in captured["caption"]

    amount_buttons = [
        b.text
        for row in captured["markup"].inline_keyboard
        for b in row
        if b.callback_data == "topup:amt"
    ]
    assert amount_buttons == ["💰 К зачислению: 500 ₽"]


async def test_topup_amount_pill_callback_is_a_harmless_noop(uow: UnitOfWork) -> None:
    """Tapping the amount readout button must never crash or navigate anywhere unexpected."""
    cb = _FakeCb("topup:amt")
    await purchase.topup_amount_pill(cb)
    assert cb.answers == [(None, {})]
