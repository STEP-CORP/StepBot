"""Importer: RemnaShop (snoups/remnashop, PostgreSQL 17) -> our schema.

Source of truth for the source format: RemnaShop's built-in backup runs
``pg_dump --format=plain`` (``db_backup_*.sql``), so we read COPY blocks; a live
DSN path exists too. Enum values arrive as UPPERCASE member names, money as
MAJOR units (Decimal inside the ``pricing`` JSONB), datetimes as UTC
timestamptz. There is no wallet — only integer "points", which we do not import.

The import is idempotent: users match by telegram_id, subscriptions by
remnawave_uuid, transactions by external_id, promocodes by code — re-running
updates instead of duplicating. Panel users are NOT touched: we adopt the
existing uuids, so subscribers keep working mid-migration.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid as uuid_mod
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from src.application.services.ids import generate_referral_code, generate_short_id
from src.application.services.pgdump import looks_like_pgdump, parse_copy_blocks
from src.application.services.plan_rebuild import rebuild_plans
from src.core.enums import (
    Availability,
    Currency,
    Locale,
    PaymentGatewayType,
    PurchaseType,
    RewardType,
    SubscriptionStatus,
    TransactionStatus,
    TransactionType,
    UserStatus,
)
from src.infrastructure.database.models.promocode import Promocode
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User

if TYPE_CHECKING:
    from pathlib import Path

    from src.application.services.referral import ReferralService
    from src.infrastructure.database.uow import UnitOfWork

SOURCE_TABLES: frozenset[str] = frozenset(
    {"users", "subscriptions", "transactions", "promocodes", "referrals"}
)

_SUB_STATUSES = frozenset({"ACTIVE", "DISABLED", "LIMITED", "EXPIRED", "DELETED"})
_PURCHASE_TYPES = frozenset({"NEW", "RENEW", "CHANGE"})
# Same-named members on both sides; the rest (VALUTIX/CRYPTOPAY/PAYMASTER/URLPAY) have
# no counterpart here and land as gateway_type=None with the raw name kept for display.
_GATEWAYS = frozenset(
    {
        "TELEGRAM_STARS",
        "YOOKASSA",
        "YOOMONEY",
        "CRYPTOMUS",
        "HELEKET",
        "FREEKASSA",
        "MULENPAY",
        "PLATEGA",
        "ROBOKASSA",
        "WATA",
    }
)
_REWARDS = frozenset(
    {"DURATION", "TRAFFIC", "DEVICES", "SUBSCRIPTION", "PERSONAL_DISCOUNT", "PURCHASE_DISCOUNT"}
)


# --- normalizers: the same reader shape feeds pg_dump strings, asyncpg natives and JSON ---


def _to_int(raw: object) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None if raw is None else int(raw)
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _to_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"t", "true", "1", "yes"}


def _to_utc(raw: object) -> dt.datetime | None:
    """RemnaShop stores UTC timestamptz; a naive value therefore reads as UTC."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dt.datetime):
        value = raw
    else:
        try:
            value = dt.datetime.fromisoformat(str(raw).strip())
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _to_json(raw: object) -> Any:
    if raw is None or isinstance(raw, dict | list):
        return raw
    try:
        return json.loads(str(raw))
    except ValueError:
        return None


def _to_decimal(raw: object) -> Decimal | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:
        return None


