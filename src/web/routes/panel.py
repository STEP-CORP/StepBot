"""Inbound Remnawave panel webhook (HMAC-verified): apply user events to local state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from src.application.dto.panel import PanelUser
from src.core.constants import PANEL_USERNAME_PREFIX
from src.core.enums import SubscriptionStatus
from src.core.exceptions import WebhookVerificationError
from src.core.logging import get_logger
from src.infrastructure.database.base import utcnow
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.di import AppContainer
from src.infrastructure.remnawave.client import _to_panel_user
from src.web.deps import get_container

router = APIRouter(prefix="/webhook", tags=["panel"])
log = get_logger(__name__)

# Panel event -> local subscription status. ``user.updated`` derives status from the payload.
_EVENT_STATUS: dict[str, SubscriptionStatus] = {
    "user.enabled": SubscriptionStatus.ACTIVE,
    "user.disabled": SubscriptionStatus.DISABLED,
    "user.expired": SubscriptionStatus.EXPIRED,
    "user.limited": SubscriptionStatus.LIMITED,
    "user.deleted": SubscriptionStatus.DELETED,
}


@router.post("/panel")
async def panel_webhook(
    request: Request, container: AppContainer = Depends(get_container)
) -> dict[str, Any]:
    body = await request.body()
    # The owner can rotate the webhook secret from the cabinet. Refresh the verifier before
    # checking the signature so a bot/web process does not need a restart to accept the new key.
    async with container.uow() as uow:
        panel_settings = await container.remnawave_config.effective(uow)
    container.panel_webhook.set_secret(panel_settings.webhook_secret)
    try:
        container.panel_webhook.verify(body, dict(request.headers))
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    event = container.panel_webhook.parse(body)
    # user.created fires for users we didn't create — ignore unless IMPORTED (gotcha #19).
    if event.event == "user.created" and not event.is_imported:
        return {"accepted": True, "ignored": True}

    applied = False
    # 2.x payloads carry the user uuid; 3.0 payloads a numeric id — either addresses a user.
    has_user_key = event.payload.get("uuid") or event.payload.get("id") is not None
    if event.event.startswith("user.") and has_user_key:
        applied = await _apply_user_event(container, event.event, event.payload)
    log.info("panel_event", event_name=event.event, applied=applied)
    return {"accepted": True, "applied": applied}


async def resolve_subscription(uow: UnitOfWork, panel_user: PanelUser) -> Subscription | None:
    """Find the local subscription a panel webhook is talking about.

    2.x events resolve by uuid — the historical hot path. 3.0 events have no uuid:
    try the numeric id first (present once backfilled), then fall back to the keys a
    pre-3.0 subscription can still be recognized by — our username template
    ``sub_<short_id>`` or a short_id adopted from the panel shortUuid on import. The
    fallbacks run only for uuid-less payloads so 2.x behaviour is untouched.
    """
    if panel_user.uuid is not None:
        return await uow.subscriptions.get_by_remnawave_uuid(panel_user.uuid)
    if panel_user.panel_id is not None:
        sub = await uow.subscriptions.get_by_remnawave_id(panel_user.panel_id)
        if sub is not None:
            return sub
    if panel_user.username.startswith(PANEL_USERNAME_PREFIX):
        sub = await uow.subscriptions.get_by_short_id(
            panel_user.username[len(PANEL_USERNAME_PREFIX) :]
        )
        if sub is not None:
            return sub
    if panel_user.short_id:
        return await uow.subscriptions.get_by_short_id(panel_user.short_id)
    return None


async def _apply_user_event(
    container: AppContainer, event_name: str, payload: dict[str, Any]
) -> bool:
    """Keep the local subscription in sync with a panel-side user change. Idempotent."""
    try:
        panel_user = _to_panel_user(payload)
    except (KeyError, ValueError, TypeError):
        return False
    async with container.uow() as uow:
        sub = await resolve_subscription(uow, panel_user)
        if sub is None:
            return False
        # First 3.0 event for this sub pins the numeric id — later lookups take the fast path.
        if panel_user.panel_id is not None and sub.remnawave_id != panel_user.panel_id:
            sub.remnawave_id = panel_user.panel_id
        status = _EVENT_STATUS.get(event_name)
        if status is not None:
            sub.status = status
        elif event_name in ("user.updated", "user.modified"):
            sub.status = (
                SubscriptionStatus.ACTIVE if panel_user.is_enabled else SubscriptionStatus.DISABLED
            )
        sub.traffic_used_bytes = panel_user.traffic_used_bytes
        if panel_user.expire_at is not None:
            sub.expire_at = panel_user.expire_at
        sub.last_webhook_at = utcnow()
        await uow.commit()
    return True
