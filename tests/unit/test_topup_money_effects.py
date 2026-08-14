"""Money effects of gateway-funded top-ups (docs/context/03): overpayment handling in
PaymentService.process and the "Мой налог" receipt queue.

Two real consequences surfaced once top-ups started flowing through editable-sum payment
gateways instead of only Telegram Stars:

1. A quickpay-style form lets the payer type ANY sum. Underpayment was already gated
   (UNDERPAYMENT_TOLERANCE); overpayment used to vanish silently — the invoice amount was
   credited and the difference was never recorded anywhere. process() now credits what
   actually arrived above a symmetric ceiling and tasks.py alerts admins with both figures.
2. Real acquiring money into DEPOSIT now needs a fiscal receipt too (previously only
   SUBSCRIPTION_PAYMENT did, because DEPOSIT was Stars-only). list_unreceipted must pick up
   gateway-funded top-ups without double-filing when that balance is later spent.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from src.application.dto.pricing import PurchaseRequest
from src.application.services.payment import PaymentService
from src.application.services.pricing import PricingService
from src.application.services.purchase import PurchaseService
from src.application.services.referral import ReferralService
from src.application.services.remnawave import RemnawaveService
from src.application.services.subscription import SubscriptionService
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.taskiq.tasks import _notify_overpayment_admins
from tests.factories import make_plan, make_user
from tests.fakes import FakeRemnawaveClient, RecordingEventBus


class _RecordingNotifier:
    """Minimal notifier double — mirrors the container.notifier protocol used in tasks.py."""

    def __init__(self) -> None:
        self.user_msgs: list[tuple[int, str]] = []
        self.admin_msgs: list[str] = []

    async def notify_user(self, telegram_id: int, text: str) -> None:
        self.user_msgs.append((telegram_id, text))

    async def notify_admins(self, text: str, *, topic: str | None = None) -> None:
        self.admin_msgs.append(text)


def _services() -> tuple[PurchaseService, PaymentService]:
    bus = RecordingEventBus()
    subs = SubscriptionService(RemnawaveService(FakeRemnawaveClient()))
    purchase = PurchaseService(PricingService(), subs, bus)
    return purchase, PaymentService(purchase, bus, ReferralService(bus))


async def _make_deposit(
    uow: UnitOfWork,
    user_id: int,
    *,
    amount_minor: int,
    gateway_type: PaymentGatewayType | None,
    external_id: str | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        type=TransactionType.DEPOSIT,
        status=TransactionStatus.PENDING,
        amount_minor=amount_minor,
        currency=Currency.RUB,
        gateway_type=gateway_type,
        external_id=external_id,
    )
    await uow.transactions.add(txn)
    return txn


# --- overpayment: credited amount + admin alert ------------------------------------------


async def test_overpayment_credits_actual_amount_received(uow: UnitOfWork) -> None:
    """An editable-sum quickpay form: invoice was 1000 RUB, the payer typed 1500 RUB — 1.5x, still
    under PaymentService.OVERPAYMENT_CAP_RATIO (2x). The balance must be credited the actual 1500,
    not the smaller invoice — the old code silently kept the 500 RUB difference for the operator.
    (A report beyond the 2x cap is a separate, deliberately UNtrusted case — see
    ``tests/unit/test_overpay_cap.py``.)"""
    _purchase, payments = _services()
    async with uow:
        user = await make_user(uow, telegram_id=42001)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,  # invoiced 1000 ₽
            gateway_type=PaymentGatewayType.YOOMONEY,
            external_id="ym-overpay-1",
        )
        await uow.commit()
        payment_id = txn.payment_id

        moved = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=150_000
        )
        await uow.commit()
        assert moved is True

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.status is TransactionStatus.COMPLETED
        assert settled.amount_minor == 150_000  # credited the actual sum, not the 1000 ₽ invoice

        credited_user = await uow.users.get(user.id)
        assert credited_user is not None
        assert credited_user.balance_minor == 150_000


async def test_overpayment_alerts_admins_with_both_amounts(uow: UnitOfWork) -> None:
    """Symmetric to the underpayment admin alert (test_audit_regressions.py) — a human must be
    able to see both the invoice and what actually arrived, to judge whether it's worth a
    refund."""
    notifier = _RecordingNotifier()
    async with uow:
        user = await make_user(uow, telegram_id=42002)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,
            gateway_type=PaymentGatewayType.YOOMONEY,
            external_id="ym-overpay-2",
        )
        await uow.commit()
        payment_id = txn.payment_id

    container = SimpleNamespace(uow=lambda: uow, notifier=notifier)
    await _notify_overpayment_admins(
        container, payment_id, expected_minor=100_000, received_minor=300_000
    )
    assert notifier.admin_msgs
    assert "Переплата" in notifier.admin_msgs[0]
    assert "1 000" in notifier.admin_msgs[0]  # expected
    assert "3 000" in notifier.admin_msgs[0]  # received
    assert not notifier.user_msgs  # the buyer's own balance_topup DM already carries the number


async def test_moderate_overpay_within_tolerance_keeps_invoice_amount(uow: UnitOfWork) -> None:
    """Below the symmetric ceiling (1.05x, mirroring the 0.95x underpayment floor) a payment
    close to the invoice is treated as an exact match — this must not regress into crediting a
    slightly-off "received" figure for ordinary rounding noise."""
    _purchase, payments = _services()
    async with uow:
        user = await make_user(uow, telegram_id=42003)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,
            gateway_type=PaymentGatewayType.YOOKASSA,
            external_id="yk-overpay-tol",
        )
        await uow.commit()
        payment_id = txn.payment_id

        # 103 000 is +3% — inside the tolerance band, must NOT trigger the overpay branch.
        moved = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=103_000
        )
        await uow.commit()
        assert moved is True

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.amount_minor == 100_000  # unchanged — same as pre-existing behaviour

        credited_user = await uow.users.get(user.id)
        assert credited_user is not None
        assert credited_user.balance_minor == 100_000


# --- underpayment gate must still work ----------------------------------------------------


async def test_underpayment_gate_still_rejects(uow: UnitOfWork) -> None:
    """Regression guard: restructuring process() for the overpay branch must not weaken the
    existing underpayment gate."""
    _purchase, payments = _services()
    async with uow:
        user = await make_user(uow, telegram_id=42004)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,
            gateway_type=PaymentGatewayType.YOOMONEY,
            external_id="ym-underpay",
        )
        await uow.commit()
        payment_id = txn.payment_id

        moved = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=50_000
        )
        await uow.commit()
        assert moved is True  # advanced PENDING -> FAILED

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.status is TransactionStatus.FAILED
        assert settled.amount_minor == 100_000  # invoice untouched on a rejected underpayment

        untouched_user = await uow.users.get(user.id)
        assert untouched_user is not None
        assert untouched_user.balance_minor == 0


# --- idempotency: a duplicate webhook after an overpay must be a safe no-op ---------------


async def test_duplicate_overpay_webhook_does_not_double_credit(uow: UnitOfWork) -> None:
    """A resent/late-retried webhook for an already-settled overpaid txn must not credit the
    balance twice and must not re-touch the settled amount_minor."""
    _purchase, payments = _services()
    async with uow:
        user = await make_user(uow, telegram_id=42005)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,
            gateway_type=PaymentGatewayType.YOOMONEY,
            external_id="ym-dup-overpay",
        )
        await uow.commit()
        payment_id = txn.payment_id

        first = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=150_000
        )
        await uow.commit()
        assert first is True

        once_credited = await uow.users.get(user.id)
        assert once_credited is not None
        assert once_credited.balance_minor == 150_000

        second = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=150_000
        )
        await uow.commit()
        assert second is False  # already terminal — duplicate webhook is a no-op

        still_credited = await uow.users.get(user.id)
        assert still_credited is not None
        assert still_credited.balance_minor == 150_000  # not credited a second time

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.amount_minor == 150_000  # not re-mutated by the duplicate


# --- "Мой налог" receipts: kassa top-ups in, balance purchases and Stars out ---------------


def _floor() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=1)


async def test_kassa_deposit_is_a_receipt_candidate_once(uow: UnitOfWork) -> None:
    """A completed gateway-funded top-up must show up in list_unreceipted — and stop showing
    up the moment it is claimed/filed, so the periodic filer can never double-register it."""
    async with uow:
        user = await make_user(uow, telegram_id=42006)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=50_000,
            gateway_type=PaymentGatewayType.YOOKASSA,
            external_id="yk-receipt-1",
        )
        txn.status = TransactionStatus.COMPLETED
        await uow.commit()

        candidates = await uow.transactions.list_unreceipted(newer_than=_floor())
        assert [c.payment_id for c in candidates] == [txn.payment_id]

        # Simulate issue_nalogo_receipts claiming, then filing, the row.
        txn.receipt_created_at = dt.datetime.now(dt.UTC)
        await uow.commit()
        assert await uow.transactions.list_unreceipted(newer_than=_floor()) == []

        txn.receipt_uuid = "receipt-123"
        await uow.commit()
        assert await uow.transactions.list_unreceipted(newer_than=_floor()) == []


async def test_balance_funded_purchase_is_not_a_receipt_candidate(uow: UnitOfWork) -> None:
    """A subscription paid FROM the balance (gateway_type is NULL) must never surface here —
    it was already fiscally receipted once, at top-up time."""
    async with uow:
        user = await make_user(uow, telegram_id=42007)
        plan, _ = await make_plan(uow, price_minor=10_000, days=30)
        await uow.commit()
        purchase, _payments = _services()
        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        # Fund the balance directly (bypassing the gateway) — only gateway_type matters here.
        await uow.users.increment_balance(user, 10_000)
        await uow.commit()
        sub_txn, _ = await purchase.checkout_from_balance(uow, req)
        await uow.commit()
        assert sub_txn.status is TransactionStatus.COMPLETED
        assert sub_txn.gateway_type is None

        assert await uow.transactions.list_unreceipted(newer_than=_floor()) == []


async def test_stars_deposit_is_not_a_receipt_candidate(uow: UnitOfWork) -> None:
    """A Telegram Stars top-up never stamps gateway_type — it must stay excluded exactly like
    before this change (Stars aren't cash/card money through a fiscal register)."""
    async with uow:
        user = await make_user(uow, telegram_id=42008)
        await uow.commit()
        txn = await _make_deposit(uow, user.id, amount_minor=20_000, gateway_type=None)
        txn.status = TransactionStatus.COMPLETED
        await uow.commit()

        assert await uow.transactions.list_unreceipted(newer_than=_floor()) == []


async def test_topup_then_spend_on_subscription_does_not_double_receipt(uow: UnitOfWork) -> None:
    """End-to-end: pay a kassa top-up (one receipt candidate), then spend that balance on a
    subscription — the spend must NOT add a second candidate for the same money."""
    _purchase, payments = _services()
    async with uow:
        user = await make_user(uow, telegram_id=42009)
        plan, _ = await make_plan(uow, price_minor=10_000, days=30)
        await uow.commit()

        deposit = await _make_deposit(
            uow,
            user.id,
            amount_minor=50_000,
            gateway_type=PaymentGatewayType.YOOKASSA,
            external_id="yk-e2e-1",
        )
        await uow.commit()
        deposit_payment_id = deposit.payment_id

        moved = await payments.process(
            uow,
            payment_id=deposit_payment_id,
            status=TransactionStatus.COMPLETED,
            amount_minor=50_000,
        )
        await uow.commit()
        assert moved is True

        candidates = await uow.transactions.list_unreceipted(newer_than=_floor())
        assert [c.payment_id for c in candidates] == [deposit_payment_id]

        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        sub_txn, _ = await _purchase.checkout_from_balance(uow, req)
        await uow.commit()
        assert sub_txn.status is TransactionStatus.COMPLETED
        assert sub_txn.gateway_type is None  # balance spend, not a gateway payment

        candidates_after = await uow.transactions.list_unreceipted(newer_than=_floor())
        # Still just the one deposit — the balance spend never adds a second candidate.
        assert [c.payment_id for c in candidates_after] == [deposit_payment_id]
