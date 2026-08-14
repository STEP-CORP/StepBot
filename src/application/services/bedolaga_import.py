"""Importer: Bedolaga (remnawave-bedolaga-telegram-bot) -> our schema.

Bedolaga runs on PostgreSQL (SQLite fallback), keeps money as integer kopeks and
datetimes as UTC timestamptz — so amounts copy 1:1 and naive strings are UTC.
The panel uuid lives on ``subscriptions.remnawave_uuid`` in multi-tariff mode and
on ``users.remnawave_uuid`` in the default single-tariff mode; we adopt either,
so subscribers keep working mid-migration.

``read_source`` accepts whatever an owner can actually hand over: the live
SQLite file, a ``backup_*.tar.gz`` archive (with ``database.sql`` / ``.sqlite`` /
``.json`` inside), a plain pg_dump ``.sql`` or the ORM-dump JSON — sniffed by
content. ``read_source_dsn`` pulls the same tables from a live Postgres.

The import is idempotent: users match by telegram_id, subscriptions by
remnawave_uuid, transactions by external_id, promocodes by code — re-running
updates instead of duplicating and never overwrites an existing user's balance.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tarfile
import tempfile
import uuid as uuid_mod
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from src.application.services.ids import generate_referral_code, generate_short_id
from src.application.services.pgdump import parse_copy_blocks
from src.application.services.plan_rebuild import rebuild_plans
from src.core.enums import (
    Currency,
    Locale,
    PaymentGatewayType,
    RewardType,
    SubscriptionStatus,
    TransactionStatus,
    TransactionType,
    UserStatus,
)
from src.core.logging import get_logger
from src.infrastructure.database.models.promocode import Promocode
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from src.application.services.referral import ReferralService
    from src.infrastructure.database.uow import UnitOfWork

log = get_logger(__name__)

SOURCE_TABLES: frozenset[str] = frozenset(
    {"users", "subscriptions", "transactions", "promocodes", "tariffs"}
)

_GIB = 1024**3
_MAX_MEMBER_BYTES = 500 * 1024 * 1024
_ARCHIVE_PRIORITY = ("database.sql", "database.sqlite", "database.json")

_STATUS_MAP: dict[str, SubscriptionStatus] = {
    "trial": SubscriptionStatus.TRIAL,
    "active": SubscriptionStatus.ACTIVE,
    "expired": SubscriptionStatus.EXPIRED,
    "disabled": SubscriptionStatus.DISABLED,
    "limited": SubscriptionStatus.LIMITED,
    "pending": SubscriptionStatus.PENDING,
}

_TXN_TYPE: dict[str, TransactionType] = {
    "deposit": TransactionType.DEPOSIT,
    "topup": TransactionType.DEPOSIT,  # legacy spelling
    "subscription_payment": TransactionType.SUBSCRIPTION_PAYMENT,
    "subscription": TransactionType.SUBSCRIPTION_PAYMENT,  # legacy
    "purchase": TransactionType.SUBSCRIPTION_PAYMENT,  # legacy
    "referral_reward": TransactionType.REFERRAL_REWARD,
    "referral": TransactionType.REFERRAL_REWARD,  # legacy
    "refund": TransactionType.REFUND,
    "withdrawal": TransactionType.WITHDRAWAL,
    "gift_payment": TransactionType.GIFT,
    "gift": TransactionType.GIFT,  # legacy
    "admin_adjust": TransactionType.GIFT,  # legacy
    "poll_reward": TransactionType.GIFT,
}

# Bedolaga PaymentMethod values that exist in our enum under the same name;
# the enum constructor validates every entry at import time.
_SAME_NAME_GATEWAYS = (
    "telegram_stars",
    "tribute",
    "yookassa",
    "cryptobot",
    "heleket",
    "mulenpay",
    "wata",
    "platega",
    "cloudpayments",
    "freekassa",
    "kassa_ai",
    "riopay",
    "severpay",
    "paypear",
    "rollypay",
    "overpay",
    "aurapay",
    "antilopay",
    "lava",
    "manual",
)
_GATEWAY_MAP: dict[str, PaymentGatewayType] = {
    name: PaymentGatewayType(name) for name in _SAME_NAME_GATEWAYS
}
_GATEWAY_MAP["pal24"] = PaymentGatewayType.PAYPALYCH
_GATEWAY_MAP["stars"] = PaymentGatewayType.TELEGRAM_STARS  # legacy spelling
_GATEWAY_MAP["cryptomus"] = PaymentGatewayType.CRYPTOMUS  # legacy forks

_TRUTHY = {"t", "true", "1", "yes", "y", "on"}


# --- normalizers: the same reader output feeds sqlite-native, pg_dump-string,
# --- asyncpg-native and JSON paths, so every value may be str OR native.


def _to_int(raw: object) -> int:
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return int(raw)
    s = str(raw).strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


def _opt_int(raw: object) -> int | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _to_int(raw)


def _to_float(raw: object) -> float:
    try:
        return float(str(raw))
    except ValueError:
        return 0.0


def _to_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int | float):
        return bool(raw)
    return str(raw or "").strip().lower() in _TRUTHY


def _to_utc(raw: object) -> dt.datetime | None:
    """Bedolaga stores UTC everywhere; naive values (sqlite / ISO strings) are UTC too."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dt.datetime):
        parsed = raw
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(raw).strip())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _to_json(raw: object) -> Any:
    """JSON columns arrive native from asyncpg/sqlite but as strings from dumps."""
    if isinstance(raw, dict | list):
        return raw
    if raw is None or raw == "":
        return None
    try:
        return json.loads(str(raw))
    except ValueError:
        return None


