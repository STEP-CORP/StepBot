"""YooMoney (personal wallet) — quickpay link + HTTP notification with HMAC-SHA256.

Create is a quickpay redirect URL (no API calls). The notification is form-encoded;
the `sign` field is HMAC-SHA256 over all notification parameters except `sign`,
sorted alphabetically and URL-encoded according to RFC 3986.

IMPORTANT:
- `amount` in the notification is the amount credited to the wallet AFTER fees.
- `withdraw_amount` is the amount actually paid/debited from the customer.
- The user can technically edit the sum in the form, so the pipeline must cross-check
  the webhook amount against the transaction price (PaymentService underpayment gate).
- `label` = our payment_id.

Settings row keys: `wallet`, `notification_secret` (Fernet-encrypted at rest).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl, quote, urlencode
from uuid import UUID

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

QUICKPAY = "https://yoomoney.ru/quickpay/confirm"


class YoomoneyGateway(BasePaymentGateway):
    gateway_type = PaymentGatewayType.YOOMONEY

    @property
    def capabilities(self) -> GatewayCapabilities:
        return GatewayCapabilities(
            currencies=frozenset({Currency.RUB}),
            needs_http_webhook=True,
        )

    def _creds(self) -> tuple[str, str]:
        wallet = str(self.settings.get("wallet") or "")
        secret = str(self.settings.get("notification_secret") or "")

        if not wallet or not secret:
            raise PaymentError(
                "YooMoney: wallet/notification_secret not configured"
            )

        return wallet, secret

    async def create_payment(self, ctx: PaymentContext) -> PaymentResult:
        wallet, _ = self._creds()

        params = {
            "receiver": wallet,
            # Kept as-is to preserve the existing StepBot redirect flow.
            # See note below about YooMoney's current documented POST/button flow.
            "quickpay-form": "shop",
            "paymentType": "AC",
            "sum": str(
                (
                    Decimal(ctx.amount.amount_minor) / Decimal(100)
                ).quantize(Decimal("0.01"))
            ),
            "label": str(ctx.payment_id),
            "targets": (ctx.description or "VPN subscription")[:100],
            "successURL": ctx.return_url or "https://t.me",
        }

        return PaymentResult(
            kind=PaymentResultKind.REDIRECT,
            external_id=str(ctx.payment_id),
            redirect_url=f"{QUICKPAY}?{urlencode(params)}",
        )

    async def handle_webhook(
        self,
        request: WebhookRequest,
    ) -> WebhookResult:
        _, secret = self._creds()

        # YooMoney sends:
        # Content-Type: application/x-www-form-urlencoded
        #
        # Empty values MUST be preserved because they participate
        # in HMAC signature calculation (for example sender=).
        try:
            pairs = parse_qsl(
                request.body.decode("utf-8", "strict"),
                keep_blank_values=True,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookVerificationError(
                "YooMoney: malformed form body"
            ) from exc

        if not pairs:
            raise WebhookVerificationError(
                "YooMoney: empty webhook body"
            )

        # Convenient lookup representation.
        # Signature calculation below uses the original pair list so
        # all received parameters participate in verification.
        f = dict(pairs)

        # -----------------------------------------------------------------
        # Verify current YooMoney notification signature.
        #
        # Since 18 May 2026 sha1_hash is no longer sent.
        #
        # Algorithm:
        #   1. take ALL received parameters;
        #   2. remove `sign`;
        #   3. sort by parameter name A-Z;
        #   4. URL-encode values as UTF-8 / RFC 3986;
        #   5. join as key=value&key=value;
        #   6. HMAC-SHA256 using notification_secret;
        #   7. compare lowercase HEX with `sign`.
        # -----------------------------------------------------------------

        got_sign = (f.get("sign") or "").strip().lower()

        if not got_sign:
            raise WebhookVerificationError(
                "YooMoney: sign is missing"
            )

        sign_items = sorted(
            (
                (key, value)
                for key, value in pairs
                if key != "sign"
            ),
            key=lambda item: item[0],
        )

        canonical = urlencode(
            sign_items,
            doseq=False,
            quote_via=quote,
            safe="",
            encoding="utf-8",
            errors="strict",
        )

        expected_sign = hmac.new(
            secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(
            got_sign,
            expected_sign.lower(),
        ):
            log.warning(
                "YooMoney webhook signature mismatch"
            )
            raise WebhookVerificationError(
                "YooMoney: sign mismatch"
            )

        # -----------------------------------------------------------------
        # Basic provider-level validation.
        # -----------------------------------------------------------------

        notification_type = f.get("notification_type") or ""

        if notification_type not in {
            "p2p-incoming",
            "card-incoming",
        }:
            raise WebhookVerificationError(
                "YooMoney: unexpected notification_type "
                f"{notification_type!r}"
            )

        currency = f.get("currency") or ""

        if currency != "643":
            raise WebhookVerificationError(
                f"YooMoney: unexpected currency {currency!r}"
            )

        # -----------------------------------------------------------------
        # Test notification.
        #
        # YooMoney marks test requests with test_notification=true.
        # The signature is verified above, but a test notification must
        # NEVER fulfil a real payment.
        # -----------------------------------------------------------------

        if (
            f.get("test_notification", "false")
            .strip()
            .lower()
            == "true"
        ):
            log.info(
                "YooMoney verified test notification received"
            )

            return WebhookResult(
                status=TransactionStatus.PENDING
            )

        # Protected / not-yet-accepted transfers must not fulfil an order.
        if (
            f.get("codepro", "false")
            .strip()
            .lower()
            == "true"
        ):
            return WebhookResult(
                status=TransactionStatus.PENDING
            )

        if (
            f.get("unaccepted", "false")
            .strip()
            .lower()
            == "true"
        ):
            return WebhookResult(
                status=TransactionStatus.PENDING
            )

        # -----------------------------------------------------------------
        # Resolve our internal payment.
        #
        # create_payment() puts ctx.payment_id into `label`, so a real
        # notification intended for StepBot must contain a valid UUID.
        # -----------------------------------------------------------------

        payment_id: UUID | None = None

        with contextlib.suppress(
            ValueError,
            TypeError,
            AttributeError,
        ):
            payment_id = UUID(f.get("label") or "")

        if payment_id is None:
            raise WebhookVerificationError(
                "YooMoney: invalid or missing label"
            )

        # -----------------------------------------------------------------
        # Amount.
        #
        # create_payment() sends the invoice price as QuickPay `sum`.
        # YooMoney defines `sum` as the amount debited from the sender.
        #
        # In the webhook:
        #   withdraw_amount = amount paid/debited from customer
        #   amount          = amount credited to our wallet after fee
        #
        # PaymentService performs an underpayment check against the invoice
        # price, therefore withdraw_amount is the correct value here.
        # -----------------------------------------------------------------

        withdraw_amount = f.get("withdraw_amount")

        if not withdraw_amount:
            raise WebhookVerificationError(
                "YooMoney: withdraw_amount is missing"
            )

        try:
            value = Decimal(withdraw_amount)

            if not value.is_finite() or value <= 0:
                raise ValueError("amount must be positive")

            amount_minor = int(
                (value * Decimal(100)).quantize(
                    Decimal("1")
                )
            )

            amount = Money(
                amount_minor,
                Currency.RUB,
            )

        except (ArithmeticError, ValueError) as exc:
            raise WebhookVerificationError(
                "YooMoney: invalid withdraw_amount"
            ) from exc

        # -----------------------------------------------------------------
        # Provider operation id.
        # -----------------------------------------------------------------

        external_id = (
            f.get("operation_id") or ""
        ).strip()

        if not external_id:
            raise WebhookVerificationError(
                "YooMoney: operation_id is missing"
            )

        log.info(
            "YooMoney payment verified: "
            f"payment_id={payment_id} "
            f"operation_id={external_id} "
            f"withdraw_amount={withdraw_amount}"
        )

        return WebhookResult(
            status=TransactionStatus.COMPLETED,
            payment_id=payment_id,
            external_id=external_id,
            amount=amount,
        )
