"""SubscriptionService — provisions and mutates subscriptions (panel-first, ADR-0005)."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from src.application.dto.pricing import PurchaseRequest
from src.application.services.ids import generate_short_id
from src.application.services.remnawave import RemnawaveService
from src.core.enums import PurchaseType, SubscriptionStatus
from src.core.exceptions import PurchaseError
from src.infrastructure.database.models.plan import Plan
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

if TYPE_CHECKING:
    from src.infrastructure.database.uow import UnitOfWork

_GIB = 1024**3


def _plan_snapshot(plan: Plan) -> dict[str, Any]:
    """Freeze the plan shape at purchase time (gotcha #8)."""
    return {
        "plan_id": plan.id,
        "name": plan.name,
        "type": plan.type.value,
        "traffic_limit_bytes": plan.traffic_limit_bytes,
        "device_limit": plan.device_limit,
        "traffic_limit_strategy": plan.traffic_limit_strategy,
        "internal_squads": list(plan.internal_squads),
        "external_squad": plan.external_squad,
    }


class SubscriptionService:
    def __init__(self, remnawave: RemnawaveService) -> None:
        self._remnawave = remnawave

    async def _fallback_squads(
        self, uow: UnitOfWork, *, is_trial: bool, rows: list[Any] | None = None
    ) -> tuple[str, ...]:
        """Squads to provision with when neither the request nor the plan names one.

        Without this an unconfigured plan/trial (e.g. the lazily-created ``Trial`` plan, whose
        internal_squads is empty) provisions a squad-less panel user, and Remnawave answers its
        subscription with the default demo hosts ("No hosts found / Check Internal Squads tab").
        Sells every available synced squad; trials narrow to the trial-eligible ones when the
        owner has flagged any (this is the first place ``is_trial_eligible`` is actually read).
        """
        rows = rows if rows is not None else list(await uow.server_squads.list())
        rows = [s for s in rows if s.is_available]
        if is_trial:
            rows = [s for s in rows if s.is_trial_eligible] or rows
        return tuple(str(s.squad_uuid) for s in rows)

    async def _resolve_squads(
        self,
        uow: UnitOfWork,
        requested: tuple[str, ...],
        *,
        is_trial: bool,
    ) -> tuple[str, ...]:
        """Drop stale plan references before they reach Remnawave.

        The local mirror is populated by the panel sync. If it has rows, an explicit plan is
        allowed to use only currently available squads; a deleted/renamed panel squad is treated
        like an unconfigured plan and falls back to the available mirror. An empty mirror is kept
        permissive for first-boot/demo installs where the panel has not been synced yet.
        """
        rows = list(await uow.server_squads.list())
        if not rows:
            return requested
        available = {str(row.squad_uuid) for row in rows if row.is_available}
        selected = tuple(squad for squad in requested if squad in available)
        return selected or await self._fallback_squads(uow, is_trial=is_trial, rows=rows)

    async def grant(
        self,
        uow: UnitOfWork,
        *,
        user: User,
        plan: Plan,
        req: PurchaseRequest,
        is_trial: bool = False,
        mark_paid: bool = True,
    ) -> Subscription:
        """Create a brand-new subscription: panel user first, then persist locally.

        ``mark_paid`` records that the user became a paying customer. A free promo grant passes
        False — a gift is neither a trial nor a purchase, so it must not flip has_had_paid (which
        gates NEW/EXISTING-only promos and feeds paid-conversion analytics)."""
        short_id = generate_short_id()
        now = dt.datetime.now(dt.UTC)
        expire_at = now + dt.timedelta(days=req.duration_days)
        # Constructor purchases override the (service) plan's limits with the picked pack.
        traffic_bytes = (
            req.traffic_limit_bytes
            if req.traffic_limit_bytes is not None
            else plan.traffic_limit_bytes or 0
        )
        device_limit = req.device_limit if req.device_limit is not None else plan.device_limit

        requested_squads = req.internal_squads or tuple(plan.internal_squads)
        # A panel squad can disappear while its UUID remains frozen in a plan snapshot. Resolve
        # against the synced mirror so this becomes a usable fallback instead of a Remnawave 500.
        squads = await self._resolve_squads(uow, requested_squads, is_trial=is_trial)
        spec = self._remnawave.build_spec(
            short_id=short_id,
            telegram_id=user.telegram_id,
            expire_at=expire_at,
            traffic_limit_bytes=traffic_bytes,
            device_limit=device_limit,
            internal_squads=squads,
            external_squad=req.external_squad or plan.external_squad,
        )
        # Panel-first: create the panel user OUTSIDE any assumption of a local commit.
        panel_user = await self._remnawave.provision(spec)

        snapshot = _plan_snapshot(plan)
        snapshot["traffic_limit_bytes"] = traffic_bytes or None
        snapshot["device_limit"] = device_limit
        subscription = Subscription(
            user_id=user.id,
            remnawave_uuid=panel_user.uuid,
            remnawave_id=panel_user.panel_id,
            short_id=short_id,
            plan_id=plan.id,
            plan_snapshot=snapshot,
            status=SubscriptionStatus.TRIAL if is_trial else SubscriptionStatus.ACTIVE,
            is_trial=is_trial,
            traffic_limit_bytes=traffic_bytes,
            device_limit=device_limit,
            internal_squads=list(spec.internal_squads),
            external_squad=spec.external_squad,
            start_at=now,
            expire_at=expire_at,
            subscription_url=panel_user.subscription_url,
        )
        await uow.subscriptions.add(subscription)

        user.current_subscription_id = subscription.id
        if is_trial:
            user.is_trial_available = False
        elif mark_paid:
            user.has_had_paid_subscription = True
        return subscription

    async def renew(
        self,
        uow: UnitOfWork,
        subscription: Subscription,
        *,
        days: int,
        telegram_id: int | None = None,
        adopt_plan: Plan | None = None,
    ) -> Subscription:
        """Extend an existing subscription and push the new expiry to the panel.

        ``telegram_id`` is passed in explicitly (not read from ``subscription.user``) to avoid
        an async lazy-load on the relationship. ``adopt_plan`` tags a plan-less (migrated) sub
        with the purchased plan so it stops looking like a "no plan" row — limits are left as
        they are (extension keeps the user's current setup; a plan switch is a CHANGE).
        """
        panel_ref = subscription.panel_ref
        if panel_ref is None:
            raise PurchaseError("cannot renew a subscription with no panel user")
        if adopt_plan is not None and subscription.plan_id is None:
            subscription.plan_id = adopt_plan.id
            subscription.plan_snapshot = _plan_snapshot(adopt_plan)
        base = subscription.expire_at or dt.datetime.now(dt.UTC)
        subscription.expire_at = max(base, dt.datetime.now(dt.UTC)) + dt.timedelta(days=days)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.grace_until = None  # paying lifts the grace window; full limits are restored
        spec = self._remnawave.build_spec(
            short_id=subscription.short_id,
            telegram_id=telegram_id,
            expire_at=subscription.expire_at,
            traffic_limit_bytes=subscription.traffic_limit_bytes,
            device_limit=subscription.device_limit,
            internal_squads=tuple(subscription.internal_squads or ()),
            external_squad=subscription.external_squad,
        )
        await self._remnawave.apply(panel_ref, spec)
        return subscription

    async def set_expiry(
        self,
        uow: UnitOfWork,
        subscription: Subscription,
        *,
        expire_at: dt.datetime,
        telegram_id: int | None = None,
    ) -> Subscription:
        """Set the subscription's expiry to an ABSOLUTE date (may extend OR shorten) and push it.

        Unlike :meth:`renew` (which only adds days), this lets an admin move the expiry to any
        target date — e.g. from a calendar. A date in the past marks the subscription expired.
        """
        panel_ref = subscription.panel_ref
        if panel_ref is None:
            raise PurchaseError("cannot change a subscription with no panel user")
        subscription.expire_at = expire_at
        subscription.status = (
            SubscriptionStatus.ACTIVE
            if expire_at > dt.datetime.now(dt.UTC)
            else SubscriptionStatus.EXPIRED
        )
        if subscription.status is SubscriptionStatus.ACTIVE:
            subscription.grace_until = None  # moving the expiry forward lifts the grace window
        spec = self._remnawave.build_spec(
            short_id=subscription.short_id,
            telegram_id=telegram_id,
            expire_at=subscription.expire_at,
            traffic_limit_bytes=subscription.traffic_limit_bytes,
            device_limit=subscription.device_limit,
            internal_squads=tuple(subscription.internal_squads or ()),
            external_squad=subscription.external_squad,
        )
        await self._remnawave.apply(panel_ref, spec)
        return subscription

    async def change(
        self,
        uow: UnitOfWork,
        subscription: Subscription,
        *,
        user: User,
        plan: Plan,
        req: PurchaseRequest,
        carryover_trial: bool = False,
        bonus_days: int = 0,
    ) -> Subscription:
        """Switch the subscription to another plan: same panel user, new period.

        The new expiry is the purchased duration PLUS the value carried over from the old period,
        so the user never loses time on a switch: ``bonus_days`` is a PAID remainder converted to
        days of the new plan (PricingService.change_bonus_days), and a TRIAL remainder is carried
        over whole when ``carryover_trial`` is set. Panel-first like every other write.
        """
        panel_ref = subscription.panel_ref
        if panel_ref is None:
            raise PurchaseError("cannot change a subscription with no panel user")
        bonus = bonus_days
        if carryover_trial and subscription.is_trial and subscription.expire_at is not None:
            remaining = (subscription.expire_at - dt.datetime.now(dt.UTC)).total_seconds()
            bonus += max(0, math.ceil(remaining / 86400))  # don't lose a partial trial day
        subscription.plan_id = plan.id
        subscription.plan_snapshot = _plan_snapshot(plan)
        subscription.is_trial = False
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.grace_until = None
        subscription.traffic_limit_bytes = (
            req.traffic_limit_bytes
            if req.traffic_limit_bytes is not None
            else (plan.traffic_limit_bytes or 0)
        )
        subscription.device_limit = (
            req.device_limit if req.device_limit is not None else plan.device_limit
        )
        if req.internal_squads:
            subscription.internal_squads = list(req.internal_squads)
        elif plan.internal_squads:
            subscription.internal_squads = list(plan.internal_squads)
        # Carry the new plan's external squad too — otherwise a plan change moving the user to a
        # different exit silently keeps the old one (build_spec reads the sub, not the plan).
        if req.external_squad is not None:
            subscription.external_squad = req.external_squad
        elif plan.external_squad is not None:
            subscription.external_squad = plan.external_squad
        subscription.expire_at = dt.datetime.now(dt.UTC) + dt.timedelta(
            days=req.duration_days + bonus
        )
        spec = self._remnawave.build_spec(
            short_id=subscription.short_id,
            telegram_id=user.telegram_id,
            expire_at=subscription.expire_at,
            traffic_limit_bytes=subscription.traffic_limit_bytes,
            device_limit=subscription.device_limit,
            internal_squads=tuple(subscription.internal_squads or ()),
            external_squad=subscription.external_squad,
        )
        # On a plan CHANGE the new plan's device cap / exit are authoritative: if it grants
        # unlimited devices or has no external squad, the old panel value must be CLEARED, not
        # left in place. create/renew keep the omit-semantics — only CHANGE opts into the clear.
        spec = replace(spec, reset_device_limit=True, reset_external_squad=True)
        await self._remnawave.apply(panel_ref, spec)
        return subscription

    async def push_limits(
        self, uow: UnitOfWork, subscription: Subscription, *, telegram_id: int | None = None
    ) -> Subscription:
        """Push the subscription's current limits to the panel without touching expiry."""
        panel_ref = subscription.panel_ref
        if panel_ref is None:
            raise PurchaseError("subscription has no panel user")
        spec = self._remnawave.build_spec(
            short_id=subscription.short_id,
            telegram_id=telegram_id,
            expire_at=subscription.expire_at or dt.datetime.now(dt.UTC),
            traffic_limit_bytes=subscription.traffic_limit_bytes,
            device_limit=subscription.device_limit,
            internal_squads=tuple(subscription.internal_squads or ()),
            external_squad=subscription.external_squad,
        )
        await self._remnawave.apply(panel_ref, spec)
        return subscription

    async def enter_grace(
        self,
        uow: UnitOfWork,
        subscription: Subscription,
        *,
        days: int,
        traffic_gb: int,
        telegram_id: int | None = None,
    ) -> Subscription:
        """Put a just-expired PAID sub into the grace window (panel-first).

        On the panel: short expiry (now + ``days``), a small traffic cap and a used-counter reset
        so the allowance is actually usable, and enabled (expiry may have disabled it). The row's
        authoritative paid limits are deliberately NOT changed — a later renew restores full
        traffic/expiry for free. ``grace_until`` marks the window; ``grace_started_at`` gates the
        cooldown.
        """
        panel_ref = subscription.panel_ref
        if panel_ref is None:
            raise PurchaseError("cannot grace a subscription with no panel user")
        now = dt.datetime.now(dt.UTC)
        grace_until = now + dt.timedelta(days=days)
        spec = self._remnawave.build_spec(
            short_id=subscription.short_id,
            telegram_id=telegram_id,
            expire_at=grace_until,
            traffic_limit_bytes=max(0, traffic_gb) * _GIB,  # 0 -> unlimited
            device_limit=subscription.device_limit,
            internal_squads=tuple(subscription.internal_squads or ()),
            external_squad=subscription.external_squad,
        )
        await self._remnawave.apply(panel_ref, spec)
        await self._remnawave.reset_traffic(panel_ref)
        await self._remnawave.enable(panel_ref)
        subscription.status = SubscriptionStatus.LIMITED
        subscription.grace_until = grace_until
        subscription.grace_started_at = now
        return subscription

    async def end_grace(self, uow: UnitOfWork, subscription: Subscription) -> Subscription:
        """Close a lapsed grace window: disable on the panel and mark EXPIRED locally.

        ``grace_started_at`` is kept so the cooldown still applies to any future expiry.
        """
        subscription.grace_until = None
        subscription.status = SubscriptionStatus.EXPIRED
        panel_ref = subscription.panel_ref
        if panel_ref is not None:
            await self._remnawave.disable(panel_ref)
        return subscription

    @staticmethod
    def apply_purchase_discount_reset(user: User, purchase_type: PurchaseType) -> None:
        """One-shot ``purchase_discount`` is consumed on ANY paid purchase (gotcha #14) —
        including a traffic top-up, whose price is also reduced by it (#9)."""
        if purchase_type in (
            PurchaseType.NEW,
            PurchaseType.RENEW,
            PurchaseType.CHANGE,
            PurchaseType.TRAFFIC_TOPUP,
        ):
            user.purchase_discount_pct = 0
