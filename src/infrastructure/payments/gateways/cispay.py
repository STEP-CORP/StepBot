"""cisPay (api.cispay.app) — card/SBP payments, HMAC-SHA256 webhook.

Create: POST /payments with the ``X-Shop-ID`` + ``X-Api-Key`` headers; ``order_id`` = our
payment_id, ``amount`` in kopecks (the provider's native unit — no rounding on our side).
Success is HTTP 201 with ``payment_url``. SBP additionally requires ``customer_id``, so we
pass a stable per-user value.
Webhook: ``X-Signature`` = HMAC-SHA256 hex over the RAW body keyed with the api key.
Statuses: PAID paid; FAILED/EXPIRED closed; REFUNDED returned.

Settings row keys: ``shop_id``, ``api_key`` (Fernet-encrypted at rest), optional
``payment_method`` (CARD by default; SBP to open the QR flow instead).
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

import httpx

from src.application.common.payments import (
    GatewayCapabilities,
    PaymentContext,
    PaymentResult,
    PaymentResultKind,
    WebhookRequest,
    WebhookResult,
)
from src.core.enums import Currency, PaymentGatewayType, TransactionStatus
from src.core.exceptions import PaymentError, WebhookVerificationError
from src.core.logging import get_logger
from src.core.money import Money
from src.infrastructure.payments.base import BasePaymentGateway

log = get_logger(__name__)

API = "https://api.cispay.app"

_PAID = {"PAID"}
_CLOSED = {"FAILED", "EXPIRED"}
_REFUNDED = {"REFUNDED"}
_METHODS = {"CARD", "SBP"}


class CispayGateway(BasePaymentGateway):
    gateway_type = PaymentGatewayType.CISPAY

    @property
    def capabilities(self) -> GatewayCapabilities:
        # RUB only, amounts already in kopecks; refunds exist in the dashboard, not via API.
        return GatewayCapabilities(currencies=frozenset({Currency.RUB}), needs_http_webhook=True)

    def _auth(self) -> dict[str, str]:
        shop_id = str(self.settings.get("shop_id") or "")
        api_key = str(self.settings.get("api_key") or "")
        if not shop_id or not api_key:
            raise PaymentError("cisPay: shop_id / api_key not configured")
        return {"X-Shop-ID": shop_id, "X-Api-Key": api_key}

    def _api_key(self) -> str:
        key = str(self.settings.get("api_key") or "")
        if not key:
            raise PaymentError("cisPay: api_key not configured")
        return key

    def _method(self) -> str:
        method = str(self.settings.get("payment_method") or "CARD").upper()
        return method if method in _METHODS else "CARD"

    async def create_payment(self, ctx: PaymentContext) -> PaymentResult:
        payload: dict[str, Any] = {
            "amount": ctx.amount.amount_minor,  # kopecks on both sides — no conversion
            "order_id": str(ctx.payment_id),
            "payment_method": self._method(),
            # Required for SBP and harmless for CARD: a stable id keeps the provider's
            # per-customer flow consistent across a user's payments.
            "customer_id": str(ctx.telegram_id or ctx.user_id),
        }
        if ctx.return_url:
            payload["redirect_success_url"] = ctx.return_url
            payload["redirect_fail_url"] = ctx.return_url
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                res = await http.post(f"{API}/payments", json=payload, headers=self._auth())
        except httpx.HTTPError as exc:
            raise PaymentError(f"cisPay: {exc}") from exc
        data = res.json() if res.status_code in (200, 201) else {}
        url = str(data.get("payment_url") or "")
        if res.status_code not in (200, 201) or not url:
            log.error("cispay create failed", status=res.status_code, body=res.text[:300])
            raise PaymentError(f"cisPay error {res.status_code}")
        return PaymentResult(
            kind=PaymentResultKind.REDIRECT,
            external_id=str(data.get("id") or ""),
            redirect_url=url,
        )

    def _map(self, body: dict[str, Any]) -> WebhookResult:
        status_raw = str(body.get("status") or "").upper()
        if status_raw in _PAID:
            status = TransactionStatus.COMPLETED
        elif status_raw in _REFUNDED:
            # The payment pipeline acts on COMPLETED/CANCELED/FAILED only, so a provider-side
            # refund is not auto-applied here (refunds are admin-driven in the cabinet). Log it
            # loudly so the operator can revoke access instead of finding out from the balance.
            log.warning(
                "cispay refund notification — revoke access manually if needed",
                order_id=str(body.get("order_id") or ""),
                external_id=str(body.get("id") or ""),
            )
            status = TransactionStatus.REFUNDED
        elif status_raw in _CLOSED:
            status = TransactionStatus.CANCELED
        else:
            status = TransactionStatus.PENDING
        payment_id = None
        with contextlib.suppress(ValueError):
            payment_id = UUID(str(body.get("order_id") or ""))
        amount = None
        with contextlib.suppress(KeyError, TypeError, ValueError):
            # `amount` is what the buyer owes; `charged_amount`/`merchant_revenue` are the
            # provider's own accounting and must NOT be used for the paid-enough check.
            # A status-only payload (no amount) is fine — the cross-check just gets skipped.
            amount = Money(int(body["amount"]), Currency.RUB)
        return WebhookResult(
            status=status,
            payment_id=payment_id,
            external_id=str(body.get("id") or "") or None,
            amount=amount,
        )

    async def handle_webhook(self, request: WebhookRequest) -> WebhookResult:
        headers = {k.lower(): v for k, v in request.headers.items()}
        signature = str(headers.get("x-signature") or "")
        if not signature:
            raise WebhookVerificationError("cisPay: missing X-Signature")
        # HMAC over the RAW body — verify_hmac raises on mismatch (and on an empty secret,
        # since _api_key() already refuses to build one).
        self.verify_hmac(request.body, signature.lower(), self._api_key())
        return self._map(self.parse_json(request.body))

    async def fetch_status(self, external_id: str) -> WebhookResult | None:
        """Reconcile path: ask the provider directly when the webhook never arrived."""
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                res = await http.get(
                    f"{API}/payments/status", params={"id": external_id}, headers=self._auth()
                )
        except httpx.HTTPError:
            return None
        if res.status_code != 200:
            return None
        result = self._map(res.json())
        return result if result.status is not TransactionStatus.PENDING else None
