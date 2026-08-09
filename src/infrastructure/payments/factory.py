"""GatewayFactory — maps PaymentGatewayType to its class (ADR-0004).

Registration is a single dict entry. The webhook route and processing pipeline are
gateway-agnostic, so adding a provider never touches them.
"""

from __future__ import annotations

from typing import Any

from src.core.enums import PaymentGatewayType
from src.core.exceptions import GatewayNotConfigured
from src.infrastructure.payments.base import BasePaymentGateway
from src.infrastructure.payments.gateways.antilopay import AntilopayGateway
from src.infrastructure.payments.gateways.aurapay import AurapayGateway
from src.infrastructure.payments.gateways.cispay import CispayGateway
from src.infrastructure.payments.gateways.cloudpayments import CloudpaymentsGateway
from src.infrastructure.payments.gateways.cryptobot import CryptobotGateway
from src.infrastructure.payments.gateways.cryptomus import CryptomusGateway
from src.infrastructure.payments.gateways.freekassa import FreekassaGateway
from src.infrastructure.payments.gateways.heleket import HeleketGateway
from src.infrastructure.payments.gateways.kassa_ai import KassaAiGateway
from src.infrastructure.payments.gateways.lava import LavaGateway
from src.infrastructure.payments.gateways.manual import ManualGateway
from src.infrastructure.payments.gateways.mulenpay import MulenpayGateway
from src.infrastructure.payments.gateways.paypalych import PaypalychGateway
from src.infrastructure.payments.gateways.platega import PlategaGateway
from src.infrastructure.payments.gateways.riopay import RiopayGateway
from src.infrastructure.payments.gateways.robokassa import RobokassaGateway
from src.infrastructure.payments.gateways.rollypay import RollypayGateway
from src.infrastructure.payments.gateways.severpay import SeverpayGateway
from src.infrastructure.payments.gateways.telegram_stars import TelegramStarsGateway
from src.infrastructure.payments.gateways.wata import WataGateway
from src.infrastructure.payments.gateways.yookassa import YookassaGateway
from src.infrastructure.payments.gateways.yoomoney import YoomoneyGateway

# Register a new provider here (plus a core.enums value and a DB seed row).
_REGISTRY: dict[PaymentGatewayType, type[BasePaymentGateway]] = {
    PaymentGatewayType.MANUAL: ManualGateway,
    PaymentGatewayType.TELEGRAM_STARS: TelegramStarsGateway,
    PaymentGatewayType.YOOKASSA: YookassaGateway,
    PaymentGatewayType.CRYPTOBOT: CryptobotGateway,
    PaymentGatewayType.PLATEGA: PlategaGateway,
    PaymentGatewayType.ROBOKASSA: RobokassaGateway,
    PaymentGatewayType.CRYPTOMUS: CryptomusGateway,
    PaymentGatewayType.HELEKET: HeleketGateway,
    PaymentGatewayType.YOOMONEY: YoomoneyGateway,
    PaymentGatewayType.WATA: WataGateway,
    PaymentGatewayType.FREEKASSA: FreekassaGateway,
    PaymentGatewayType.PAYPALYCH: PaypalychGateway,
    PaymentGatewayType.CLOUDPAYMENTS: CloudpaymentsGateway,
    PaymentGatewayType.LAVA: LavaGateway,
    PaymentGatewayType.MULENPAY: MulenpayGateway,
    PaymentGatewayType.KASSA_AI: KassaAiGateway,
    PaymentGatewayType.ROLLYPAY: RollypayGateway,
    PaymentGatewayType.CISPAY: CispayGateway,
    PaymentGatewayType.RIOPAY: RiopayGateway,
    PaymentGatewayType.SEVERPAY: SeverpayGateway,
    PaymentGatewayType.AURAPAY: AurapayGateway,
    PaymentGatewayType.ANTILOPAY: AntilopayGateway,
}


class GatewayFactory:
    """Constructs a gateway instance from its type and (decrypted) settings."""

    def __init__(self, registry: dict[PaymentGatewayType, type[BasePaymentGateway]] | None = None):
        self._registry = registry or dict(_REGISTRY)

    def supported(self) -> frozenset[PaymentGatewayType]:
        return frozenset(self._registry)

    def create(
        self, gateway_type: PaymentGatewayType, settings: dict[str, Any]
    ) -> BasePaymentGateway:
        cls = self._registry.get(gateway_type)
        if cls is None:
            raise GatewayNotConfigured(f"no gateway registered for {gateway_type.value}")
        return cls(settings)
