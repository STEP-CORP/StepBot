"""Admin-managed Remnawave connection settings.

The first installation still gets its connection profile from ``.env``.  Once an owner
saves a value in the cabinet, the database override wins and is shared by every process.
Secret fields use the same Fernet box as payment credentials, so the panel token never
needs to be stored in the repository or returned to the browser.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.core.config.remnawave import PanelAuthType, RemnawaveSettings
from src.core.exceptions import ConfigError

if TYPE_CHECKING:
    from src.infrastructure.database.uow import UnitOfWork
    from src.infrastructure.payments.crypto import SecretBox


class RemnawaveConfigError(ConfigError):
    """The owner supplied an invalid panel connection value."""


_PREFIX = "REMNAWAVE__"
_FIELDS = (
    "base_url",
    "auth_type",
    "token",
    "basic_user",
    "basic_password",
    "caddy_api_key",
    "cf_access_client_id",
    "cf_access_client_secret",
    "secret_key_cookie",
    "webhook_secret",
    "force_local",
)
_SECRET_FIELDS = frozenset(
    {
        "token",
        "basic_password",
        "caddy_api_key",
        "cf_access_client_id",
        "cf_access_client_secret",
        "secret_key_cookie",
        "webhook_secret",
    }
)
_MASK = "••••••••"


def _key(field: str) -> str:
    return f"{_PREFIX}{field.upper()}"


class RemnawaveConfigService:
    """Resolve env defaults plus encrypted database overrides.

    A short process-local cache avoids an extra SQL query for every panel request while
    still making changes visible to the bot/worker processes within a few seconds.
    """

    CACHE_TTL = 5.0

    def __init__(self, secret_box: SecretBox | None, env: RemnawaveSettings) -> None:
        self._box = secret_box
        self._env = env
        self._cache: RemnawaveSettings | None = None
        self._cache_at = 0.0

    def invalidate(self) -> None:
        self._cache = None
        self._cache_at = 0.0

    async def effective(self, uow: UnitOfWork) -> RemnawaveSettings:
        if self._cache is not None and time.monotonic() - self._cache_at < self.CACHE_TTL:
            return self._cache.model_copy(deep=True)

        stored = await uow.bot_config.as_dict()
        values: dict[str, Any] = {}
        for field in _FIELDS:
            db_key = _key(field)
            value = stored.get(db_key, getattr(self._env, field))
            if field in _SECRET_FIELDS and db_key in stored and value and self._box is not None:
                try:
                    value = self._box.decrypt(str(value))
                except ConfigError:
                    # A rotated/missing APP__CRYPT_KEY must not make the whole app start with
                    # an unusable profile. The env value remains the safe fallback.
                    value = getattr(self._env, field)
            values[field] = "" if value is None else str(value)

        try:
            settings = RemnawaveSettings(**values)
        except ValueError as exc:
            raise RemnawaveConfigError(f"invalid saved Remnawave settings: {exc}") from exc
        self._cache = settings
        self._cache_at = time.monotonic()
        return settings.model_copy(deep=True)

    async def listing(self, uow: UnitOfWork) -> dict[str, Any]:
        settings = await self.effective(uow)
        return {
            "base_url": settings.base_url,
            "auth_type": settings.auth_type.value,
            "basic_user": settings.basic_user,
            "force_local": settings.force_local or "auto",
            "token_set": bool(settings.token),
            "basic_password_set": bool(settings.basic_password),
            "caddy_api_key_set": bool(settings.caddy_api_key),
            "cf_access_client_id_set": bool(settings.cf_access_client_id),
            "cf_access_client_secret_set": bool(settings.cf_access_client_secret),
            "secret_key_cookie_set": bool(settings.secret_key_cookie),
            "webhook_secret_set": bool(settings.webhook_secret),
        }

    async def update(self, uow: UnitOfWork, changes: dict[str, Any]) -> list[str]:
        unknown = sorted(set(changes) - set(_FIELDS))
        if unknown:
            raise RemnawaveConfigError(f"unknown Remnawave setting: {unknown[0]}")

        written: list[str] = []
        for field, raw in changes.items():
            if raw is None:
                continue
            value = str(raw).strip()
            if field == "base_url":
                value = value.rstrip("/")
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise RemnawaveConfigError("base_url must be a full http(s) URL")
            elif field == "auth_type":
                try:
                    value = PanelAuthType(value).value
                except ValueError as exc:
                    allowed = ", ".join(item.value for item in PanelAuthType)
                    raise RemnawaveConfigError(f"auth_type must be one of: {allowed}") from exc
            elif field == "force_local":
                value = {"auto": "", "": "", "true": "true", "false": "false"}.get(
                    value.lower(), "__invalid__"
                )
                if value == "__invalid__":
                    raise RemnawaveConfigError("force_local must be auto, true, or false")

            stored_value: object = value
            if field in _SECRET_FIELDS and value and self._box is not None:
                stored_value = self._box.encrypt(value)
            await uow.bot_config.upsert(_key(field), stored_value)
            written.append(field)

        self.invalidate()
        return written

    async def reset(self, uow: UnitOfWork) -> None:
        for field in _FIELDS:
            await uow.bot_config.delete_by(key=_key(field))
        self.invalidate()


__all__ = ["RemnawaveConfigError", "RemnawaveConfigService"]
