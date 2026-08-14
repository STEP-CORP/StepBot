"""Transaction DAO — the idempotent payment state machine lives here (docs/context/03)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import CursorResult, select, update

from src.core.enums import PaymentGatewayType, TransactionStatus, TransactionType
from src.infrastructure.database.base import utcnow
from src.infrastructure.database.dao.base import BaseDAO
from src.infrastructure.database.models.transaction import Transaction


class TransactionDAO(BaseDAO[Transaction]):
    model = Transaction

    async def get_by_payment_id(self, payment_id: uuid.UUID) -> Transaction | None:
        return await self.find_one(payment_id=payment_id)

    async def get_by_external(
        self, external_id: str, gateway_type: PaymentGatewayType
    ) -> Transaction | None:
        return await self.find_one(external_id=external_id, gateway_type=gateway_type)

    async def list_recent(self, user_id: int, limit: int = 20) -> list[Transaction]:
        """Latest transactions first — LIMIT without ORDER BY returns arbitrary rows."""
        stmt = (
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.id.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_unreceipted(
        self, *, newer_than: dt.datetime, limit: int = 50
    ) -> list[Transaction]:
        """Completed external money-in transactions still without a fiscal receipt.

        Covers gateway-paid subscriptions (as before) AND gateway top-ups of the wallet
        (DEPOSIT) — real acquiring now funds top-ups too, and that money needs a "Мой налог"
        receipt exactly like a direct purchase. ``gateway_type IS NOT NULL`` is the load-bearing
        filter here: a balance-funded purchase (``PurchaseService.checkout_from_balance``) never
        stamps it, so spending an already-receipted top-up on a subscription can never file a
        second receipt for the same money. A Telegram Stars top-up also leaves it NULL (no
        gateway_type is ever stamped on that flow) — Stars aren't cash/card money through a
        fiscal register, so they correctly stay out of this query, same as before this change.
        """
        stmt = (
            select(Transaction)
            .where(
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.type.in_(
                    (TransactionType.SUBSCRIPTION_PAYMENT, TransactionType.DEPOSIT)
                ),
                Transaction.gateway_type.is_not(None),
                Transaction.amount_minor > 0,
                Transaction.receipt_uuid.is_(None),
                # receipt_created_at is stamped as an "in-flight" claim BEFORE the irreversible
                # fiscal registration, so a crash between filing and the receipt_uuid write can't
                # re-file the same income on the next run (nalogo idempotency). The importers
                # (shopbot_import / bedolaga_import / remnashop_import) reuse the SAME field as a
                # permanent "never file" marker: they insert rows straight into COMPLETED (never
                # through PaymentService's CAS), often WITHOUT a source date — which would
                # otherwise fall back to "now" and land inside this query's 3-day window. A
                # historical top-up/purchase from someone else's bot must never get registered as
                # today's income under THIS INN, so the importers pre-stamp receipt_created_at at
                # insert time and this filter excludes them exactly like an in-flight row.
                Transaction.receipt_created_at.is_(None),
                Transaction.created_at > newer_than,
            )
            .order_by(Transaction.created_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_stuck_pending(
        self, *, older_than: dt.datetime, newer_than: dt.datetime, limit: int = 50
    ) -> list[Transaction]:
        """PENDING gateway transactions old enough to suspect a lost webhook/fulfilment."""
        stmt = (
            select(Transaction)
            .where(
                Transaction.status == TransactionStatus.PENDING,
                Transaction.gateway_type.is_not(None),
                Transaction.external_id.is_not(None),
                Transaction.created_at < older_than,
                Transaction.created_at > newer_than,
            )
            .order_by(Transaction.created_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def lock_for_update(self, payment_id: uuid.UUID) -> Transaction | None:
        """Row-lock a transaction for the duration of the transaction (gotcha #6).

        ``with_for_update`` is a no-op on SQLite but correct on Postgres.
        """
        stmt = select(Transaction).where(Transaction.payment_id == payment_id).with_for_update()
        return (await self.session.scalars(stmt)).first()

    async def transition_status(
        self,
        payment_id: uuid.UUID,
        to_status: TransactionStatus,
        allowed_from: Iterable[TransactionStatus],
    ) -> bool:
        """Atomic CAS status change. Returns True iff a row moved (idempotent).

        Duplicate / late / out-of-order webhooks find the row already advanced and get
        ``False`` — the caller treats that as "already handled".
        """
        values: dict[str, object] = {"status": to_status}
        if to_status is TransactionStatus.COMPLETED:
            values["completed_at"] = utcnow()
        stmt = (
            update(Transaction)
            .where(
                Transaction.payment_id == payment_id,
                Transaction.status.in_(tuple(allowed_from)),
            )
            .values(**values)
        )
        result = await self.session.execute(stmt)
        return (cast("CursorResult[Any]", result).rowcount or 0) > 0
