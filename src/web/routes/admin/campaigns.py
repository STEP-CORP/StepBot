"""Admin: ad campaigns with deep-link attribution (screen 09)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from src.core.enums import TransactionStatus, TransactionType
from src.infrastructure.database.models.campaign import Campaign
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import OkOut, audit, iso
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/campaigns")

_PARAM_RE = re.compile(r"^[a-zA-Z0-9_\-]{2,64}$")


async def _metrics(uow: Any, campaign_ids: list[int]) -> dict[int, dict[str, int]]:
    """regs / trials / paid users / revenue per campaign."""
    out: dict[int, dict[str, int]] = {
        cid: {"regs": 0, "trials": 0, "paid": 0, "revenue_minor": 0} for cid in campaign_ids
    }
    if not campaign_ids:
        return out

    regs = (
        await uow.session.execute(
            select(User.campaign_id, func.count())
            .where(User.campaign_id.in_(campaign_ids))
            .group_by(User.campaign_id)
        )
    ).all()
    for cid, n in regs:
        out[cid]["regs"] = n

    trials = (
        await uow.session.execute(
            select(User.campaign_id, func.count())
            .select_from(Subscription)
            .join(User, User.id == Subscription.user_id)
            .where(User.campaign_id.in_(campaign_ids), Subscription.is_trial.is_(True))
            .group_by(User.campaign_id)
        )
    ).all()
    for cid, n in trials:
        out[cid]["trials"] = n

    fin = (
        await uow.session.execute(
            select(
                User.campaign_id,
                func.count(func.distinct(Transaction.user_id)),
                func.coalesce(func.sum(Transaction.amount_minor), 0),
            )
            .select_from(Transaction)
            .join(User, User.id == Transaction.user_id)
            .where(
                User.campaign_id.in_(campaign_ids),
                Transaction.status == TransactionStatus.COMPLETED,
                # External money only — a balance-funded SUBSCRIPTION_PAYMENT (gateway_type
                # NULL) is an internal transfer already counted as the DEPOSIT that funded it.
                # Summing both inflated campaign revenue/ROI/avg-check (same fix as the
                # dashboard's _EXTERNAL_MONEY).
                or_(
                    Transaction.type == TransactionType.DEPOSIT,
                    (Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT)
                    & Transaction.gateway_type.is_not(None),
                ),
            )
            .group_by(User.campaign_id)
        )
    ).all()
    for cid, paid_users, revenue in fin:
        out[cid]["paid"] = paid_users
        out[cid]["revenue_minor"] = int(revenue)
    return out


@router.get("")
async def list_campaigns(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        campaigns = list(await uow.campaigns.list())
        metrics = await _metrics(uow, [c.id for c in campaigns])
        bot_username = await container.bot_config.value(uow, "BOT_USERNAME")
    items = []
    for c in sorted(campaigns, key=lambda c: c.id, reverse=True):
        m = metrics[c.id]
        regs, paid, revenue, cost = m["regs"], m["paid"], m["revenue_minor"], c.cost_minor
        items.append(
            {
                "id": c.id,
                "name": c.name,
                "start_param": c.start_param,
                "link": (
                    f"https://t.me/{bot_username}?start={c.start_param}" if bot_username else None
                ),
                "is_active": c.is_active,
                "created_at": iso(c.created_at),
                "cost_minor": cost,
                **m,
                "cr_pct": round(paid * 100 / regs, 1) if regs else 0.0,
                "cpa_minor": round(cost / paid) if paid else None,
                "roi_pct": round((revenue - cost) * 100 / cost) if cost else None,
                "avg_check_minor": round(revenue / paid) if paid else 0,
            }
        )
    return {"items": items}


async def _check_group_exists(uow: Any, promo_group_id: int | None) -> None:
    """A campaign's promo_group_id is optional, but an id that doesn't exist would hit
    the FK on insert/update (ondelete="SET NULL" doesn't help an id that was never valid)
    and 500 instead of a legible refusal — same class of bug just fixed for promocodes,
    see ``_resolve_group_binding`` in ``routes/admin/promos.py``."""
    if promo_group_id is not None and await uow.promo_groups.get(promo_group_id) is None:
        raise HTTPException(400, "promo group not found")


class CampaignIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    start_param: str = Field(..., min_length=2, max_length=64)
    promo_group_id: int | None = None
    cost_minor: int = Field(0, ge=0)


@router.post("")
async def create_campaign(
    body: CampaignIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    if not _PARAM_RE.match(body.start_param):
        raise HTTPException(400, "start_param: only latin letters, digits, _ and -")
    async with container.uow() as uow:
        if await uow.campaigns.find_one(start_param=body.start_param):
            raise HTTPException(409, "start_param already in use")
        await _check_group_exists(uow, body.promo_group_id)
        campaign = Campaign(
            name=body.name,
            start_param=body.start_param,
            promo_group_id=body.promo_group_id,
            cost_minor=body.cost_minor,
        )
        await uow.campaigns.add(campaign)
        await audit(uow, identity, "campaign.create", f"campaign:{body.start_param}")
        await uow.commit()
        return {"ok": True, "id": campaign.id}


class CampaignPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    is_active: bool | None = None
    cost_minor: int | None = Field(None, ge=0)
    promo_group_id: int | None = None


@router.patch("/{campaign_id}")
async def patch_campaign(
    campaign_id: int,
    body: CampaignPatch,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    data = body.model_dump(exclude_unset=True)
    async with container.uow() as uow:
        campaign = await uow.campaigns.get(campaign_id)
        if campaign is None:
            raise HTTPException(404, "campaign not found")
        if "promo_group_id" in data:
            await _check_group_exists(uow, data["promo_group_id"])
        for k, v in data.items():
            setattr(campaign, k, v)
        await audit(uow, identity, "campaign.patch", f"campaign:{campaign.start_param}", **data)
        await uow.commit()
    return OkOut()


@router.delete("/{campaign_id}")
async def delete_campaign(
    campaign_id: int,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OkOut:
    async with container.uow() as uow:
        campaign = await uow.campaigns.get(campaign_id)
        if campaign is None:
            raise HTTPException(404, "campaign not found")
        await uow.campaigns.delete(campaign)
        await audit(uow, identity, "campaign.delete", f"campaign:{campaign.start_param}")
        await uow.commit()
    return OkOut()
