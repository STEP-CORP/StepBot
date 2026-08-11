"""Inbound Remnawave webhook verification + parsing (docs/context/01)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from src.core.exceptions import WebhookVerificationError


@dataclass(frozen=True, slots=True)
class PanelEvent:
    event: str  # e.g. "user.created", "node.down", "torrent_blocker.report"
    payload: dict[str, Any]

    @property
    def is_imported(self) -> bool:
        """user.created fires for users we didn't create — act only on IMPORTED (gotcha #19)."""
        return str(self.payload.get("tag") or "").upper() == "IMPORTED"


class WebhookVerifier:
    """HMAC-SHA256 verifier for panel webhooks. Header name is panel-configurable."""

    def __init__(self, secret: str, *, signature_header: str = "x-remnawave-signature") -> None:
        self.set_secret(secret)
        self._header = signature_header.lower()

    def set_secret(self, secret: str) -> None:
        """Replace the shared secret after an owner changes the panel settings."""
        self._secret = secret.encode()

    def _expected(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def verify(self, body: bytes, headers: dict[str, str]) -> None:
        # Fail closed on an empty secret regardless of env: an empty-key HMAC is attacker-
        # computable, so accepting it would let a forged /webhook/panel mutate subscription
        # state (delete/disable/expire) for any known uuid. (Prod already refuses to boot
        # without the secret; this protects a careless non-prod deploy too.)
        if not self._secret:
            raise WebhookVerificationError("panel webhook secret is not configured")
        provided = ""
        for key, value in headers.items():
            if key.lower() == self._header:
                provided = value
                break
        try:
            ok = bool(provided) and hmac.compare_digest(self._expected(body), provided)
        except TypeError:
            # non-ASCII signature header -> treat as mismatch (403), not an uncaught 500
            ok = False
        if not ok:
            raise WebhookVerificationError("panel webhook signature mismatch")

    def parse(self, body: bytes) -> PanelEvent:
        data = json.loads(body.decode() or "{}")
        return PanelEvent(
            event=str(data.get("event") or data.get("eventName") or ""),
            payload=dict(data.get("data") or data.get("payload") or data),
        )