def _to_str_list(raw: object) -> list[str]:
    """UUID[] arrives as a native list (asyncpg), pg-array literal ``{a,b}`` or JSON list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        return [p.strip().strip('"') for p in inner.split(",") if p.strip()] if inner else []
    parsed = _to_json(s)
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [s]


def _clamp_pct(raw: object) -> int:
    return max(0, min(100, _to_int(raw) or 0))


def _upper(raw: object) -> str:
    return str(raw or "").strip().upper()


def _short_id_from_url(raw: object) -> str | None:
    """Panel sub URLs end with the short id; RemnaShop keeps no separate column for it."""
    url = str(raw or "")
    if not url:
        return None
    segment = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1][:16]
    if segment and all(c.isalnum() or c in "_-" for c in segment) and segment.isascii():
        return segment
    return None


def read_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read a RemnaShop dump into plain dicts (sync — call via asyncio.to_thread)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    parsed: dict[str, list[dict[str, Any]]]
    if stripped.startswith("{"):
        # Escape hatch: {"<table>": [rows]} JSON exported by hand.
        loaded: Any = json.loads(stripped)
        if not isinstance(loaded, dict):
            raise ValueError("это не .sql дамп remnashop")
        parsed = {
            str(table): [row for row in rows if isinstance(row, dict)]
            for table, rows in loaded.items()
            if isinstance(rows, list)
        }
    elif looks_like_pgdump(text):
        parsed = dict(parse_copy_blocks(text, set(SOURCE_TABLES)))
    else:
        raise ValueError("это не .sql дамп remnashop")
    return {table: parsed.get(table, []) for table in SOURCE_TABLES}


async def read_source_dsn(dsn: str) -> dict[str, list[dict[str, Any]]]:
    """Same shape straight from a live RemnaShop Postgres."""
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(dsn=dsn, timeout=8)
    try:
        data: dict[str, list[dict[str, Any]]] = {}
        for table in sorted(SOURCE_TABLES):
            try:
                records = await conn.fetch(f'SELECT * FROM "{table}"')
            except asyncpg.PostgresError:
                data[table] = []
            else:
                data[table] = [dict(r) for r in records]
        return data
    finally:
        await conn.close()


def probe(data: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Counts + sanity check without writing anything."""
    if not data.get("users"):
        return {"ok": False, "detail": "таблица users пуста или это не дамп remnashop"}
    completed = [t for t in data.get("transactions", []) if _upper(t.get("status")) == "COMPLETED"]
    return {
        "ok": True,
        "counts": {
            "users": len(data["users"]),
            "subscriptions": len(data.get("subscriptions", [])),
            "completed_transactions": len(completed),
            "promocodes": len(data.get("promocodes", [])),
        },
    }


