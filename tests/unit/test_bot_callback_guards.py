"""Bot callback-data hardening + reply-menu escape hatch.

- purchase.py handlers must not raise on crafted/stale ``callback_data`` — they fall back
  to the relevant menu instead of ``int(...)``-ing garbage.
- promo/withdraw text handlers must yield a bottom-bar tap (reply mode) to the menu rather
  than swallow the label as a promocode / withdrawal details.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.bot.handlers import purchase, reply_menu


class _FakeCb:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[Any, dict[str, Any]]] = []

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))


# --- Fix #5: crafted/stale purchase callbacks fall back, never raise -----------


@pytest.mark.parametrize(
    ("handler", "fallback", "data"),
    [
        ("show_durations", "open_buy", "plan:abc"),
        ("show_durations", "open_buy", "plan:"),
        ("choose_payment", "open_buy", "dur:1"),  # wrong arity
        ("choose_payment", "open_buy", "dur:1:x"),  # non-numeric days
        ("pay", "open_buy", "pay:1:2"),  # wrong arity
        ("pay", "open_buy", "pay:1:two:bal"),
        ("constructor_packs", "show_constructor", "cper:x"),
        ("constructor_payment", "show_constructor", "cpack:1"),
        ("constructor_pay", "show_constructor", "cpay:1:2"),
        ("traffic_pack_pay", "traffic_menu", "tpack:x"),
        ("traffic_pay", "traffic_menu", "tpay:x:bal"),
        ("topup_pay", "topup_menu", "topupm:1"),  # wrong arity
        ("topup_pay", "topup_menu", "topupm:x:stars"),  # non-numeric amount
    ],
)
async def test_crafted_callback_falls_back_without_raising(
    monkeypatch: pytest.MonkeyPatch, handler: str, fallback: str, data: str
) -> None:
    called: list[str | None] = []

    async def _fake_fallback(cb: Any, container: Any, db_user: Any) -> None:
        called.append(cb.data)

    monkeypatch.setattr(purchase, fallback, _fake_fallback)
    cb = _FakeCb(data)
    # container/db_user are never touched before the guard returns.
    await getattr(purchase, handler)(cb, container=object(), db_user=object())
    assert called == [data]


async def test_topup_amount_rejects_non_numeric() -> None:
    cb = _FakeCb("topup:abc")
    await purchase.topup_amount(cb, container=object(), db_user=object())
    assert cb.answers and cb.answers[0][1].get("show_alert") is True


# --- Fix #3: reply-menu bottom-bar tap escapes promo/withdraw FSM input --------


class _FakeState:
    def __init__(self) -> None:
        self.cleared = False

    async def clear(self) -> None:
        self.cleared = True


class _FakeUow:
    async def __aenter__(self) -> _FakeUow:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeContainer:
    def __init__(self, mode: str) -> None:
        self._mode = mode
        self.bot_config = SimpleNamespace(value=self._value)

    def uow(self) -> _FakeUow:
        return _FakeUow()

    async def _value(self, uow: Any, key: str) -> Any:
        return self._mode if key == "MAIN_MENU_MODE" else None


async def test_maybe_dispatch_noop_in_inline_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[Any] = []
    monkeypatch.setattr(
        reply_menu,
        "dispatch",
        lambda *a, **k: dispatched.append(a),  # never called
    )
    state: Any = _FakeState()
    msg: Any = SimpleNamespace(text="👤 Личный кабинет")
    container: Any = _FakeContainer("inline")
    db_user: Any = object()
    handled = await reply_menu.maybe_dispatch_menu_button(
        msg, container, db_user=db_user, state=state
    )
    assert handled is False
    assert state.cleared is False  # the pending form is left intact
    assert dispatched == []


async def test_maybe_dispatch_yields_matching_label_in_reply_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_match(container: Any, text: str) -> reply_menu.MenuMatch:
        return ("action", "cabinet", None)

    dispatched: list[reply_menu.MenuMatch] = []

    async def _fake_dispatch(
        message: Any, container: Any, db_user: Any, state: Any, menu_match: reply_menu.MenuMatch
    ) -> None:
        dispatched.append(menu_match)

    monkeypatch.setattr(reply_menu, "_match_button", _fake_match)
    monkeypatch.setattr(reply_menu, "dispatch", _fake_dispatch)
    state: Any = _FakeState()
    msg: Any = SimpleNamespace(text="👤 Личный кабинет")
    container: Any = _FakeContainer("reply")
    db_user: Any = object()
    handled = await reply_menu.maybe_dispatch_menu_button(
        msg, container, db_user=db_user, state=state
    )
    assert handled is True
    assert state.cleared is True  # form aborted, tap opens the menu action instead
    assert dispatched == [("action", "cabinet", None)]


async def test_maybe_dispatch_keeps_input_when_not_a_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_match(container: Any, text: str) -> None:
        return None

    monkeypatch.setattr(reply_menu, "_match_button", _no_match)
    state: Any = _FakeState()
    msg: Any = SimpleNamespace(text="PROMO2026")  # a real promocode, not a menu label
    container: Any = _FakeContainer("reply")
    db_user: Any = object()
    handled = await reply_menu.maybe_dispatch_menu_button(
        msg, container, db_user=db_user, state=state
    )
    assert handled is False
    assert state.cleared is False  # promo/withdraw keeps consuming it as input
