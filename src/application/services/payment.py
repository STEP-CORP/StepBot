"""PaymentService — the idempotent transaction-completion pipeline (docs/context/03).

Invoked by the taskiq worker after a verified webhook. Owns the CAS status transition so
duplicate / late / out-of-order callbacks are safe no-ops.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from uuid import UUID

from src.application.common.events import EventBus
from src.application.common.notifier import Notifier
from src.application.events import PaymentCompleted
from src.application.services.purchase import PurchaseService
from src.application.services.referral import ReferralService
from src.core.enums import TransactionStatus, TransactionType
from src.core.exceptions import NotFound
from src.core.logging import get_logger
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.services.reports import fmt_amount

if TYPE_CHECKING:
    from src.infrastructure.database.uow import UnitOfWork

# Transaction types that represent external money ENTERING the system — these trigger a
# referral commission (a balance-funded purchase does not; that money was rewarded on top-up).
log = get_logger(__name__)

_REWARDABLE = (TransactionType.DEPOSIT, TransactionType.SUBSCRIPTION_PAYMENT)


class PaymentService:
    def __init__(
        self,
        purchase: PurchaseService,
        event_bus: EventBus,
        referrals: ReferralService,
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self._purchase = purchase
        self._events = event_bus
        self._referrals = referrals
        # Optional by design (many tests build a PaymentService without one and don't care about
        # admin alerts — the cap/credit logic below still runs correctly, just silently). Wired
        # for real in AppContainer so this is the ONE common layer every settlement path shares
        # (webhook task, the periodic reconciler, AND the bot's on-demand "Проверить оплату") —
        # an overpayment alert can no longer depend on which entry point happened to settle it.
        self._notifier = notifier

    # Received amount may be net of provider fees. Only YooMoney reports net — a personal-wallet
    # quickpay whose sum the payer can EDIT (the abuse vector) — and its receiver fee is ≤3%, so a
    # legitimate full payment still nets ≥97%. Every OTHER gateway reports the gross charged amount
    # (== invoice, ratio 1.0). 0.95 keeps a 5% floor: it clears YooMoney's ≤3% fee with margin yet
    # rejects a payer who edited the sum down. The old 0.90 let them skim ~10% off every purchase.
    UNDERPAYMENT_TOLERANCE = 0.95

    # Symmetric ceiling for the same editable-sum forms (YooMoney quickpay lets the payer type
    # ANY sum, not just a smaller one). Silently crediting only the invoice amount used to just
    # let the difference vanish — real money taken, no record, nobody told. Above this ratio (and
    # up to OVERPAYMENT_CAP_RATIO below) we credit what actually arrived instead and alert admins
    # with both figures; below it a payment within 5% of the invoice is treated as an exact match
    # (rounding noise), the same tolerance shape as the underpayment floor.
    OVERPAYMENT_TOLERANCE = 1.05

    # Hard sanity ceiling on AUTO-crediting an overpay — never trust an unbounded number straight
    # off the wire. Every webhook parser (~15 gateways) hardcodes ``Currency.RUB`` on the reported
    # amount, so the currency equality check above is a no-op in practice: a misreporting gateway
    # (kopecks where rubles are expected, a foreign-currency payout, an aggregator quirk) is only
    # ever caught by THIS ratio. Up to 2x the invoice we still trust the figure — a payer editing
    # a quickpay form realistically doesn't type more than double. AT or above 2x, the gap looks
    # far more like a parsing bug than a deliberate gift: crediting it would also inflate the
    # referral payout and the "Мой налог" receipt on money nobody actually sent. Past this ratio
    # we credit ONLY the invoice amount (never the received figure, never the cap itself) and fire
    # a distinct, louder admin alert — a human must verify against the gateway's own dashboard
    # before crediting anything beyond what was quoted.
    OVERPAYMENT_CAP_RATIO = 2.0

    async def process(
        self,
        uow: UnitOfWork,
        *,
        payment_id: UUID,
        status: TransactionStatus,
        amount_minor: int | None = None,
    ) -> bool:
        """Apply a webhook outcome to a transaction. Returns True iff it advanced the state.

        A ``False`` return means the transaction was already in a terminal state — the
        webhook was a duplicate and must be treated as successfully handled.
        """
        txn = await uow.transactions.lock_for_update(payment_id)  # serialize concurrent hooks
        if txn is None:
            raise NotFound(f"transaction {payment_id} not found")

        overpaid = False  # within the cap: credit what actually arrived
        overpaid_capped = False  # past the cap: never trust the figure, credit the invoice only
        if (
            status is TransactionStatus.COMPLETED
            and amount_minor is not None
            and txn.amount_minor > 0
        ):
            if amount_minor < txn.amount_minor * self.UNDERPAYMENT_TOLERANCE:
                # Quickpay-style forms let the user edit the sum — never fulfil an underpayment.
                log.warning(
                    "underpaid webhook rejected",
                    payment_id=str(payment_id),
                    expected=txn.amount_minor,
                    received=amount_minor,
                )
                status = TransactionStatus.FAILED
            elif amount_minor > txn.amount_minor * self.OVERPAYMENT_CAP_RATIO:
                overpaid_capped = True  # handled below, only once CAS actually claims it
            elif amount_minor > txn.amount_minor * self.OVERPAYMENT_TOLERANCE:
                overpaid = True  # handled below, only once the CAS transition actually claims it

        if status is TransactionStatus.COMPLETED:
            moved = await uow.transactions.transition_status(
                payment_id, TransactionStatus.COMPLETED, (TransactionStatus.PENDING,)
            )
            if not moved:
                return False
            if (overpaid or overpaid_capped) and amount_minor is not None:
                # Only mutate/alert once CAS confirmed WE are the one settling it — a
                # duplicate/late resend of the same webhook finds the txn already terminal
                # (`moved` False above) and must never re-touch a settled transaction's amount.
                invoice_minor = txn.amount_minor
                # Freeze what was actually quoted BEFORE any mutation below, and the anomalous
                # received figure alongside it — DEPOSIT's ``pricing`` is otherwise empty, so once
                # amount_minor is (maybe) overwritten the invoice side of the story is gone for
                # good; a Telegram alert text is not an audit trail. This survives a duplicate/late
                # webhook untouched too, since it only ever runs on the transition that wins CAS.
                txn.pricing = {
                    **(txn.pricing or {}),
                    "invoice_amount_minor": invoice_minor,
                    "webhook_received_minor": amount_minor,
                }
                if overpaid_capped:
                    log.warning(
                        "overpaid webhook OVER CAP, invoice credited only",
                        payment_id=str(payment_id),
                        expected=invoice_minor,
                        received=amount_minor,
                        cap_ratio=self.OVERPAYMENT_CAP_RATIO,
                    )
                    # amount_minor deliberately NOT applied — the invoice amount stands. The
                    # reported figure is past the sanity cap and no longer trustworthy enough to
                    # auto-credit, let alone feed the referral commission or the fiscal receipt.
                else:
                    log.warning(
                        "overpaid webhook credited in full",
                        payment_id=str(payment_id),
                        expected=invoice_minor,
                        received=amount_minor,
                    )
                    # Credit what actually arrived rather than the smaller invoice — the
                    # alternative is the difference silently vanishing. This becomes the one
                    # number everything downstream uses (balance credit below, referral
                    # commission, the fiscal receipt, the PaymentCompleted event), so there is
                    # never a second "real" amount floating around.
                    txn.amount_minor = amount_minor
                await self._alert_overpay(
                    txn,
                    invoice_minor=invoice_minor,
                    received_minor=amount_minor,
                    capped=overpaid_capped,
                )
            await self._fulfill(uow, txn)
            # Referral commission on money entering (atomic, idempotent per transaction).
            if txn.type in _REWARDABLE and txn.amount_minor > 0:
                payer = await uow.users.get(txn.user_id)
                if payer is not None:
                    await self._referrals.reward_on_topup(
                        uow, payer=payer, amount_minor=txn.amount_minor, transaction_id=txn.id
                    )
            await self._events.publish(
                PaymentCompleted(
                    user_id=txn.user_id,
                    transaction_id=txn.id,
                    amount_minor=txn.amount_minor,
                    currency=txn.currency.value,
                )
            )
            return True

        if status in (TransactionStatus.CANCELED, TransactionStatus.FAILED):
            return await uow.transactions.transition_status(
                payment_id, status, (TransactionStatus.PENDING,)
            )

        return False

    async def _fulfill(self, uow: UnitOfWork, txn: Transaction) -> None:
        if txn.type is TransactionType.SUBSCRIPTION_PAYMENT:
            await self._purchase.fulfill(uow, txn)
        elif txn.type is TransactionType.DEPOSIT:
            user = await uow.users.get(txn.user_id)
            if user is not None:
                await uow.users.increment_balance(user, txn.amount_minor)  # atomic (no lost update)
                user.has_made_first_topup = True

    async def _alert_overpay(
        self, txn: Transaction, *, invoice_minor: int, received_minor: int, capped: bool
    ) -> None:
        """Best-effort admin DM, fired from the ONE place every settlement path shares
        (webhook task, the periodic reconciler, the bot's on-demand "Проверить оплату") — so
        the alert can never depend on which entry point happened to settle the payment.

        ``capped`` picks a visibly louder message: a routine moderate overpay (credited in
        full) is a heads-up; a past-the-cap report (invoice credited, difference withheld) is a
        likely gateway/parsing bug and needs a human to actually go check before anyone touches
        the difference.
        """
        if self._notifier is None:  # test doubles / callers that don't wire one — no alert
            return
        currency = txn.currency.value
        expected = fmt_amount(invoice_minor, currency)
        received = fmt_amount(received_minor, currency)
        if capped:
            text = (
                f"\U0001f6a8 Переплата ВЫШЕ ПРЕДЕЛА (x{self.OVERPAYMENT_CAP_RATIO:g}) по счёту "
                f"{txn.payment_id}: ожидалось {expected}, вебхук сообщил {received}. Начислена "
                "ТОЛЬКО сумма счёта — цифра больше похожа на ошибку шлюза/парсера, чем на "
                "реальную переплату. Сверьте платёж в кабинете шлюза, прежде чем доначислять "
                "разницу вручную."
            )
        else:
            text = (
                f"\U0001f4b8 Переплата по счёту {txn.payment_id}: ожидалось {expected}, "
                f"получено {received}. Зачислена фактически полученная сумма — "
                "проверьте, не ошибся ли плательщик."
            )
        with contextlib.suppress(Exception):  # a notifier blip must never fail the settlement
            await self._notifier.notify_admins(text, topic="payments")
