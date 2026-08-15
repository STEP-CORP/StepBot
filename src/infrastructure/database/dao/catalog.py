"""Thin per-aggregate DAOs for catalogue / config tables."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.enums import PaymentGatewayType
from src.infrastructure.database.dao.base import BaseDAO
from src.infrastructure.database.models.payment_gateway import PaymentGateway
from src.infrastructure.database.models.plan import Plan, PlanDuration
from src.infrastructure.database.models.promo_group import PromoGroup
from src.infrastructure.database.models.server_squad import ServerSquad
from src.infrastructure.database.models.settings import Settings


class PlanDAO(BaseDAO[Plan]):
    model = Plan

    async def get_by_code(self, public_code: str) -> Plan | None:
        return await self.find_one(public_code=public_code)

    async def list_with_durations(self) -> Sequence[Plan]:
        stmt = (
            select(Plan)
            .options(selectinload(Plan.durations).selectinload(PlanDuration.prices))
            .order_by(Plan.order_index, Plan.id)
        )
        return (await self.session.scalars(stmt)).all()

    async def get_with_durations(self, plan_id: int) -> Plan | None:
        stmt = (
            select(Plan)
            .options(selectinload(Plan.durations).selectinload(PlanDuration.prices))
            .where(Plan.id == plan_id)
        )
        return (await self.session.scalars(stmt)).first()


class ServerSquadDAO(BaseDAO[ServerSquad]):
    model = ServerSquad


class PromoGroupDAO(BaseDAO[PromoGroup]):
    model = PromoGroup

    async def get_default(self) -> PromoGroup | None:
        return await self.find_one(is_default=True)

    async def lock_for_update(self, group_id: int) -> PromoGroup | None:
        """Row-lock a group (no-op on SQLite, real on Postgres).

        Deletion counts the rows still pointing here; without the lock a promocode
        bound between the count and the DELETE survives with a NULLed group — live
        but permanently unredeemable. FOR UPDATE conflicts with the FOR KEY SHARE
        that the child insert takes, so the racer either waits and gets the 409 or
        waits and fails on the foreign key.
        """
        stmt = select(PromoGroup).where(PromoGroup.id == group_id).with_for_update()
        return (await self.session.scalars(stmt)).first()


class PaymentGatewayDAO(BaseDAO[PaymentGateway]):
    model = PaymentGateway

    async def get_active(self, gateway_type: PaymentGatewayType) -> PaymentGateway | None:
        return await self.find_one(type=gateway_type, is_active=True)


class SettingsDAO(BaseDAO[Settings]):
    model = Settings

    async def get_singleton(self) -> Settings | None:
        """Return the single settings row (id=1 by convention), or None if unseeded."""
        return await self.find_one()
