"""Channel gate parsing, cart intent round-trip, trial carryover on plan change."""

from __future__ import annotations

import datetime as dt
import uuid

from src.application.dto.pricing import PurchaseRequest
from src.application.services.pricing import PricingService
from src.application.services.purchase import PurchaseService
from src.application.services.remnawave import RemnawaveService
from src.application.services.subscription import SubscriptionService
from src.bot.gate import parse_channels
from src.core.enums import Currency, PurchaseType
from src.infrastructure.database.models.server_squad import ServerSquad
from src.infrastructure.database.uow import UnitOfWork
from tests.factories import make_plan, make_user
from tests.fakes import FakeRemnawaveClient, RecordingEventBus


def test_parse_channels_merges_legacy_and_dedups() -> None:
    raw = "@news | Новости | https://t.me/news\n-1001234 | VIP\n@news"
    channels = parse_channels(raw, legacy_id="@main")
    refs = [c.ref for c in channels]
    assert refs == ["@main", "@news", "-1001234"]  # legacy first, dedup @news
    assert channels[1].title == "Новости"
    assert channels[1].url == "https://t.me/news"
    assert channels[2].title == "VIP"  # url derived from the id
    assert channels[0].url == "https://t.me/main"


async def test_cart_intent_round_trip() -> None:
    # In-memory stand-in for Redis: just needs get/set/getdel.
    class FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        async def set(self, k: str, v: str, ex: int = 0) -> None:
            self.store[k] = v

        async def get(self, k: str) -> str | None:
            return self.store.get(k)

        async def getdel(self, k: str) -> str | None:
            return self.store.pop(k, None)

        async def delete(self, k: str) -> None:
            self.store.pop(k, None)

    from src.infrastructure.services.cart import pop_cart, save_cart

    redis = FakeRedis()
    req = PurchaseRequest(
        user_id=7,
        plan_id=3,
        duration_days=30,
        currency=Currency.RUB,
        purchase_type=PurchaseType.CHANGE,
        subscription_id=11,
        traffic_pack_id=2,
    )
    await save_cart(redis, req, ttl_seconds=3600)  # type: ignore[arg-type]
    restored = await pop_cart(redis, 7)  # type: ignore[arg-type]
    assert restored is not None
    assert restored.plan_id == 3 and restored.subscription_id == 11
    # pop is a destructive GETDEL — a second (concurrent) consumer must get nothing.
    assert await pop_cart(redis, 7) is None  # type: ignore[arg-type]
    assert restored.purchase_type is PurchaseType.CHANGE
    assert restored.traffic_pack_id == 2
    assert await pop_cart(redis, 7) is None  # single-use


async def test_trial_carryover_adds_bonus_days_on_change(uow: UnitOfWork) -> None:
    fake = FakeRemnawaveClient()
    subs = SubscriptionService(RemnawaveService(fake))
    async with uow:
        user = await make_user(uow)
        trial_plan, _ = await make_plan(uow, code="trial")
        trial_plan.is_trial = True
        paid_plan, _ = await make_plan(uow, public_code="pro", name="Pro")
        await uow.commit()

        # a trial with 5 days left
        req = PurchaseRequest(
            user_id=user.id,
            plan_id=trial_plan.id,
            duration_days=30,
            currency=Currency.RUB,
            purchase_type=PurchaseType.NEW,
        )
        sub = await subs.grant(uow, user=user, plan=trial_plan, req=req, is_trial=True)
        sub.expire_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=5)
        await uow.commit()

        change_req = PurchaseRequest(
            user_id=user.id,
            plan_id=paid_plan.id,
            duration_days=30,
            currency=Currency.RUB,
            purchase_type=PurchaseType.CHANGE,
            subscription_id=sub.id,
        )
        await subs.change(uow, sub, user=user, plan=paid_plan, req=change_req, carryover_trial=True)
        left = (sub.expire_at - dt.datetime.now(dt.UTC)).days
        assert 34 <= left <= 35  # 30 paid + 5 carried over
        assert sub.is_trial is False

        # without carryover the trial remainder is dropped
        sub.is_trial = True
        sub.expire_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=5)
        await subs.change(
            uow, sub, user=user, plan=paid_plan, req=change_req, carryover_trial=False
        )
        left2 = (sub.expire_at - dt.datetime.now(dt.UTC)).days
        assert 29 <= left2 <= 30


async def test_stale_plan_squad_falls_back_to_synced_available_squad(uow: UnitOfWork) -> None:
    """A deleted panel squad in a frozen plan must not turn provisioning into a 500."""
    fake = FakeRemnawaveClient()
    subs = SubscriptionService(RemnawaveService(fake))
    live_uuid = uuid.uuid4()
    async with uow:
        user = await make_user(uow)
        plan, _ = await make_plan(uow, code="trial-stale")
        plan.internal_squads = [str(uuid.uuid4())]
        uow.session.add(ServerSquad(squad_uuid=live_uuid, display_name="Live squad"))
        await uow.commit()

        sub = await subs.grant(
            uow,
            user=user,
            plan=plan,
            req=PurchaseRequest(
                user_id=user.id,
                plan_id=plan.id,
                duration_days=3,
                currency=Currency.RUB,
            ),
            is_trial=True,
        )

        assert sub.internal_squads == [str(live_uuid)]
        assert next(iter(fake.users.values())).internal_squads == (str(live_uuid),)


async def test_purchase_service_reads_carryover_flag(uow: UnitOfWork) -> None:
    """CHANGE through PurchaseService honours the TRIAL_CARRYOVER_DAYS config."""
    from src.application.services.bot_config import BotConfigService

    fake = FakeRemnawaveClient()
    subs = SubscriptionService(RemnawaveService(fake))
    cfg = BotConfigService()
    purchase = PurchaseService(PricingService(), subs, RecordingEventBus(), config=cfg)
    async with uow:
        user = await make_user(uow, balance_minor=1_000_000)
        trial_plan, _ = await make_plan(uow, code="trial")
        trial_plan.is_trial = True
        paid_plan, _ = await make_plan(uow, public_code="pro", name="Pro", price_minor=30000)
        await uow.commit()

        sub = await subs.grant(
            uow,
            user=user,
            plan=trial_plan,
            req=PurchaseRequest(
                user_id=user.id,
                plan_id=trial_plan.id,
                duration_days=30,
                currency=Currency.RUB,
            ),
            is_trial=True,
        )
        sub.expire_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=7)
        await uow.commit()

        req = PurchaseRequest(
            user_id=user.id,
            plan_id=paid_plan.id,
            duration_days=30,
            currency=Currency.RUB,
            purchase_type=PurchaseType.CHANGE,
            subscription_id=sub.id,
        )
        await purchase.checkout_from_balance(uow, req)
        await uow.commit()
        left = (sub.expire_at - dt.datetime.now(dt.UTC)).days
        assert 36 <= left <= 37  # default TRIAL_CARRYOVER_DAYS is True -> +7
