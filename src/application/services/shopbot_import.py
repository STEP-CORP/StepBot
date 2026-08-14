"""Importer: remnawave-shopbot (SQLite users.db) -> our schema.

Source of truth for the source format: shopbot keeps everything in one SQLite file
(`users.db`), money as float RUB, datetimes as naive MSK strings, and links each
vpn_key to its own Remnawave panel user (uuid + short_uuid + subscription_url) —
which maps 1:1 onto our «panel-user per subscription» invariant.

The import is idempotent: users match by telegram_id, subscriptions by
remnawave_uuid, transactions by external_id, promocodes by code — re-running
updates instead of duplicating. Panel users are NOT touched: we adopt the
existing uuids, so subscribers keep working mid-migration.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid as uuid_mod
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from src.application.services.ids import generate_referral_code, generate_short_id
from src.application.services.plan_rebuild import rebuild_plans
from src.core.enums import (
    Currency,
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
    from pathlib import Path

    from src.application.services.referral import ReferralService
    from src.infrastructure.database.uow import UnitOfWork

log = get_logger(__name__)

_MSK = dt.timezone(dt.timedelta(hours=3))

_GATEWAY_MAP: dict[str, PaymentGatewayType] = {
    "yookassa": PaymentGatewayType.YOOKASSA,
    "telegram stars": PaymentGatewayType.TELEGRAM_STARS,
    "cryptobot": PaymentGatewayType.CRYPTOBOT,
}

_ACTION_TYPE: dict[str, TransactionType] = {
    "new": TransactionType.SUBSCRIPTION_PAYMENT,
    "extend": TransactionType.SUBSCRIPTION_PAYMENT,
    "top_up": TransactionType.DEPOSIT,
    "topup": TransactionType.DEPOSIT,
    "referral_bonus": TransactionType.REFERRAL_REWARD,
    "admin_balance_adjust": TransactionType.GIFT,
}

_PAID_STATUSES = {"paid", "succeeded", "success", "completed"}


def _to_utc(raw: object) -> dt.datetime | None:
    """Shopbot writes naive MSK strings (a few legacy rows are UTC — ±3h is fine)."""
    if not raw:
        return None
    s = str(raw).strip().replace("T", " ").split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=_MSK).astimezone(dt.UTC)
    return None


def _rub_to_minor(raw: object) -> int:
    """Float RUB -> integer kopeks; Decimal(str(...)) kills float tails like 99.99000001."""
    try:
        value = Decimal(str(raw or 0))
    except ArithmeticError:
        return 0
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
    except sqlite3.Error:
        return []
    return [dict(r) for r in cur.fetchall()]


def read_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read the whole shopbot DB into plain dicts (sync — call via asyncio.to_thread)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "users": _rows(conn, "users"),
            "vpn_keys": _rows(conn, "vpn_keys"),
            "transactions": _rows(conn, "transactions"),
            "promo_codes": _rows(conn, "promo_codes"),
        }
    finally:
        conn.close()


def probe(path: Path) -> dict[str, Any]:
    """Counts + sanity warnings without writing anything."""
    data = read_source(path)
    if not data["users"]:
        return {"ok": False, "detail": "таблица users пуста или это не users.db шопбота"}
    paid = [t for t in data["transactions"] if str(t.get("status", "")).lower() in _PAID_STATUSES]
    return {
        "ok": True,
        "counts": {
            "users": len(data["users"]),
            "vpn_keys": len(data["vpn_keys"]),
            "paid_transactions": len(paid),
            "promo_codes": len(data["promo_codes"]),
        },
    }


class ShopbotImportService:
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
        by_tid = await self._import_users(uow, data["users"], summary)
        await self._link_referrals(uow, data["users"], by_tid, summary)
        await self._import_keys(uow, data["vpn_keys"], by_tid, summary)
        await self._import_transactions(uow, data["transactions"], by_tid, summary)
        await self._import_promocodes(uow, data["promo_codes"], summary)
        # Rebuild the tariff catalog from the imported subscriptions — without it the
        # operator lands with users but an empty tariff list and plan-less subs.
        await rebuild_plans(uow, source="remnawave-shopbot", summary=summary)
        return summary

    async def _import_users(
        self, uow: UnitOfWork, rows: list[dict[str, Any]], summary: dict[str, Any]
    ) -> dict[int, User]:
        by_tid: dict[int, User] = {}
        for row in rows:
            tid = row.get("telegram_id")
            if not tid:
                continue
            tid = int(tid)
            # Both wallets merge into ours; the referral one is money the user owns too.
            balance = _rub_to_minor(row.get("balance")) + _rub_to_minor(row.get("referral_balance"))
            user = await uow.users.find_one(telegram_id=tid)
            if user is None:
                user = User(
                    telegram_id=tid,
                    username=(row.get("username") or None),
                    referral_code=generate_referral_code(),
                    currency=Currency.RUB,
                    balance_minor=balance,
                )
                await uow.users.add(user)
                created = _to_utc(row.get("registration_date"))
                if created is not None:
                    user.created_at = created
                summary["users_created"] += 1
            else:
                summary["users_updated"] += 1
            user.is_trial_available = not bool(row.get("trial_used"))
            if bool(row.get("is_banned")):
                user.status = UserStatus.BLOCKED
            by_tid[tid] = user
        await uow.session.flush()
        return by_tid

    async def _link_referrals(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_tid: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        """Second pass: shopbot stores referrer's telegram_id; FK is not enforced there."""
        for row in rows:
            tid, ref = row.get("telegram_id"), row.get("referred_by")
            if not tid or not ref or int(ref) == int(tid):
                continue
            referred = by_tid.get(int(tid))
            referrer = by_tid.get(int(ref)) or await uow.users.find_one(telegram_id=int(ref))
            if referred is None or referrer is None or referred.referred_by_id is not None:
                continue
            bound = await self._referrals.bind(uow, referred, referrer.referral_code)
            if bound is not None:
                summary["referrals_linked"] += 1

    async def _import_keys(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_tid: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        best_sub: dict[int, Subscription] = {}
        for row in rows:
            user = by_tid.get(int(row.get("user_id") or 0))
            raw_uuid = row.get("remnawave_user_uuid") or row.get("xui_client_uuid")
            if user is None or not raw_uuid:
                summary["skipped"].append(f"ключ #{row.get('key_id')}: нет юзера или uuid панели")
                continue
            try:
                panel_uuid = uuid_mod.UUID(str(raw_uuid))
            except ValueError:
                summary["skipped"].append(f"ключ #{row.get('key_id')}: кривой uuid")
                continue

            expire = _to_utc(row.get("expire_at") or row.get("expiry_date"))
            email = str(row.get("key_email") or row.get("email") or "")
            is_trial = email.startswith("trial_")
            if expire is not None and expire > now:
                status = SubscriptionStatus.TRIAL if is_trial else SubscriptionStatus.ACTIVE
            else:
                status = SubscriptionStatus.EXPIRED

            sub = await uow.subscriptions.find_one(remnawave_uuid=panel_uuid)
            if sub is None:
                short = str(row.get("short_uuid") or "")[:16] or generate_short_id()
                if await uow.subscriptions.find_one(short_id=short) is not None:
                    short = generate_short_id()
                sub = Subscription(user_id=user.id, remnawave_uuid=panel_uuid, short_id=short)
                await uow.subscriptions.add(sub)
            sub.status = status
            sub.is_trial = is_trial
            sub.expire_at = expire
            sub.subscription_url = (
                row.get("subscription_url") or row.get("connection_string") or None
            )
            sub.traffic_limit_bytes = int(row.get("traffic_limit_bytes") or 0)
            sub.traffic_limit_strategy = row.get("traffic_limit_strategy") or None
            if row.get("squad_uuid"):
                sub.internal_squads = [str(row["squad_uuid"])]
            sub.plan_snapshot = {
                "name": row.get("tag") or row.get("host_name") or "Imported",
                "source": "remnawave-shopbot",
                "host_name": row.get("host_name"),
            }
            summary["subscriptions"] += 1

            if status.is_usable:
                current = best_sub.get(user.id)
                if current is None or (sub.expire_at or now) > (current.expire_at or now):
                    best_sub[user.id] = sub

        await uow.session.flush()
        for user_id, sub in best_sub.items():
            user = await uow.users.get(user_id)
            if user is not None and user.current_subscription_id is None:
                user.current_subscription_id = sub.id

    async def _import_transactions(
        self,
        uow: UnitOfWork,
        rows: list[dict[str, Any]],
        by_tid: dict[int, User],
        summary: dict[str, Any],
    ) -> None:
        for row in rows:
            if str(row.get("status", "")).lower() not in _PAID_STATUSES:
                continue
            user = by_tid.get(int(row.get("user_id") or 0))
            if user is None:
                continue
            external = str(row.get("payment_id") or "") or f"shopbot-{row.get('transaction_id')}"
            if await uow.transactions.find_one(external_id=external) is not None:
                continue

            meta: dict[str, Any] = {}
            if row.get("metadata"):
                try:
                    meta = json.loads(row["metadata"])
                except (ValueError, TypeError):
                    meta = {}
            method = str(row.get("payment_method") or "")
            txn_type = _ACTION_TYPE.get(
                str(meta.get("action") or ""), TransactionType.SUBSCRIPTION_PAYMENT
            )
            created = _to_utc(row.get("created_date")) or dt.datetime.now(dt.UTC)
            txn = Transaction(
                user_id=user.id,
                type=txn_type,
                status=TransactionStatus.COMPLETED,
                amount_minor=_rub_to_minor(row.get("amount_rub")),
                currency=Currency.RUB,
                external_id=external,
                gateway_type=_GATEWAY_MAP.get(method.lower()),
                gateway_display_name=method or "shopbot",
                completed_at=created,
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
            code = str(row.get("code") or "").upper()
            if not code:
                continue
            ptype = str(row.get("promo_type") or "discount")
            percent = row.get("discount_percent")
            amount = row.get("discount_amount")
            if ptype == "balance":
                reward, value = RewardType.BALANCE, _rub_to_minor(row.get("reward_value"))
            elif ptype == "universal":
                reward, value = RewardType.DURATION, int(row.get("reward_value") or 0)
            elif percent:
                reward, value = RewardType.PURCHASE_DISCOUNT, int(float(percent))
            else:
                summary["skipped"].append(
                    f"промокод {code}: фикс-скидка {amount}₽ не поддерживается"
                )
                continue

            promo = await uow.promocodes.find_one(code=code)
            if promo is None:
                promo = Promocode(code=code, reward_type=reward, reward_value=value)
                uow.session.add(promo)
            else:
                promo.reward_type, promo.reward_value = reward, value
            promo.is_active = bool(row.get("is_active", 1))
            promo.expires_at = _to_utc(row.get("valid_until"))
            promo.max_activations = row.get("usage_limit_total")
            summary["promocodes"] += 1
