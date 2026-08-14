"""Composition root — builds the object graph from Settings.

App-lifetime singletons (engine, redis, panel client, gateway factory, services) live here;
a fresh :class:`UnitOfWork` is produced per operation via :meth:`uow`. The web app, the taskiq
worker and ``scripts/smoke.py`` all construct one of these.
"""

from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.application.services.bot_config import BotConfigService
from src.application.services.device_guard import DeviceGuardService
from src.application.services.panel_sync import PanelSyncService
from src.application.services.payment import PaymentService
from src.application.services.pricing import PricingService
from src.application.services.promo import PromoService
from src.application.services.purchase import PurchaseService
from src.application.services.referral import ReferralService
from src.application.services.remnawave import RemnawaveService
from src.application.services.resync import RemnawaveResyncService
from src.application.services.subscription import SubscriptionService
from src.application.services.traffic import TrafficService
from src.core.config import Settings, get_settings
from src.core.constants import APP_VERSION
from src.core.i18n import Translator, load_translations
from src.infrastructure.database.engine import create_engine, create_session_factory
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.events import InProcessEventBus
from src.infrastructure.payments.crypto import SecretBox
from src.infrastructure.payments.factory import GatewayFactory
from src.infrastructure.redis.client import create_redis
from src.infrastructure.remnawave.client import RemnawaveHttpClient
from src.infrastructure.remnawave.connection import build_profile
from src.infrastructure.remnawave.webhook import WebhookVerifier
from src.infrastructure.services.ai_support import AiSupportService
from src.infrastructure.services.mailer import Mailer
from src.infrastructure.services.notification import LogNotifier, TelegramNotifier
from src.infrastructure.services.postback import wire_postback_events
from src.infrastructure.services.reports import wire_report_events
from src.infrastructure.services.telemetry import TelemetryReporter, install_id_from_token
from src.infrastructure.services.user_notifications import wire_user_notifications


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        # --- infrastructure singletons ------------------------------------
        self.engine: AsyncEngine = create_engine(settings)
        self.session_factory: async_sessionmaker[AsyncSession] = create_session_factory(self.engine)
        self.redis: Redis = create_redis(settings)

        self.remnawave_client = RemnawaveHttpClient.from_profile(build_profile(settings.remnawave))
        self.panel_webhook = WebhookVerifier(settings.remnawave.webhook_secret)
        self.gateway_factory = GatewayFactory()
        self.secret_box: SecretBox | None = (
            SecretBox(settings.app.crypt_key) if settings.app.crypt_key else None
        )
        self.event_bus = InProcessEventBus()
        self.translator: Translator = load_translations()
        self.telemetry = TelemetryReporter(
            enabled=settings.telemetry.enabled,
            url=settings.telemetry.url,
            token=settings.telemetry.token,
            app_version=APP_VERSION,
            install_id=install_id_from_token(settings.bot.token),
        )
        # Real Telegram delivery when a bot token is set, otherwise a logging no-op.
        self.notifier: LogNotifier | TelegramNotifier = (
            TelegramNotifier(settings.bot.token, settings.app.owner_ids)
            if settings.bot.token
            else LogNotifier()
        )

        # --- services (stateless singletons) ------------------------------
        self.remnawave = RemnawaveService(self.remnawave_client)
        self.pricing = PricingService()
        self.bot_config = BotConfigService(self.secret_box)
        self.subscriptions = SubscriptionService(self.remnawave)
        self.purchase = PurchaseService(
            self.pricing, self.subscriptions, self.event_bus, config=self.bot_config
        )
        self.referrals = ReferralService(
            self.event_bus, subscriptions=self.subscriptions, config=self.bot_config
        )
        self.payments = PaymentService(
            self.purchase, self.event_bus, self.referrals, notifier=self.notifier
        )
        self.promo = PromoService(self.subscriptions)
        self.panel_sync = PanelSyncService(self.remnawave_client)
        self.device_guard = DeviceGuardService(self.remnawave_client)
        self.resync = RemnawaveResyncService(self.remnawave_client, self.subscriptions)
        self.traffic = TrafficService(self.remnawave_client, cache=self.redis)
        self.ai_support = AiSupportService(
            self.remnawave_client, self.notifier, self.bot_config, self.uow
        )

        # Screen 14: instant report topics (payments/tickets/registrations) listen on the bus.
        wire_report_events(self)
        wire_postback_events(self)  # S2S tracking pixels on registration/trial/purchase
        wire_user_notifications(self)  # user-facing lifecycle DMs (referral reward)

    @classmethod
    def from_env(cls) -> AppContainer:
        return cls(get_settings())

    async def build_mailer(self) -> Mailer:
        """SMTP mailer from hot-reload config (built per call so key changes apply)."""
        async with self.uow() as uow:
            cfg = self.bot_config
            return Mailer(
                host=str(await cfg.value(uow, "SMTP_HOST") or ""),
                port=int(await cfg.value(uow, "SMTP_PORT") or 587),
                user=str(await cfg.value(uow, "SMTP_USER") or ""),
                password=str(await cfg.value(uow, "SMTP_PASSWORD") or ""),
                from_addr=str(await cfg.value(uow, "SMTP_FROM") or ""),
            )

    def uow(self) -> UnitOfWork:
        """A fresh unit of work. Use as ``async with container.uow() as uow: ...``."""
        return UnitOfWork(self.session_factory)

    async def aclose(self) -> None:
        await self.telemetry.aclose()
        await self.notifier.aclose()
        await self.remnawave_client.aclose()
        await self.redis.aclose()
        await self.engine.dispose()
