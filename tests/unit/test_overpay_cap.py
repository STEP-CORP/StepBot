"""Overpayment sanity cap (docs/context/03): PaymentService.OVERPAYMENT_CAP_RATIO.

Before this cap, ANY webhook-reported amount above the invoice was credited in full and
`txn.amount_minor` was overwritten with it — no matter how large. Since every gateway webhook
parser hardcodes ``Currency.RUB`` on the reported amount, the currency cross-check never
actually rejects anything: a misreporting gateway (kopecks where rubles are expected, a
foreign-currency payout, an aggregator quirk) would inflate the credited balance, the referral
commission paid out on top of it, AND the "Мой налог" fiscal receipt — real money leaving the
business on a number nobody actually sent.

This file covers:
1. a moderate overpay (within the 2x cap) still credits what actually arrived, unchanged;
2. an overpay PAST the cap credits only the invoice and fires a distinct, louder admin alert
   instead of trusting the reported figure;
3. the invoice amount survives the mutation (readable from ``pricing`` after the fact);
4. a duplicate/late webhook resend is a safe no-op in both cases (no double credit, no
   double alert, no re-mutation);
5. imported/historical transactions (shopbot_import et al.) never enter the "Мой налог" receipt
   queue, while an ordinary gateway-settled top-up still does.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from src.application.services.payment import PaymentService
from src.application.services.pricing import PricingService
from src.application.services.purchase import PurchaseService
from src.application.services.referral import ReferralService
from src.application.services.remnawave import RemnawaveService
from src.application.services.shopbot_import import ShopbotImportService, read_source
from src.application.services.subscription import SubscriptionService
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus, TransactionType
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.uow import UnitOfWork
from tests.factories import make_user
from tests.fakes import FakeRemnawaveClient, RecordingEventBus


class _RecordingNotifier:
    """Minimal Notifier double — mirrors ``application.common.notifier.Notifier``."""

    def __init__(self) -> None:
        self.admin_msgs: list[str] = []

    async def notify_user(self, telegram_id: int, text: str) -> bool:
        return True

    async def notify_admins(self, text: str, *, topic: str | None = None) -> None:
        self.admin_msgs.append(text)

    async def notify_admins_document(self, document: object, *, caption: str | None = None) -> None:
        pass


def _services(notifier: _RecordingNotifier | None = None) -> tuple[PurchaseService, PaymentService]:
    bus = RecordingEventBus()
    subs = SubscriptionService(RemnawaveService(FakeRemnawaveClient()))
    purchase = PurchaseService(PricingService(), subs, bus)
    return purchase, PaymentService(purchase, bus, ReferralService(bus), notifier=notifier)


async def _make_deposit(
    uow: UnitOfWork, user_id: int, *, amount_minor: int, external_id: str
) -> Transaction:
    txn = Transaction(
        user_id=user_id,
        type=TransactionType.DEPOSIT,
        status=TransactionStatus.PENDING,
        amount_minor=amount_minor,
        currency=Currency.RUB,
        gateway_type=PaymentGatewayType.YOOMONEY,
        external_id=external_id,
    )
    await uow.transactions.add(txn)
    return txn


# --- within the cap: still credits the actual figure --------------------------------------


async def test_moderate_overpay_within_cap_credits_actual_amount(uow: UnitOfWork) -> None:
    """1.8x invoice — above OVERPAYMENT_TOLERANCE (1.05x), below OVERPAYMENT_CAP_RATIO (2x): a
    payer plausibly typed a bigger sum into an editable quickpay form. Credit what arrived."""
    notifier = _RecordingNotifier()
    _purchase, payments = _services(notifier)
    async with uow:
        user = await make_user(uow, telegram_id=51001)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,
            external_id="cap-moderate-1",  # invoiced 1000 ₽
        )
        await uow.commit()
        payment_id = txn.payment_id

        moved = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=180_000
        )
        await uow.commit()
        assert moved is True

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.amount_minor == 180_000  # credited the actual sum received

        credited_user = await uow.users.get(user.id)
        assert credited_user is not None
        assert credited_user.balance_minor == 180_000

    # Alerted (routine heads-up), not with the louder over-cap wording.
    assert len(notifier.admin_msgs) == 1
    assert "ВЫШЕ ПРЕДЕЛА" not in notifier.admin_msgs[0]
    assert "1 000" in notifier.admin_msgs[0]  # invoice
    assert "1 800" in notifier.admin_msgs[0]  # received


# --- past the cap: invoice-only credit + a louder alert -------------------------------------


async def test_overpay_above_cap_credits_invoice_only(uow: UnitOfWork) -> None:
    """2.5x invoice — past OVERPAYMENT_CAP_RATIO (2x). A number this far off the invoice is more
    consistent with a gateway/currency parsing bug than a genuine gift: auto-crediting it would
    also inflate the referral commission and the fiscal receipt on money nobody sent. Only the
    invoice amount is credited; the anomalous figure is never auto-applied."""
    notifier = _RecordingNotifier()
    _purchase, payments = _services(notifier)
    async with uow:
        user = await make_user(uow, telegram_id=51002)
        await uow.commit()
        txn = await _make_deposit(
            uow,
            user.id,
            amount_minor=100_000,
            external_id="cap-over-1",  # invoiced 1000 ₽
        )
        await uow.commit()
        payment_id = txn.payment_id

        moved = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=250_000
        )
        await uow.commit()
        assert moved is True

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.status is TransactionStatus.COMPLETED
        assert settled.amount_minor == 100_000  # invoice only — the 250 000 figure is NOT trusted

        credited_user = await uow.users.get(user.id)
        assert credited_user is not None
        assert credited_user.balance_minor == 100_000  # not 250 000

    # A distinct, more alarming alert — not the routine overpay heads-up.
    assert len(notifier.admin_msgs) == 1
    msg = notifier.admin_msgs[0]
    assert "ВЫШЕ ПРЕДЕЛА" in msg
    assert "1 000" in msg  # invoice
    assert "2 500" in msg  # what the webhook reported (never credited)


# The referral commission (`_REWARDABLE` in payment.py) is paid on ``txn.amount_minor`` — the
# exact field asserted above to stay at the invoice (100 000) for an over-cap overpay, and at the
# credited figure for a moderate one. There is no second, separately-computed "amount charged"
# anywhere in the reward path, so those two assertions already rule out an inflated payout.


# --- invoice amount survives the mutation ----------------------------------------------------


async def test_invoice_amount_recorded_before_mutation(uow: UnitOfWork) -> None:
    """After settlement, both the original invoice AND the webhook's reported figure must still
    be readable — DEPOSIT's ``pricing`` is otherwise empty, so this is the only place either
    number survives once ``amount_minor`` itself is mutated (or capped) away from the invoice."""
    _purchase, payments = _services(_RecordingNotifier())
    async with uow:
        user = await make_user(uow, telegram_id=51003)
        await uow.commit()
        moderate = await _make_deposit(
            uow, user.id, amount_minor=100_000, external_id="pricing-moderate"
        )
        capped = await _make_deposit(uow, user.id, amount_minor=100_000, external_id="pricing-cap")
        await uow.commit()

        await payments.process(
            uow,
            payment_id=moderate.payment_id,
            status=TransactionStatus.COMPLETED,
            amount_minor=180_000,
        )
        await payments.process(
            uow,
            payment_id=capped.payment_id,
            status=TransactionStatus.COMPLETED,
            amount_minor=250_000,
        )
        await uow.commit()

        settled_moderate = await uow.transactions.get_by_payment_id(moderate.payment_id)
        assert settled_moderate is not None
        assert settled_moderate.amount_minor == 180_000  # mutated
        assert settled_moderate.pricing["invoice_amount_minor"] == 100_000  # original, preserved
        assert settled_moderate.pricing["webhook_received_minor"] == 180_000

        settled_capped = await uow.transactions.get_by_payment_id(capped.payment_id)
        assert settled_capped is not None
        assert settled_capped.amount_minor == 100_000  # never mutated (invoice stands)
        assert settled_capped.pricing["invoice_amount_minor"] == 100_000
        # The anomalous received figure is on record even though it was never credited —
        # otherwise it is only ever visible in a Telegram alert, not in the audit trail.
        assert settled_capped.pricing["webhook_received_minor"] == 250_000


# --- idempotency: duplicate/late webhook is a safe no-op in both branches -------------------


async def test_duplicate_webhook_after_capped_overpay_is_idempotent(uow: UnitOfWork) -> None:
    """A resent webhook for an already-settled, over-cap-overpaid txn must not credit twice,
    must not re-touch amount_minor/pricing, and must not fire a second alert."""
    notifier = _RecordingNotifier()
    _purchase, payments = _services(notifier)
    async with uow:
        user = await make_user(uow, telegram_id=51004)
        await uow.commit()
        txn = await _make_deposit(uow, user.id, amount_minor=100_000, external_id="cap-dup-1")
        await uow.commit()
        payment_id = txn.payment_id

        first = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=250_000
        )
        await uow.commit()
        assert first is True

        second = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=250_000
        )
        await uow.commit()
        assert second is False  # already terminal — duplicate webhook is a no-op

        settled = await uow.transactions.get_by_payment_id(payment_id)
        assert settled is not None
        assert settled.amount_minor == 100_000  # unchanged by the duplicate

        credited_user = await uow.users.get(user.id)
        assert credited_user is not None
        assert credited_user.balance_minor == 100_000  # credited exactly once

    assert len(notifier.admin_msgs) == 1  # not re-alerted on the duplicate


# --- "Мой налог" receipts must not fire on historical/imported money -----------------------


def _floor() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=3)


async def test_normal_kassa_deposit_is_receipt_candidate(uow: UnitOfWork) -> None:
    """A live, gateway-settled top-up must still show up for "Мой налог" filing — the import
    exclusion below must not accidentally swallow real, current money too."""
    _purchase, payments = _services()
    async with uow:
        user = await make_user(uow, telegram_id=51005)
        await uow.commit()
        txn = await _make_deposit(uow, user.id, amount_minor=50_000, external_id="receipt-live-1")
        await uow.commit()
        payment_id = txn.payment_id

        moved = await payments.process(
            uow, payment_id=payment_id, status=TransactionStatus.COMPLETED, amount_minor=50_000
        )
        await uow.commit()
        assert moved is True

        candidates = await uow.transactions.list_unreceipted(newer_than=_floor())
        assert [c.payment_id for c in candidates] == [payment_id]


def _make_shopbot_source(path: Path, *, created_date: str | None) -> None:
    """Minimal shopbot users.db: one user, one paid top-up. ``vpn_keys``/``promo_codes`` are
    intentionally absent — ``read_source`` tolerates missing tables (empty list)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY, username TEXT, total_spent REAL,
            trial_used BOOLEAN, registration_date TIMESTAMP, is_banned BOOLEAN,
            balance REAL, referred_by INTEGER, referral_balance REAL
        );
        CREATE TABLE transactions (
            transaction_id INTEGER PRIMARY KEY, payment_id TEXT, user_id INTEGER,
            status TEXT, amount_rub REAL, payment_method TEXT, metadata TEXT,
            created_date TIMESTAMP
        );
        """
    )
    conn.execute(
        "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
        (601, "importeduser", 0.0, 0, "2024-01-01 00:00:00", 0, 0.0, None, 0.0),
    )
    conn.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
        (
            1,
            "yk-import-1",
            601,
            "paid",
            1000.0,
            "YooKassa",
            '{"action": "top_up"}',
            created_date,
        ),
    )
    conn.commit()
    conn.close()


async def test_imported_deposit_is_never_a_receipt_candidate(
    uow: UnitOfWork, tmp_path: Path
) -> None:
    """Worst case from the incident: the source dump has NO date for the row, so the importer's
    fallback stamps ``created_at`` to "now" — squarely inside any 3-day nalogo lookback window.
    The transaction must still never surface for fiscal filing: it is someone else's historical
    income, already outside this business's "Мой налог" registration."""
    db = tmp_path / "users.db"
    _make_shopbot_source(db, created_date=None)

    service = ShopbotImportService(ReferralService(RecordingEventBus()))
    async with uow:
        summary = await service.run(uow, read_source(db))
        await uow.commit()
    assert summary["transactions"] == 1

    async with uow:
        imported = await uow.transactions.find_one(external_id="yk-import-1")
        assert imported is not None
        assert imported.type is TransactionType.DEPOSIT
        assert imported.status is TransactionStatus.COMPLETED
        assert imported.gateway_type is not None  # would otherwise match list_unreceipted anyway

        candidates = await uow.transactions.list_unreceipted(newer_than=_floor())
        assert imported.payment_id not in [c.payment_id for c in candidates]
