"""Promocode reward_type='group': create-time validation, activation grants membership,
and existing broken rows (promo_group_id left NULL) fail with a message the buyer can
actually read, while the operator gets the technical detail in the log (docs/context/04).

Admin route handlers open their own ``async with container.uow()`` block, so calls to
them must happen *outside* any block this test already has open on the same UnitOfWork
(it isn't reentrant) — set up fixtures, close the block, call the route, then reopen a
fresh block to inspect the result.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.application.dto.pricing import PurchaseRequest
from src.application.services.pricing import PricingService
from src.application.services.promo import PromoError, PromoService
from src.bot.handlers.start import _attribute
from src.core.enums import Currency, RewardType, Role
from src.infrastructure.database.models.campaign import Campaign
from src.infrastructure.database.models.promo_group import PromoGroup, UserPromoGroup
from src.infrastructure.database.models.promocode import Promocode, PromocodeActivation
from src.infrastructure.database.uow import UnitOfWork
from src.web.routes.admin.campaigns import (
    CampaignIn,
    CampaignPatch,
    create_campaign,
    patch_campaign,
)
from src.web.routes.admin.deps import AdminIdentity
from src.web.routes.admin.promos import (
    BulkIn,
    PromoIn,
    PromoPatch,
    bulk_promocodes,
    create_promocode,
    delete_promogroup,
    list_promocodes,
    patch_promocode,
)
from tests.factories import make_plan, make_user


def _identity() -> AdminIdentity:
    return AdminIdentity(user_id=1, username="admin", role=Role.OWNER)


async def _no_bot_username(_uow: object, _key: str) -> str:
    return ""


def _container(uow: UnitOfWork) -> SimpleNamespace:
    """Bare AppContainer stand-in — the admin routes only reach for .uow()/.bot_config."""
    return SimpleNamespace(uow=lambda: uow, bot_config=SimpleNamespace(value=_no_bot_username))


async def _make_group(uow: UnitOfWork, *, server_discount_pct: int = 30) -> int:
    """Seed a PromoGroup and hand back its id (row is committed by the caller)."""
    group = PromoGroup(name="vip", priority=10, server_discount_pct=server_discount_pct)
    uow.session.add(group)
    await uow.flush()
    return group.id


# --- creation: single --------------------------------------------------------------------


async def test_create_group_promo_requires_bound_group(uow: UnitOfWork) -> None:
    body = PromoIn(code="NOGROUP", reward_type="group", reward_value=0)
    with pytest.raises(HTTPException) as exc_info:
        await create_promocode(body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


async def test_create_group_promo_rejects_unknown_group(uow: UnitOfWork) -> None:
    body = PromoIn(code="GHOST", reward_type="group", reward_value=0, promo_group_id=999)
    with pytest.raises(HTTPException) as exc_info:
        await create_promocode(body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


async def test_create_group_promo_with_valid_group_succeeds(uow: UnitOfWork) -> None:
    async with uow:
        group_id = await _make_group(uow)
        await uow.commit()

    body = PromoIn(code="VIPCODE", reward_type="group", reward_value=0, promo_group_id=group_id)
    out = await create_promocode(body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert out["ok"] is True

    async with uow:
        promo = await uow.promocodes.get(out["id"])
        assert promo is not None
        assert promo.promo_group_id == group_id


async def test_other_reward_types_ignore_stray_promo_group_id(uow: UnitOfWork) -> None:
    """A promo_group_id sent for a non-group reward must never leak into the row —
    it simply doesn't apply, and mustn't fail the request either."""
    body = PromoIn(code="CASHBACK", reward_type="balance", reward_value=500, promo_group_id=42)
    out = await create_promocode(body, _identity(), _container(uow))  # type: ignore[arg-type]

    async with uow:
        promo = await uow.promocodes.get(out["id"])
        assert promo is not None
        assert promo.promo_group_id is None


# --- activation: membership + pricing effect ----------------------------------------------


async def test_group_promo_activation_grants_membership_and_discount(uow: UnitOfWork) -> None:
    svc = PromoService()
    async with uow:
        group_id = await _make_group(uow, server_discount_pct=25)
        user = await make_user(uow)
        plan, _ = await make_plan(uow, price_minor=20000, days=30)
        uow.session.add(
            Promocode(code="VIP25", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        )
        await uow.commit()

        reward = await svc.apply(uow, user, "VIP25")
        assert reward is RewardType.PROMO_GROUP
        await uow.commit()

        # fact check: a membership row actually exists, not just "no exception raised"
        member = await uow.session.scalar(
            select(UserPromoGroup).where(
                UserPromoGroup.user_id == user.id, UserPromoGroup.promo_group_id == group_id
            )
        )
        assert member is not None

        req = PurchaseRequest(
            user_id=user.id, plan_id=plan.id, duration_days=30, currency=Currency.RUB
        )
        quote = await PricingService().quote(uow, req)
    assert quote.discount_pct == 25
    assert quote.final.amount_minor == 15000


# --- the actual bug: broken existing rows get a human-readable message --------------------


async def test_broken_group_promo_activation_gives_friendly_message(uow: UnitOfWork) -> None:
    """Simulates data left over from before create-time validation existed: reward_type
    is 'group' but promo_group_id is NULL. Must not surface the raw English exception
    ('unsupported wallet reward') to the buyer."""
    svc = PromoService()
    async with uow:
        user = await make_user(uow)
        uow.session.add(
            Promocode(code="BROKEN", reward_type=RewardType.PROMO_GROUP, promo_group_id=None)
        )
        await uow.commit()

        with pytest.raises(PromoError) as exc_info:
            await svc.apply(uow, user, "BROKEN")
    # the exact copy the buyer sees — not just "isn't the old English string"; any other
    # technical text would also pass a mere "not in" check
    assert str(exc_info.value) == "промокод временно не работает, мы уже разбираемся"


# --- editing: can't patch a working code into the broken state, can patch it out ----------


async def test_patch_cannot_unbind_group_promo(uow: UnitOfWork) -> None:
    async with uow:
        group_id = await _make_group(uow)
        promo = Promocode(code="VIPX", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        uow.session.add(promo)
        await uow.commit()
        promo_id = promo.id

    body = PromoPatch(promo_group_id=None)
    with pytest.raises(HTTPException) as exc_info:
        await patch_promocode(promo_id, body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


async def test_patch_group_id_rejected_for_non_group_reward(uow: UnitOfWork) -> None:
    async with uow:
        group_id = await _make_group(uow)
        promo = Promocode(code="BAL10", reward_type=RewardType.BALANCE, reward_value=1000)
        uow.session.add(promo)
        await uow.commit()
        promo_id = promo.id

    body = PromoPatch(promo_group_id=group_id)
    with pytest.raises(HTTPException) as exc_info:
        await patch_promocode(promo_id, body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


async def test_patch_rebinds_broken_group_promo(uow: UnitOfWork) -> None:
    """The operator-facing repair path: an already-broken group code can be fixed in
    place by binding it to an existing group, instead of deleting/recreating it."""
    svc = PromoService()
    async with uow:
        group_id = await _make_group(uow)
        promo = Promocode(code="FIXME", reward_type=RewardType.PROMO_GROUP, promo_group_id=None)
        uow.session.add(promo)
        user = await make_user(uow)
        await uow.commit()
        promo_id = promo.id
        user_id = user.id

    body = PromoPatch(promo_group_id=group_id)
    await patch_promocode(promo_id, body, _identity(), _container(uow))  # type: ignore[arg-type]

    async with uow:
        user = await uow.users.get(user_id)
        assert user is not None
        # fact check: activation now actually works end to end
        reward = await svc.apply(uow, user, "FIXME")
        assert reward is RewardType.PROMO_GROUP


async def test_list_promocodes_flags_broken_group_row(uow: UnitOfWork) -> None:
    async with uow:
        uow.session.add(
            Promocode(code="BROKEN2", reward_type=RewardType.PROMO_GROUP, promo_group_id=None)
        )
        group_id = await _make_group(uow)
        uow.session.add(
            Promocode(code="OK2", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        )
        await uow.commit()

    out = await list_promocodes(_container(uow))  # type: ignore[arg-type]
    by_code = {row["code"]: row for row in out["items"]}
    assert by_code["BROKEN2"]["broken"] is True
    assert by_code["OK2"]["broken"] is False


# --- bulk generation: same rule applies -----------------------------------------------------


async def test_bulk_group_requires_bound_group(uow: UnitOfWork) -> None:
    body = BulkIn(count=3, reward_type="group", reward_value=0)
    with pytest.raises(HTTPException) as exc_info:
        await bulk_promocodes(body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


async def test_bulk_group_binds_every_generated_code(uow: UnitOfWork) -> None:
    async with uow:
        group_id = await _make_group(uow)
        await uow.commit()

    body = BulkIn(count=3, reward_type="group", reward_value=0, promo_group_id=group_id)
    out = await bulk_promocodes(body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert out["count"] == 3

    async with uow:
        for item in out["items"]:
            promo = await uow.promocodes.find_one(code=item["code"])
            assert promo is not None
            assert promo.promo_group_id == group_id


# --- B1: a second way into an already-joined group must not corrupt the DB ----------------


async def test_second_code_for_same_group_is_idempotent(uow: UnitOfWork) -> None:
    """Bulk generation hands out several codes for one group by design (see its
    docstring) — a user who redeems two different codes bound to the same group must
    not hit the ``user_promo_groups`` composite-PK collision. The second activation is
    a no-op on membership but still records its own activation (each code is its own
    grant, redeemable/trackable independently)."""
    svc = PromoService()
    async with uow:
        group_id = await _make_group(uow)
        user = await make_user(uow)
        uow.session.add(
            Promocode(code="FIRST", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        )
        uow.session.add(
            Promocode(code="SECOND", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        )
        await uow.commit()

        reward1 = await svc.apply(uow, user, "FIRST")
        assert reward1 is RewardType.PROMO_GROUP
        await uow.commit()

        # must not raise IntegrityError on the (user_id, promo_group_id) composite PK
        reward2 = await svc.apply(uow, user, "SECOND")
        assert reward2 is RewardType.PROMO_GROUP
        await uow.commit()

        members = (
            (
                await uow.session.execute(
                    select(UserPromoGroup).where(
                        UserPromoGroup.user_id == user.id, UserPromoGroup.promo_group_id == group_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 1  # exactly one membership row, not two

        activations = (
            (
                await uow.session.execute(
                    select(PromocodeActivation).where(PromocodeActivation.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(activations) == 2  # both codes still get their own activation record


async def test_group_promo_after_campaign_attribution_is_idempotent(uow: UnitOfWork) -> None:
    """The other real path into a group: a campaign deep-link attributes the user on
    first ``/start`` (bot/handlers/start.py::_attribute) *before* they ever type a
    promo code. Redeeming a group-reward code for that same group afterwards must
    succeed instead of hitting the composite-PK collision."""
    svc = PromoService()
    async with uow:
        group_id = await _make_group(uow, server_discount_pct=15)
        user = await make_user(uow)
        uow.session.add(
            Campaign(name="ads", start_param="promo1", promo_group_id=group_id, is_active=True)
        )
        uow.session.add(
            Promocode(code="VIPCAMP", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        )
        await uow.commit()
        user_id = user.id

    # simulates the bot's /start?start=promo1 handling — attributes the campaign and
    # (per start.py) grants its bound group right there
    await _attribute(_container(uow), SimpleNamespace(id=user_id), "promo1", created=True)  # type: ignore[arg-type]

    async with uow:
        member = await uow.session.scalar(
            select(UserPromoGroup).where(
                UserPromoGroup.user_id == user_id, UserPromoGroup.promo_group_id == group_id
            )
        )
        assert member is not None  # campaign attribution already granted the group

        user = await uow.users.get(user_id)
        assert user is not None
        reward = await svc.apply(uow, user, "VIPCAMP")  # must not raise
        assert reward is RewardType.PROMO_GROUP
        await uow.commit()

        members = (
            (
                await uow.session.execute(
                    select(UserPromoGroup).where(
                        UserPromoGroup.user_id == user_id, UserPromoGroup.promo_group_id == group_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(members) == 1  # still exactly one row


# --- S2: deleting a group must not leave live promocodes/campaigns pointing at it ---------


async def test_delete_promogroup_blocked_by_active_promocode(uow: UnitOfWork) -> None:
    async with uow:
        group_id = await _make_group(uow)
        uow.session.add(
            Promocode(code="VIPLIVE", reward_type=RewardType.PROMO_GROUP, promo_group_id=group_id)
        )
        await uow.commit()

    with pytest.raises(HTTPException) as exc_info:
        await delete_promogroup(group_id, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 409
    assert "1 active promocode" in str(exc_info.value.detail)

    async with uow:
        # refused, not silently no-op'd: the group is still there
        assert await uow.promo_groups.get(group_id) is not None


async def test_delete_promogroup_blocked_by_active_campaign(uow: UnitOfWork) -> None:
    async with uow:
        group_id = await _make_group(uow)
        uow.session.add(
            Campaign(name="ads", start_param="p2", promo_group_id=group_id, is_active=True)
        )
        await uow.commit()

    with pytest.raises(HTTPException) as exc_info:
        await delete_promogroup(group_id, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 409
    assert "1 active campaign" in str(exc_info.value.detail)


async def test_delete_promogroup_ignores_inactive_promocode(uow: UnitOfWork) -> None:
    """A disabled group-reward code left bound to the group is no longer "live" — it
    can't be redeemed, so it isn't dangerous to leave ``promo_group_id`` NULL'd out
    from under it. Deletion must not be blocked by dead weight."""
    async with uow:
        group_id = await _make_group(uow)
        uow.session.add(
            Promocode(
                code="OLDCODE",
                reward_type=RewardType.PROMO_GROUP,
                promo_group_id=group_id,
                is_active=False,
            )
        )
        await uow.commit()

    out = await delete_promogroup(group_id, _identity(), _container(uow))  # type: ignore[arg-type]
    assert out.ok is True

    async with uow:
        assert await uow.promo_groups.get(group_id) is None


async def test_delete_promogroup_cascades_and_nulls_refs_with_fk_enforced(
    uow_fk: UnitOfWork,
) -> None:
    """Same delete, but against a DB with real FK enforcement on (sqlite ignores
    ondelete= otherwise — see conftest.uow_fk) — proves the ON DELETE SET NULL on
    promocodes/campaigns and ON DELETE CASCADE on memberships that Postgres enforces
    in production actually do what the model declares, for the one path (no *active*
    references) where the app lets the delete through."""
    async with uow_fk:
        group_id = await _make_group(uow_fk)
        user = await make_user(uow_fk)
        promo = Promocode(
            code="DEADCODE",
            reward_type=RewardType.PROMO_GROUP,
            promo_group_id=group_id,
            is_active=False,
        )
        uow_fk.session.add(promo)
        uow_fk.session.add(
            Campaign(name="old", start_param="oldp", promo_group_id=group_id, is_active=False)
        )
        uow_fk.session.add(UserPromoGroup(user_id=user.id, promo_group_id=group_id))
        await uow_fk.commit()
        promo_id = promo.id
        user_id = user.id

    out = await delete_promogroup(group_id, _identity(), _container(uow_fk))  # type: ignore[arg-type]
    assert out.ok is True

    async with uow_fk:
        assert await uow_fk.promo_groups.get(group_id) is None

        promo = await uow_fk.promocodes.get(promo_id)
        assert promo is not None
        assert promo.promo_group_id is None  # SET NULL, the code itself survives

        campaign = await uow_fk.campaigns.find_one(start_param="oldp")
        assert campaign is not None
        assert campaign.promo_group_id is None  # SET NULL here too

        # CASCADE: the membership row is gone, not left dangling on a deleted group
        member = await uow_fk.session.scalar(
            select(UserPromoGroup).where(UserPromoGroup.user_id == user_id)
        )
        assert member is None


# --- MELKOE: campaigns must validate promo_group_id like promocodes do --------------------


async def test_create_campaign_rejects_unknown_group(uow: UnitOfWork) -> None:
    body = CampaignIn(name="ads", start_param="ghost1", promo_group_id=999)
    with pytest.raises(HTTPException) as exc_info:
        await create_campaign(body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400


async def test_patch_campaign_rejects_unknown_group(uow: UnitOfWork) -> None:
    async with uow:
        uow.session.add(Campaign(name="ads", start_param="realp"))
        await uow.commit()
        campaign_id = (await uow.campaigns.find_one(start_param="realp")).id

    body = CampaignPatch(promo_group_id=999)
    with pytest.raises(HTTPException) as exc_info:
        await patch_campaign(campaign_id, body, _identity(), _container(uow))  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400