def _text(raw: object, limit: int) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s[:limit] if s else None


# --- source readers ----------------------------------------------------------


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
    except sqlite3.Error:
        return []
    return [dict(r) for r in cur.fetchall()]


def _read_sqlite(path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {table: _rows(conn, table) for table in SOURCE_TABLES}
    finally:
        conn.close()


def _fill(partial: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {table: [dict(r) for r in partial.get(table, ())] for table in SOURCE_TABLES}


def _from_orm_dump(obj: Any) -> dict[str, list[dict[str, Any]]]:
    """Bedolaga's no-pg_dump fallback: ``{"data": {table: [row-dicts]}}``."""
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        data = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for table in SOURCE_TABLES:
        rows = data.get(table)
        if isinstance(rows, list):
            out[table] = [dict(r) for r in rows if isinstance(r, dict)]
        else:
            out[table] = []
    return out


def _read_archive(path: Path) -> dict[str, list[dict[str, Any]]]:
    """``backup_*.tar.gz``: pick the best dump inside (.sql > .sqlite > .json)."""
    with tarfile.open(path, mode="r:gz") as tar:
        members: dict[str, tarfile.TarInfo] = {}
        for member in tar.getmembers():
            name = PurePosixPath(member.name).name
            if member.isfile() and name in _ARCHIVE_PRIORITY and member.size <= _MAX_MEMBER_BYTES:
                members.setdefault(name, member)
        for name in _ARCHIVE_PRIORITY:
            found = members.get(name)
            handle = tar.extractfile(found) if found is not None else None
            if handle is None:
                continue
            payload = handle.read()
            if name == "database.sql":
                text = payload.decode("utf-8", errors="replace")
                return _fill(parse_copy_blocks(text, set(SOURCE_TABLES)))
            if name == "database.json":
                return _from_orm_dump(json.loads(payload))
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
                tmp.write(payload)
            tmp_path = Path(tmp.name)
            try:
                return _read_sqlite(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
    return _fill({})


def read_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read any Bedolaga export into plain dicts (sync — call via asyncio.to_thread).

    Sniffs by content, not extension: SQLite file, tar.gz backup, ORM-dump JSON
    or plain pg_dump text. Always returns all ``SOURCE_TABLES`` keys.
    """
    with path.open("rb") as fh:
        head = fh.read(16)
    if head.startswith(b"SQLite format 3"):
        return _read_sqlite(path)
    if head.startswith(b"\x1f\x8b"):
        return _read_archive(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.lstrip().startswith("{"):
        return _from_orm_dump(json.loads(text))
    return _fill(parse_copy_blocks(text, set(SOURCE_TABLES)))


async def read_source_dsn(dsn: str) -> dict[str, list[dict[str, Any]]]:
    """Pull the source tables straight from a live Bedolaga Postgres."""
    import asyncpg  # type: ignore[import-untyped]  # optional path, no stubs published

    conn = await asyncpg.connect(dsn=dsn, timeout=8)
    try:
        out: dict[str, list[dict[str, Any]]] = {}
        for table in SOURCE_TABLES:
            try:
                records = await conn.fetch(f'SELECT * FROM "{table}"')
            except asyncpg.PostgresError:
                out[table] = []
            else:
                out[table] = [dict(r) for r in records]
        return out
    finally:
        await conn.close()


def probe(data: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Counts + sanity check without writing anything."""
    paid = sum(1 for t in data["transactions"] if _to_bool(t.get("is_completed")))
    result: dict[str, Any] = {
        "ok": bool(data["users"]),
        "counts": {
            "users": len(data["users"]),
            "subscriptions": len(data["subscriptions"]),
            "paid_transactions": paid,
            "promocodes": len(data["promocodes"]),
        },
    }
    if not result["ok"]:
        result["detail"] = "таблица users пуста — это не база Bedolaga"
    return result


class BedolagaImportService:
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
        by_src = await self._import_users(uow, data, summary)
        await self._link_referrals(uow, data["users"], by_src, summary)
        await self._import_subscriptions(uow, data, by_src, summary)
        await self._import_transactions(uow, data["transactions"], by_src, summary)
        await self._import_promocodes(uow, data["promocodes"], summary)
        # Rebuild the tariff catalog from the imported subscriptions — without it the
        # operator lands with users but an empty tariff list and plan-less subs.
        await rebuild_plans(uow, source="bedolaga", summary=summary)
        return summary

    @staticmethod
    async def _adopt_referral_code(uow: UnitOfWork, row: dict[str, Any]) -> str:
        """Keep the source code so old invite links survive; regen on conflict/oversize."""
        code = str(row.get("referral_code") or "").strip()
        if 1 <= len(code) <= 16 and await uow.users.find_one(referral_code=code) is None:
            return code
        return generate_referral_code()

    async def _import_users(
        self, uow: UnitOfWork, data: dict[str, list[dict[str, Any]]], summary: dict[str, Any]
    ) -> dict[int, User]:
        """Returns source users.id -> User (subs/transactions reference the internal id)."""
        with_subs = {_to_int(row.get("user_id")) for row in data["subscriptions"]}
        by_src: dict[int, User] = {}
        for row in data["users"]:
            src_id = _to_int(row.get("id"))
            if str(row.get("status") or "").lower() == "deleted":
                summary["skipped"].append(f"юзер #{src_id}: удалён в источнике")
                continue
            tid_raw = row.get("telegram_id")
            if tid_raw is None or str(tid_raw).strip() == "":
                summary["skipped"].append(f"юзер #{src_id}: без telegram_id (кабинетный)")
                continue
            tid = _to_int(tid_raw)

            user = await uow.users.find_one(telegram_id=tid)
            if user is None:
                user = User(
                    telegram_id=tid,
                    referral_code=await self._adopt_referral_code(uow, row),
                    currency=Currency.RUB,
                    balance_minor=_to_int(row.get("balance_kopeks")),  # already kopeks, no x100
                )
                await uow.users.add(user)
                created = _to_utc(row.get("created_at"))
                if created is not None:
                    user.created_at = created
                summary["users_created"] += 1
            else:
                summary["users_updated"] += 1
            user.username = _text(row.get("username"), 64)
            user.first_name = _text(row.get("first_name"), 128)
            user.last_name = _text(row.get("last_name"), 128)
            lang = str(row.get("language") or "").strip().lower()
            user.language = Locale.EN if lang == "en" else Locale.default()
            if str(row.get("status") or "").lower() in {"blocked", "banned"}:
                user.status = UserStatus.BLOCKED
            # Web-cabinet identity travels too, but email is unique: adopt it only when
            # no OTHER user already owns it (organic signup or an earlier import).
            email = str(row.get("email") or "").strip().lower()
            if email:
                clash = await uow.users.find_one(email=email)
                if clash is None or clash.id == user.id:
                    user.email = email
                    user.email_verified = _to_bool(row.get("email_verified"))
                    if row.get("password_hash"):
                        user.password_hash = str(row["password_hash"])[:255]
                else:
                    summary["skipped"].append(f"email {email}: уже занят другим юзером")
            had_paid = _to_bool(row.get("has_had_paid_subscription"))
            user.has_had_paid_subscription = had_paid
            user.has_made_first_topup = _to_bool(row.get("has_made_first_topup"))
            user.is_trial_available = not had_paid and src_id not in with_subs
            user.referral_commission_percent = _opt_int(row.get("referral_commission_percent"))
            by_src[src_id] = user
        await uow.session.flush()
        return by_src

    async def _link_referrals(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_src: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        """Second pass: referred_by_id is the source-internal users.id, not a telegram_id."""
        for row in rows:
            ref_raw = row.get("referred_by_id")
            if ref_raw is None or str(ref_raw).strip() == "":
                continue
            referred = by_src.get(_to_int(row.get("id")))
            referrer = by_src.get(_to_int(ref_raw))
            if referred is None or referrer is None or referred is referrer:
                continue
            if referred.referred_by_id is not None:
                continue
            bound = await self._referrals.bind(uow, referred, referrer.referral_code)
            if bound is not None:
                summary["referrals_linked"] += 1

    async def _import_subscriptions(
        self,
        uow: UnitOfWork,
        data: dict[str, list[dict[str, Any]]],
        by_src: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        users_by_src = {_to_int(r.get("id")): r for r in data["users"]}
        tariff_names = {_to_int(t.get("id")): str(t.get("name") or "") for t in data["tariffs"]}
        now = dt.datetime.now(dt.UTC)
        seen: dict[uuid_mod.UUID, Subscription] = {}  # two rows may share a user-level uuid
        best_sub: dict[int, Subscription] = {}
        for row in data["subscriptions"]:
            src_id = _to_int(row.get("id"))
            src_user_id = _to_int(row.get("user_id"))
            user = by_src.get(src_user_id)
            if user is None:
                summary["skipped"].append(f"подписка #{src_id}: юзер не импортирован")
                continue
            # Single-tariff installs keep the panel uuid on the user row, not the sub.
            user_row = users_by_src.get(src_user_id) or {}
            raw_uuid = row.get("remnawave_uuid") or user_row.get("remnawave_uuid")
            if not raw_uuid:
                summary["skipped"].append(f"подписка #{src_id}: нет uuid панели")
                continue
            try:
                panel_uuid = uuid_mod.UUID(str(raw_uuid))
            except ValueError:
                summary["skipped"].append(f"подписка #{src_id}: кривой uuid панели")
                continue

            expire = _to_utc(row.get("end_date"))
            raw_status = str(row.get("status") or "").strip().lower()
            status = _STATUS_MAP.get(raw_status, SubscriptionStatus.EXPIRED)
            if status.is_usable and expire is not None and expire <= now:
                status = SubscriptionStatus.EXPIRED

            sub = seen.get(panel_uuid) or await uow.subscriptions.find_one(
                remnawave_uuid=panel_uuid
            )
            if sub is None:
                short = str(row.get("remnawave_short_id") or "").strip()[:16]
                if not short:
                    short = str(row.get("remnawave_short_uuid") or "").strip()[:16]
                if not short or await uow.subscriptions.find_one(short_id=short) is not None:
                    short = generate_short_id()
                sub = Subscription(user_id=user.id, remnawave_uuid=panel_uuid, short_id=short)
                await uow.subscriptions.add(sub)
                created = _to_utc(row.get("created_at"))
                if created is not None:
                    sub.created_at = created
            seen[panel_uuid] = sub
            sub.status = status
            sub.is_trial = _to_bool(row.get("is_trial"))
            sub.start_at = _to_utc(row.get("start_date"))
            sub.expire_at = expire
            sub.traffic_limit_bytes = _to_int(row.get("traffic_limit_gb")) * _GIB
            sub.traffic_used_bytes = int(_to_float(row.get("traffic_used_gb")) * _GIB)
            sub.device_limit = _opt_int(row.get("device_limit"))
            squads = _to_json(row.get("connected_squads"))
            sub.internal_squads = [str(s) for s in squads] if isinstance(squads, list) else []
            sub.subscription_url = _text(row.get("subscription_url"), 512)
            sub.crypto_link = _text(row.get("subscription_crypto_link"), 512)
            sub.autopay_enabled = _to_bool(row.get("autopay_enabled"))
            sub.autopay_days_before = _to_int(row.get("autopay_days_before"))
            sub.autopay_period_days = _opt_int(row.get("autopay_period_days"))
            tariff_id = _opt_int(row.get("tariff_id"))
            tariff_name = tariff_names.get(tariff_id) if tariff_id is not None else None
            sub.plan_snapshot = {
                "name": tariff_name or "Imported",
                "source": "bedolaga",
                "tariff_id": tariff_id,
            }
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
        by_src: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        for row in rows:
            if not _to_bool(row.get("is_completed")):
                continue
            src_id = _to_int(row.get("id"))
            src_type = str(row.get("type") or "").strip().lower()
            txn_type = _TXN_TYPE.get(src_type)
            if txn_type is None:  # incl. 'failed_refund' — nothing to mirror it onto
                summary["skipped"].append(f"транзакция #{src_id}: тип {src_type} не переносится")
                continue
            user = by_src.get(_to_int(row.get("user_id")))
            if user is None:
                continue
            external = (str(row.get("external_id") or "").strip() or f"bedolaga-{src_id}")[:128]
            if await uow.transactions.find_one(external_id=external) is not None:
                continue

            method = str(row.get("payment_method") or "").strip().lower()
            created = _to_utc(row.get("created_at")) or dt.datetime.now(dt.UTC)
            txn = Transaction(
                user_id=user.id,
                type=txn_type,
                status=TransactionStatus.COMPLETED,
                amount_minor=_to_int(row.get("amount_kopeks")),  # already kopeks, no x100
                currency=Currency.RUB,
                external_id=external,
                gateway_type=_GATEWAY_MAP.get(method),
                gateway_display_name=_text(method, 64) or "bedolaga",
                completed_at=_to_utc(row.get("completed_at")) or created,
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
            code = str(row.get("code") or "").strip().upper()[:64]
            if not code:
                continue
            ptype = str(row.get("type") or "").strip().lower()
            if ptype == "balance":
                reward, value = RewardType.BALANCE, _to_int(row.get("balance_bonus_kopeks"))
            elif ptype in {"subscription_days", "trial_subscription"}:
                reward, value = RewardType.DURATION, _to_int(row.get("subscription_days"))
            elif ptype == "discount":
                # Source quirk: for this type balance_bonus_kopeks holds a PERCENT.
                reward = RewardType.PURCHASE_DISCOUNT
                value = min(_to_int(row.get("balance_bonus_kopeks")), 100)
            else:
                summary["skipped"].append(f"промокод {code}: тип {ptype} не поддерживается")
                continue

            promo = await uow.promocodes.find_one(code=code)
            if promo is None:
                promo = Promocode(code=code, reward_type=reward, reward_value=value)
                uow.session.add(promo)
            else:
                promo.reward_type, promo.reward_value = reward, value
            active_raw = row.get("is_active")
            promo.is_active = _to_bool(active_raw) if active_raw is not None else True
            promo.first_purchase_only = _to_bool(row.get("first_purchase_only"))
            promo.expires_at = _to_utc(row.get("valid_until"))
            promo.max_activations = _to_int(row.get("max_uses")) or None
            summary["promocodes"] += 1
