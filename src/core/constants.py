"""Domain constants and magic numbers, named once."""

from __future__ import annotations

from typing import Final

# --- "unlimited" modelling on the panel (see docs/context/01) --------------
UNLIMITED_TRAFFIC_BYTES: Final = 0  # panel convention: 0 == unlimited traffic
UNLIMITED_EXPIRE_DAYS: Final = 3650  # ~10 years; panel treats far-future expire as unlimited
UNLIMITED_EXPIRE_YEAR: Final = 2099

# --- units -----------------------------------------------------------------
BYTES_PER_GB: Final = 1024**3

# --- identifiers -----------------------------------------------------------
APP_VERSION: Final = "1.15.1"  # bump on release
SHORT_ID_LENGTH: Final = 10  # permanent per-subscription suffix (max column width 16)
REFERRAL_CODE_LENGTH: Final = 8

# --- Remnawave -------------------------------------------------------------
MIN_REMNAWAVE_VERSION: Final = (2, 8, 0)
PANEL_RETRY_ATTEMPTS: Final = 4
PANEL_RETRY_BASE_DELAY: Final = 0.5  # seconds; jittered exponential backoff
# Panel username template for bot-created users: sub_<short_id>. Shared by the
# provisioning service AND the 3.x client (which resolves the numeric user id back
# from this username) — one constant so the two can never drift apart.
PANEL_USERNAME_PREFIX: Final = "sub_"
PANEL_USERNAME_MAX: Final = 34

# --- webhooks / auth -------------------------------------------------------
INITDATA_MAX_AGE_SECONDS: Final = 600  # clock-skew tolerance for Telegram WebApp initData
TELEGRAM_UPDATE_CONCURRENCY: Final = 100  # semaphore slots for webhook update processing

# --- money / discounts -----------------------------------------------------
MAX_DISCOUNT_PERCENT: Final = 100
# Top-up ceiling shared by the cabinet API and the bot's top-up flow — callback data / request
# bodies are forgeable, so both surfaces re-validate against this instead of trusting the client.
MAX_DEPOSIT_AMOUNT_MINOR: Final = 100_000_000  # 1,000,000 RUB

# --- system actor ----------------------------------------------------------
SYSTEM_ACTOR_ID: Final = -1

# --- deep-link prefixes ----------------------------------------------------
REFERRAL_DEEPLINK_PREFIX: Final = "ref_"

# --- redis key templates ---------------------------------------------------
REDIS_PENDING_REFERRAL: Final = "pending_referral:{telegram_id}"
REDIS_WEBHOOK_LOCK: Final = "webhook_lock:{gateway}:{external_id}"
REDIS_TORRENT_BLOCKER_LOCK: Final = "torrent_blocker_lock:{user}:{node}:{ip}"
PENDING_REFERRAL_TTL_SECONDS: Final = 7 * 24 * 3600