class RemnashopImportService:
    def __init__(self, referrals: ReferralService) -> None:
        self._referrals = referrals

    async def run(self, uow: UnitOfWork, data: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "users_created": 0,
            "users_updated": 0,
            "referrals_linked": 0,
            "subscriptions": 0,
            "transactions": 0,
            "promocodes": 0,
            "skipped": [],
        }
        by_sid, current_ids = await self._import_users(uow, data["users"], summary)
        await self._link_referrals(uow, data["referrals"], by_sid, summary)
        await self._import_subscriptions(uow, data["subscriptions"], by_sid, current_ids, summary)
        await self._import_transactions(uow, data["transactions"], by_sid, summary)
        await self._import_promocodes(uow, data["promocodes"], summary)
        # Rebuild the tariff catalog from the imported subscriptions — without it the
        # operator lands with users but an empty tariff list and plan-less subs.
        await rebuild_plans(uow, source="remnashop", summary=summary)
        return summary

    @staticmethod
    async def _referral_code(uow: UnitOfWork, raw: object) -> str:
        """Adopt the source code when it fits our String(16) and is free (source allows 64)."""
        code = str(raw or "").strip()
        if 1 <= len(code) <= 16 and await uow.users.find_one(referral_code=code) is None:
            return code
        return generate_referral_code()

    async def _import_users(
        self, uow: UnitOfWork, rows: list[dict[str, Any]], summary: dict[str, Any]
    ) -> tuple[dict[int, User], set[int]]:
        by_sid: dict[int, User] = {}
        current_ids: set[int] = set()
        for row in rows:
            sid = _to_int(row.get("id"))
            if sid is None:
                continue
            tid = _to_int(row.get("telegram_id"))
            if tid is None:
                summary["skipped"].append(f"юзер #{sid}: web-аккаунт без Telegram")
                continue
            user = await uow.users.find_one(telegram_id=tid)
            if user is None:
                user = User(
                    telegram_id=tid,
                    referral_code=await self._referral_code(uow, row.get("referral_code")),
                    currency=Currency.RUB,
                )
                await uow.users.add(user)
                created = _to_utc(row.get("created_at"))
                if created is not None:
                    user.created_at = created
                summary["users_created"] += 1
            else:
                summary["users_updated"] += 1
            user.username = str(row.get("username") or "")[:64] or None
            user.first_name = str(row.get("name") or "")[:128] or None
            language = str(row.get("language") or "").strip().lower()
            user.language = Locale(language) if language in set(Locale) else Locale.default()
            if _to_bool(row.get("is_blocked")):
                user.status = UserStatus.BLOCKED
            user.is_trial_available = _to_bool(row.get("is_trial_available"))
            user.is_rules_accepted = _to_bool(row.get("is_rules_accepted"))
            user.personal_discount_pct = _clamp_pct(row.get("personal_discount"))
            user.purchase_discount_pct = _clamp_pct(row.get("purchase_discount"))
            by_sid[sid] = user
            current = _to_int(row.get("current_subscription_id"))
            if current is not None:
                current_ids.add(current)
        await uow.session.flush()
        return by_sid, current_ids

    async def _link_referrals(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_sid: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        """Only FIRST level: SECOND is derived in the target, importing it would double-link."""
        for row in rows:
            level = _upper(row.get("level"))  # enum label "FIRST" or int 1, source-dependent
            if level not in {"FIRST", "1"}:
                continue
            referred = by_sid.get(_to_int(row.get("referred_id")) or 0)
            referrer = by_sid.get(_to_int(row.get("referrer_id")) or 0)
            if referred is None or referrer is None or referred.referred_by_id is not None:
                continue
            bound = await self._referrals.bind(uow, referred, referrer.referral_code)
            if bound is not None:
                summary["referrals_linked"] += 1

    async def _import_subscriptions(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_sid: dict[int, User],
        current_ids: set[int],
        summary: dict[str, Any],
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        best_sub: dict[int, Subscription] = {}
        for row in rows:
            sid = _to_int(row.get("id"))
            # Historical rows share the same panel user — importing them would collide on
            # remnawave_uuid. Only rows referenced by current_subscription_id are real.
            if sid is None or sid not in current_ids:
                continue
            user = by_sid.get(_to_int(row.get("user_id")) or 0)
            if user is None:
                continue
            try:
                panel_uuid = uuid_mod.UUID(str(row.get("user_remna_id")))
            except ValueError:
                summary["skipped"].append(f"подписка #{sid}: нет uuid панели")
                continue

            expire = _to_utc(row.get("expire_at"))  # year-2099 sentinel = unlimited, keep as is
            status_name = _upper(row.get("status"))
            status = (
                SubscriptionStatus[status_name]
                if status_name in _SUB_STATUSES
                else SubscriptionStatus.EXPIRED
            )
            if status.is_usable and expire is not None and expire <= now:
                status = SubscriptionStatus.EXPIRED

            sub = await uow.subscriptions.find_one(remnawave_uuid=panel_uuid)
            if sub is None:
                short = _short_id_from_url(row.get("url"))
                if short is None or await uow.subscriptions.find_one(short_id=short) is not None:
                    short = generate_short_id()
                sub = Subscription(user_id=user.id, remnawave_uuid=panel_uuid, short_id=short)
                await uow.subscriptions.add(sub)
            sub.status = status
            sub.is_trial = _to_bool(row.get("is_trial"))
            sub.expire_at = expire
            sub.subscription_url = str(row.get("url") or "")[:512] or None
            sub.traffic_limit_bytes = (_to_int(row.get("traffic_limit")) or 0) * 1024**3
            sub.device_limit = _to_int(row.get("device_limit"))
            sub.traffic_limit_strategy = str(row.get("traffic_limit_strategy") or "")[:32] or None
            sub.internal_squads = _to_str_list(row.get("internal_squads"))
            sub.external_squad = str(row.get("external_squad") or "")[:36] or None
            snapshot = _to_json(row.get("plan_snapshot"))
            if not isinstance(snapshot, dict):
                snapshot = {}
            if not snapshot.get("name"):
                snapshot["name"] = "Imported"
            snapshot["source"] = "remnashop"
            sub.plan_snapshot = snapshot
            summary["subscriptions"] += 1

            if status.is_usable:
                current = best_sub.get(user.id)
                if current is None or (sub.expire_at or now) > (current.expire_at or now):
                    best_sub[user.id] = sub

        await uow.session.flush()
        for user_id, sub in best_sub.items():
            owner = await uow.users.get(user_id)
            if owner is not None and owner.current_subscription_id is None:
                owner.current_subscription_id = sub.id

    async def _import_transactions(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_sid: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        for row in rows:
            if _upper(row.get("status")) != "COMPLETED":
                continue
            if _to_bool(row.get("is_test")):  # sandbox payments must not inflate imported revenue
                continue
            user = by_sid.get(_to_int(row.get("user_id")) or 0)
            if user is None:
                continue
            # Empty payment_id falls back to a stable synthetic id (mirror shopbot/bedolaga) so
            # the transaction is imported and still matches on re-run, not silently dropped.
            external = str(row.get("payment_id") or "") or f"remnashop-{_to_int(row.get('id'))}"
            if await uow.transactions.find_one(external_id=external) is not None:
                continue

            pricing = _to_json(row.get("pricing"))
            if not isinstance(pricing, dict):
                pricing = {}
            amount = _to_decimal(pricing.get("final_amount"))
            if amount is None:
                summary["skipped"].append(f"платёж {external}: нет суммы в pricing")
                continue
            currency_name = _upper(row.get("currency"))
            try:
                currency = Currency[currency_name]
            except KeyError:
                summary["skipped"].append(
                    f"платёж {external}: валюта {currency_name or '?'} не поддерживается"
                )
                continue
            # Major units -> minor: RUB/USD x100, XTR (stars) x1.
            amount_minor = int(
                (amount * 10**currency.exponent).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )

            gateway_name = _upper(row.get("gateway_type"))
            gateway = PaymentGatewayType[gateway_name] if gateway_name in _GATEWAYS else None
            display = str(row.get("gateway_display_name") or "").strip() or gateway_name
            purchase_name = _upper(row.get("purchase_type"))
            snapshot = _to_json(row.get("plan_snapshot"))
            created = _to_utc(row.get("created_at")) or dt.datetime.now(dt.UTC)

            txn = Transaction(
                user_id=user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                status=TransactionStatus.COMPLETED,
                amount_minor=amount_minor,
                currency=currency,
                external_id=external,
                gateway_type=gateway,
                gateway_display_name=display[:64] or None,
                payment_method=str(row.get("payment_method") or "")[:64] or None,
                purchase_type=(
                    PurchaseType[purchase_name] if purchase_name in _PURCHASE_TYPES else None
                ),
                pricing=_coerce_pricing(pricing),
                plan_snapshot=snapshot if isinstance(snapshot, dict) else None,
                completed_at=_to_utc(row.get("updated_at")) or created,
                # Historical money from BEFORE this bot existed — never a candidate for a fresh
                # "Мой налог" receipt under THIS INN. `created` above falls back to "now" when
                # the dump has no date, which would otherwise land squarely inside
                # list_unreceipted's lookback window; pre-stamping receipt_created_at (normally
                # an in-flight filing claim) permanently excludes the row from that query without
                # a schema change — nothing else in the app reads this field.
                receipt_created_at=dt.datetime.now(dt.UTC),
            )
            await uow.transactions.add(txn)
            txn.created_at = created
            summary["transactions"] += 1

    async def _import_promocodes(
        self, uow: UnitOfWork, rows: list[dict[str, Any]], summary: dict[str, Any]
    ) -> None:
        for row in rows:
            code = str(row.get("code") or "")[:64]  # source codes are case-sensitive, keep as is
            if not code:
                continue
            reward_name = _upper(row.get("reward_type"))
            if reward_name not in _REWARDS:
                summary["skipped"].append(
                    f"промокод {code}: тип {reward_name or '?'} не поддерживается"
                )
                continue
            reward = RewardType[reward_name]
            value = _to_int(row.get("reward")) or 0

            promo = await uow.promocodes.find_one(code=code)
            if promo is None:
                promo = Promocode(code=code, reward_type=reward, reward_value=value)
                uow.session.add(promo)
            else:
                promo.reward_type, promo.reward_value = reward, value
            snapshot = _to_json(row.get("plan_snapshot"))
            promo.plan_snapshot = snapshot if isinstance(snapshot, dict) else None
            availability_name = _upper(row.get("availability"))
            try:
                promo.availability = Availability[availability_name]
            except KeyError:
                promo.availability = Availability.ALL
            promo.is_active = _to_bool(row.get("is_active"))
            promo.is_reusable = _to_bool(row.get("is_reusable"))
            promo.expires_at = _to_utc(row.get("expires_at"))
            promo.max_activations = _to_int(row.get("max_activations"))
            summary["promocodes"] += 1


def _coerce_pricing(pricing: dict[str, Any]) -> dict[str, Any]:
    """JSONB Decimals may arrive as strings; store plain numbers for downstream math."""
    out = dict(pricing)
    for key in ("original_amount", "final_amount"):
        value = _to_decimal(out.get(key))
        if value is not None:
            out[key] = int(value) if value == value.to_integral_value() else float(value)
    percent = _to_int(out.get("discount_percent"))
    if percent is not None:
        out["discount_percent"] = percent
    return out
