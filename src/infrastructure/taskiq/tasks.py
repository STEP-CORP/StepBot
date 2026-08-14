"""Background tasks. Import path registered with the worker (see compose.local.yml)."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import TYPE_CHECKING
from uuid import UUID

from src.core.enums import PurchaseType, TransactionStatus, TransactionType
from src.core.logging import get_logger
from src.infrastructure.taskiq.broker import broker, get_container, is_transient_infra

if TYPE_CHECKING:
    from src.infrastructure.di import AppContainer

log = get_logger(__name__)

# Sleeps between in-task fulfilment attempts on a transient failure (panel blip, DB/Redis
# wobble). SimpleRetryMiddleware re-kicks with NO delay, so its 5 attempts alone burn within
# seconds and can't outlive even a 30s panel outage — which then parks the payment until the
# reconciler, i.e. minutes of user-visible delay. Sleeping in-task gives each run ~20s of
# breathing room; the middleware re-kicks multiply that. Durable backstop stays the reconciler.
PROCESS_BACKOFF: tuple[float, ...] = (5.0, 15.0)


async def _process_with_backoff(
    container: AppContainer,
    *,
    payment_id: UUID,
    status: TransactionStatus,
    amount_minor: int | None,
) -> bool:
    """CAS + fulfil with short in-task backoff on transient errors. Each attempt opens a
    fresh UoW, so no DB locks are held across the sleeps. Non-transient errors raise at once."""
    for delay in PROCESS_BACKOFF:
        try:
            async with container.uow() as uow:
                moved = await container.payments.process(
                    uow, payment_id=payment_id, status=status, amount_minor=amount_minor
                )
                await uow.commit()
            return moved
        except Exception as exc:
            if not is_transient_infra(exc):
                raise
            log.warning(
                "process_payment transient failure, retrying in-task",
                payment_id=str(payment_id),
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    async with container.uow() as uow:
        moved = await container.payments.process(
            uow, payment_id=payment_id, status=status, amount_minor=amount_minor
        )
        await uow.commit()
    return moved


async def _deadletter_payment(
    container: object,
    payment_id: UUID,
    status: TransactionStatus,
    *,
    amount_minor: int | None,
    error: Exception,
) -> None:
    """A verified payment could not be fulfilled by a non-transient error. Surface it to admins
    instead of letting taskiq drop it after its retries expire. The transaction is intentionally
    left in place (not forced terminal) so it stays manually recoverable. The alert is deduped per
    payment for 24h so a periodic reconciler re-kick that hits the same error can't spam admins.
    """
    import contextlib
    from typing import cast

    from src.infrastructure.di import AppContainer

    c = cast(AppContainer, container)
    log.error(
        "process_payment dead-letter (non-transient)",
        payment_id=str(payment_id),
        status=status.value,
        error=str(error),
    )
    user_id: int | None = None
    with contextlib.suppress(Exception):
        async with c.uow() as uow:
            txn = await uow.transactions.get_by_payment_id(payment_id)
            user_id = txn.user_id if txn else None

    fresh = True
    with contextlib.suppress(Exception):
        fresh = bool(await c.redis.set(f"pay:deadletter:{payment_id}", "1", nx=True, ex=86400))
    if not fresh:
        return  # already alerted within the last day
    with contextlib.suppress(Exception):
        await c.notifier.notify_admins(
            f"\U0001f6d1 Платёж не удалось провести (payment_id={payment_id}, user_id={user_id}, "
            f"статус={status.value}): {error}. Нужна ручная проверка: деньги могли списаться, "
            f"а подписка не выдана.",
            topic="payments",
        )


@broker.task(retry_on_error=True, max_retries=5)
async def process_payment(
    payment_id: str,
    status: str,
    saved_method_enc: str | None = None,
    saved_method_title: str | None = None,
    amount_minor: int | None = None,
) -> bool:
    """Complete a transaction from a verified webhook (idempotent CAS + fulfilment).

    Enqueued by the payment webhook route; never run inline. Safe to retry — a duplicate
    finds the transaction already terminal and no-ops. Notifies the buyer AFTER commit (only
    the out-of-band webhook path reaches here; in-bot flows reply in the handler themselves).
    ``saved_method_enc`` is a provider-saved card token (Fernet-encrypted by the route) —
    persisted on the paying user for card autopay.
    """
    container = get_container()
    pid = UUID(payment_id)
    try:
        moved = await _process_with_backoff(
            container, payment_id=pid, status=TransactionStatus(status), amount_minor=amount_minor
        )
    except Exception as exc:
        if is_transient_infra(exc):
            raise  # transient: let SimpleRetryMiddleware re-kick; the reconciler is the backstop
        # Non-transient fulfilment error — retrying can't fix it. Dead-letter instead of burning
        # five silent retries and stranding a captured payment unseen: alert admins (deduped so a
        # reconciler re-kick can't spam them) and leave the txn recoverable for a manual re-run.
        await _deadletter_payment(
            container, pid, TransactionStatus(status), amount_minor=amount_minor, error=exc
        )
        return False
    log.info("process_payment", payment_id=payment_id, status=status, advanced=moved)

    if saved_method_enc:  # a duplicate webhook still carries a valid token; store is idempotent
        await _store_saved_method(
            container, pid, method_enc=saved_method_enc, title=saved_method_title
        )
    # Gate the "paid" side-effects on the ACTUAL post-CAS state, not the inbound status:
    # an underpaid webhook advances the txn to FAILED (moved=True) yet status stays
    # "completed" — trusting the string would DM a false "payment received".
    settled_status: TransactionStatus | None = None
    if moved:
        async with container.uow() as uow:
            settled = await uow.transactions.get_by_payment_id(pid)
        settled_status = settled.status if settled is not None else None
    if settled_status is TransactionStatus.COMPLETED:
        await _notify_paid(container, pid)
        await _try_auto_purchase(container, pid)
        # An overpayment admin alert (if any) already fired inside PaymentService.process —
        # the one common layer every settlement path shares (this task, the reconciler, and the
        # bot's on-demand "Проверить оплату"), so it can't depend on which one settled it.
    elif settled_status in (TransactionStatus.FAILED, TransactionStatus.CANCELED):
        # The txn just went terminal-negative — the buyer is otherwise never told.
        await _notify_payment_negative(
            container,
            pid,
            settled=settled_status,
            inbound=TransactionStatus(status),
            received_minor=amount_minor,
        )
    return moved


async def _try_auto_purchase(container: object, payment_id: UUID) -> None:
    """After a top-up credits the wallet, complete a stashed «smart cart» purchase.

    Every outcome talks to the user — the stash was created with an explicit promise
    («подписка оформится сама»), so silence here reads as a broken payment. On failure
    the intent is re-stashed (pop_cart is a destructive GETDEL) and admins are alerted;
    a plain shortfall keeps the cart waiting for the next top-up.
    """
    import contextlib
    from typing import cast

    from src.core.enums import TransactionType
    from src.core.exceptions import InsufficientBalance
    from src.infrastructure.services.cart import pop_cart, save_cart
    from src.web.routes.admin.notifications import notification_text

    c = cast("AppContainer", container)
    async with c.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
        if txn is None or txn.type is not TransactionType.DEPOSIT:
            return
        if not bool(await c.bot_config.value(uow, "AUTO_PURCHASE_AFTER_TOPUP")):
            return
        user_id = txn.user_id
        cart_ttl = int(await c.bot_config.value(uow, "CART_TTL_SECONDS"))
    req = await pop_cart(c.redis, user_id)  # atomic: a concurrent deposit gets None
    if req is None:
        return

    async def _dm(event: str, **values: object) -> None:
        async with c.uow() as uow:
            user = await uow.users.get(user_id)
            telegram_id = user.telegram_id if user else None
            text = await notification_text(uow, event, **values)
        if telegram_id is not None and text:
            with contextlib.suppress(Exception):  # a blocked bot must not kill the flow
                await c.notifier.notify_user(telegram_id, text)

    try:
        async with c.uow() as uow:
            await c.purchase.checkout_from_balance(uow, req)
            await uow.commit()
    except InsufficientBalance:
        # Topped up less than the price — keep the promise alive for the next deposit.
        with contextlib.suppress(Exception):
            await save_cart(c.redis, req, cart_ttl)
        log.info("auto purchase waiting for more balance", user=user_id)
        await _dm("auto_purchase_failed")
        return
    except Exception as exc:
        # Panel/DB failure — the money IS on the balance; tell the user how to finish
        # and give admins the context, instead of destroying the intent silently.
        with contextlib.suppress(Exception):
            await save_cart(c.redis, req, cart_ttl)
        log.warning("auto purchase failed", user=user_id, error=str(exc))
        await _dm("auto_purchase_failed")
        with contextlib.suppress(Exception):
            await c.notifier.notify_admins(
                f"⚠️ Автопокупка после пополнения не удалась (user_id={user_id}, "
                f"plan_id={req.plan_id}): {exc}",
                topic="payments",
            )
        return
    async with c.uow() as uow:
        user = await uow.users.get(user_id)
        sub = (
            await uow.subscriptions.get(user.current_subscription_id)
            if user and user.current_subscription_id
            else None
        )
        telegram_id = user.telegram_id if user else None
        url = sub.subscription_url if sub else None
        plan_name = str((sub.plan_snapshot or {}).get("name") or "") if sub else ""
        expire = sub.expire_at.strftime("%d.%m.%Y") if sub and sub.expire_at else ""
        text = await notification_text(uow, "auto_purchase", plan=plan_name, expire=expire)
    if telegram_id is not None and text:
        if url:
            text += f"\n{url}"
        with contextlib.suppress(Exception):
            await c.notifier.notify_user(telegram_id, text)


async def _lifecycle_dm(c: object, telegram_id: int | None, event: str, **values: object) -> None:
    """DM the user an owner-editable lifecycle template (NOTIF-1). Silent when the owner
    disabled the event or there is no chat. Best-effort — never raises to the caller."""
    if telegram_id is None:
        return
    from typing import cast

    from src.infrastructure.di import AppContainer
    from src.web.routes.admin.notifications import notification_text

    cc = cast(AppContainer, c)
    async with cc.uow() as uow:
        text = await notification_text(uow, event, **values)
    if text:
        await cc.notifier.notify_user(telegram_id, text)


async def _store_saved_method(
    container: object, payment_id: UUID, *, method_enc: str, title: str | None
) -> None:
    """Persist a provider-saved card on the transaction's user (token stays encrypted)."""
    from typing import cast

    from src.infrastructure.di import AppContainer

    c = cast(AppContainer, container)
    async with c.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
        if txn is None:
            return
        user = await uow.users.get(txn.user_id)
        if user is None:
            return
        user.saved_payment_method_id = method_enc
        user.saved_payment_method_title = title
        await uow.commit()
        log.info("saved payment method stored", user_id=user.id, title=title)


async def _notify_payment_negative(
    container: object,
    payment_id: UUID,
    *,
    settled: TransactionStatus,
    inbound: TransactionStatus,
    received_minor: int | None,
) -> None:
    """DM the buyer that a payment ended FAILED/CANCELED. Best-effort, post-commit.

    An inbound COMPLETED that settled as FAILED is the underpayment gate firing — real
    money was taken, so that one also always alerts the admins with both amounts.
    """
    import contextlib
    from typing import cast

    from src.infrastructure.di import AppContainer
    from src.infrastructure.services.reports import fmt_amount
    from src.web.routes.admin.notifications import notification_text

    c = cast(AppContainer, container)
    underpaid = settled is TransactionStatus.FAILED and inbound is TransactionStatus.COMPLETED
    async with c.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
        if txn is None:
            return
        user = await uow.users.get(txn.user_id)
        amount = fmt_amount(txn.amount_minor, txn.currency.value)
        if underpaid:
            paid = (
                fmt_amount(received_minor, txn.currency.value)
                if received_minor is not None
                else "меньше счёта"
            )
            text = await notification_text(uow, "payment_underpaid", paid=paid, expected=amount)
        elif settled is TransactionStatus.CANCELED:
            text = await notification_text(uow, "payment_canceled", amount=amount)
        else:
            text = await notification_text(uow, "payment_failed", amount=amount)
        telegram_id = user.telegram_id if user else None
    if telegram_id is not None and text:
        with contextlib.suppress(Exception):
            await c.notifier.notify_user(telegram_id, text)
    if underpaid:
        # Money was actually received — a human must reconcile/refund it.
        with contextlib.suppress(Exception):
            await c.notifier.notify_admins(
                f"⚠️ Недоплата по счёту {payment_id}: ожидалось {amount}, "
                f"получено {received_minor if received_minor is not None else '?'} "
                f"(minor units). Транзакция переведена в FAILED — нужен возврат/разбор.",
                topic="payments",
            )


async def _notify_overpayment_admins(
    container: object, payment_id: UUID, *, expected_minor: int, received_minor: int
) -> None:
    """Alert admins that a settled webhook reported MORE than the invoice — an editable-sum
    quickpay form (or a manual overpay) let the payer send extra. Symmetric to the underpayment
    alert in ``_notify_payment_negative``, but there is no separate buyer DM here: PaymentService
    already credited the actual amount received (single source of truth for the balance, the
    referral reward and the fiscal receipt), so ``_notify_paid``'s own DM already shows the
    correct, higher figure. This is purely a heads-up for a human to check it wasn't a mistake
    worth refunding — no money is ever left uncredited or unaccounted for.

    No longer called from a production path (superseded by ``PaymentService._alert_overpay``,
    the single common layer every settlement path now shares — see the cap/mutation logic in
    ``PaymentService.process``). Left in place because an existing unit test exercises this
    exact function directly.
    """
    import contextlib
    from typing import cast

    from src.infrastructure.di import AppContainer
    from src.infrastructure.services.reports import fmt_amount

    c = cast(AppContainer, container)
    async with c.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
        if txn is None:
            return
        currency = txn.currency.value
    with contextlib.suppress(Exception):
        await c.notifier.notify_admins(
            f"\U0001f4b8 Переплата по счёту {payment_id}: ожидалось "
            f"{fmt_amount(expected_minor, currency)}, получено "
            f"{fmt_amount(received_minor, currency)}. Зачислена фактически полученная сумма — "
            "проверьте, не ошибся ли плательщик.",
            topic="payments",
        )


async def _notify_paid(container: object, payment_id: UUID) -> None:
    """DM the buyer that an out-of-band payment completed. Best-effort, post-commit."""
    from typing import cast

    from src.infrastructure.di import AppContainer

    c = cast(AppContainer, container)
    async with c.uow() as uow:
        txn = await uow.transactions.get_by_payment_id(payment_id)
        if txn is None:
            return
        user = await uow.users.get(txn.user_id)
        if user is None:
            return
        from src.infrastructure.services.reports import fmt_amount
        from src.web.routes.admin.notifications import notification_text

        sub_url = None
        text: str | None
        if txn.type is TransactionType.DEPOSIT:
            text = await notification_text(
                uow,
                "balance_topup",
                name=user.first_name or "",
                amount=fmt_amount(txn.amount_minor, txn.currency.value),
                balance=fmt_amount(user.balance_minor, txn.currency.value),
            )
        elif txn.type is TransactionType.SUBSCRIPTION_PAYMENT:
            plan_name, expire = "", ""
            if user.current_subscription_id is not None:
                sub = await uow.subscriptions.get(user.current_subscription_id)
                if sub is not None:
                    plan_name = str((sub.plan_snapshot or {}).get("name") or "")
                    expire = sub.expire_at.strftime("%d.%m.%Y") if sub.expire_at else ""
                    sub_url = sub.subscription_url
            # Distinct owner-editable template per purchase kind (NOTIF-1).
            event = "purchase"
            if txn.purchase_type is PurchaseType.RENEW:
                event = "renewal"
            elif txn.purchase_type is PurchaseType.CHANGE:
                event = "plan_changed"
            elif txn.purchase_type is PurchaseType.TRAFFIC_TOPUP:
                event = "traffic_topup"
            text = await notification_text(
                uow, event, name=user.first_name or "", plan=plan_name, expire=expire
            )
            if text is not None and sub_url and event != "traffic_topup":
                text += f"\n{sub_url}"
        else:
            text = "✅ Оплата получена."
        telegram_id = user.telegram_id
        email = user.email

    if telegram_id is not None:
        if text is not None:  # None = owner disabled this notification
            await c.notifier.notify_user(telegram_id, text)
    elif email:  # web / guest buyer has no Telegram — deliver by email
        body = "Спасибо за оплату!"
        if sub_url:
            body += f"\n\nВаша ссылка-подписка:\n{sub_url}\n\nВставьте её в Happ/v2RayTun/Hiddify."
        mailer = await c.build_mailer()
        await mailer.send(email, "VPN — подписка активна", body)


@broker.task(schedule=[{"cron": "* * * * *"}])
async def worker_heartbeat() -> None:
    """Prove the worker is alive: stamp a short-lived Redis key every minute.

    /health/deep flags the worker as down when this key is missing or stale — so a
    silently crash-looping worker (which /health can't see: web stays up) trips an
    external uptime monitor instead of only surfacing via angry customers.
    """
    import contextlib
    import time as _time

    container = get_container()
    with contextlib.suppress(Exception):  # best-effort; never crash-loop the worker on Redis blips
        await container.redis.set("worker:heartbeat", str(int(_time.time())), ex=300)


_RECONCILE_PAGE = 50  # rows per sweep, matches list_stuck_pending's default limit


@broker.task(schedule=[{"cron": "* * * * *"}])
async def reconcile_pending_payments(window_minutes: int | None = None) -> int:
    """Recover paid-but-stuck gateway transactions by polling the provider.

    Covers both real-world failure modes of webhook delivery: the webhook never
    arrived (proxy/CDN ate it, wrong URL) and the fulfilment crashed after we had
    already answered 200 (panel outage). Polling is idempotent — PaymentService
    CAS-guards the transition, so a race with a late webhook is harmless.

    Two-tier cadence in one cron (was */5, i.e. 3-8 min of user-visible delay for a
    stuck payment): most ticks sweep only RECENT txns, so a payment whose fast path
    just died is recovered while the buyer is still looking at the bot, and per-minute
    provider polling stays cheap; roughly every 5th tick widens to the 24h tail (proxy
    ate the webhook hours ago). The wide tier keys off a persisted last-run marker, not
    minute arithmetic — a lock-skip or slow pickup postpones the tail sweep, never loses it.
    """
    import contextlib
    import time
    from uuid import uuid4

    container = get_container()
    # Best-effort non-overlap guard: a degraded sweep (serial polls, 15-20s timeouts each) can
    # outlast its tick, and stacked sweeps would hammer the providers. Correctness never
    # depends on this lock — the CAS transition makes double-polling harmless.
    token = uuid4().hex
    if not await container.redis.set("reconcile:pending:lock", token, nx=True, ex=300):
        log.info("reconcile sweep skipped, lock held")
        return 0
    try:
        full = False
        if window_minutes is None:
            last_full: float | None = None
            with contextlib.suppress(Exception):
                raw = await container.redis.get("reconcile:pending:last_full")
                last_full = float(raw) if raw else None
            full = last_full is None or time.time() - last_full >= 300
            window_minutes = 24 * 60 if full else 30
        recovered = await _reconcile_pending(container, window_minutes, paginate=full)
        if full:
            with contextlib.suppress(Exception):
                await container.redis.set("reconcile:pending:last_full", str(time.time()))
        return recovered
    finally:
        with contextlib.suppress(Exception):
            # Release only OUR lock — a sweep that outlived the TTL must not delete the
            # lock of the sweep that acquired it after expiry.
            if await container.redis.get("reconcile:pending:lock") == token:
                await container.redis.delete("reconcile:pending:lock")


async def _reconcile_pending(
    container: AppContainer, window_minutes: int, *, paginate: bool = False
) -> int:
    import contextlib

    from src.infrastructure.payments.crypto import decrypt_gateway_settings

    now = dt.datetime.now(dt.UTC)
    floor = now - dt.timedelta(minutes=window_minutes)
    newer_than = floor
    if paginate:
        # Walk cursor: a >50-row backlog of abandoned invoices would otherwise pin every
        # wide sweep to the identical oldest-50 page and starve the rest of the window.
        with contextlib.suppress(Exception):
            raw = await container.redis.get("reconcile:pending:cursor")
            if raw:
                cursor = dt.datetime.fromisoformat(raw)
                if cursor > floor:
                    newer_than = cursor
    recovered = 0
    async with container.uow() as uow:
        # 90s age gate: the fast path normally settles in seconds, so anything PENDING
        # this long is genuinely stuck — and the minute cron picks it up on its next tick
        # (was: 3 min gate + */5 cron = 3-8 min of user-visible delay, the exact complaint).
        stuck = await uow.transactions.list_stuck_pending(
            older_than=now - dt.timedelta(seconds=90),
            newer_than=newer_than,
            limit=_RECONCILE_PAGE,
        )
        rows = {g.type: g for g in await uow.payment_gateways.list() if g.is_active}
    if paginate:
        with contextlib.suppress(Exception):
            if len(stuck) == _RECONCILE_PAGE:  # more behind — next wide sweep continues here
                await container.redis.set(
                    "reconcile:pending:cursor", stuck[-1].created_at.isoformat()
                )
            else:  # window exhausted — wrap to its start next time
                await container.redis.delete("reconcile:pending:cursor")
    for txn in stuck:
        gtype = txn.gateway_type
        if gtype is None:  # filtered by the query, but keep mypy honest
            continue
        row = rows.get(gtype)
        if row is None or gtype not in container.gateway_factory.supported():
            continue
        gateway = container.gateway_factory.create(
            gtype, decrypt_gateway_settings(container.secret_box, dict(row.settings))
        )
        try:
            result = await gateway.fetch_status(str(txn.external_id))
        except Exception as exc:
            log.warning("reconcile poll failed", payment_id=str(txn.payment_id), error=str(exc))
            continue
        if result is None or result.status is TransactionStatus.PENDING:
            continue
        if result.saved_method is not None:  # webhook was lost — don't lose the card with it
            if container.secret_box is not None:
                enc = container.secret_box.encrypt(result.saved_method.method_id)
                await _store_saved_method(
                    container, txn.payment_id, method_enc=enc, title=result.saved_method.title
                )
            else:  # fail closed: never persist a raw charge token without a crypt key
                log.warning("reconcile: saved method dropped, APP__CRYPT_KEY not configured")
        # Thread the polled amount through the SAME underpayment gate the live webhook uses, so the
        # amount cross-check is one unavoidable choke point — a gateway that is both reconcilable
        # AND amount-editable (added later) can't slip an underpaid txn through this path. Guard by
        # currency so a foreign-currency amount is never compared against RUB kopeks.
        recovered_minor = (
            result.amount.amount_minor
            if result.amount is not None and result.amount.currency == txn.currency
            else None
        )
        try:
            async with container.uow() as uow:
                moved = await container.payments.process(
                    uow,
                    payment_id=txn.payment_id,
                    status=result.status,
                    amount_minor=recovered_minor,
                )
                await uow.commit()
        except Exception as exc:
            log.warning("reconcile process failed", payment_id=str(txn.payment_id), error=str(exc))
            continue
        if moved:
            recovered += 1
            log.info(
                "payment reconciled",
                payment_id=str(txn.payment_id),
                status=result.status.value,
            )
            # Gate side-effects on the SETTLED status, not the polled one: the underpayment
            # gate can flip an inbound COMPLETED to FAILED (same rule as process_payment).
            async with container.uow() as uow:
                settled_row = await uow.transactions.get_by_payment_id(txn.payment_id)
            settled_status = settled_row.status if settled_row is not None else None
            if settled_status is TransactionStatus.COMPLETED:
                await _notify_paid(container, txn.payment_id)
                # Same post-completion side effects as the live webhook path (tasks.py:48-50):
                # a recovered top-up must still complete its stashed smart-cart purchase (#5).
                await _try_auto_purchase(container, txn.payment_id)
                # An overpayment admin alert (if any) already fired inside PaymentService.process
                # — the same common layer the live webhook task and "Проверить оплату" share, so
                # there is no separate stale-``txn`` expected-amount check to keep in sync here.
            elif settled_status in (TransactionStatus.FAILED, TransactionStatus.CANCELED):
                await _notify_payment_negative(
                    container,
                    txn.payment_id,
                    settled=settled_status,
                    inbound=result.status,
                    received_minor=recovered_minor,
                )
    return recovered


@broker.task(retry_on_error=True, max_retries=5)
async def panel_write_retry(subscription_id: int) -> None:
    """Re-drive a failed panel-first write by reconciling the panel user's enabled state to our
    local status (ADR-0005 retry queue).

    Enqueued when a best-effort panel write lost the race with a panel outage — e.g. a refund
    flips the sub to DISABLED locally but the panel `disable` threw, leaving the refunded user
    still able to connect. Idempotent: re-runs converge. NOTE: retry_on_error re-kicks with NO
    delay (taskiq SimpleRetryMiddleware), so the 5 attempts burn within seconds — they can't
    outlast a multi-minute outage. The durable backstop is ``reconcile_disabled_subs`` below,
    which sweeps locally-DISABLED-but-panel-enabled subs every few minutes regardless.
    """
    container = get_container()
    async with container.uow() as uow:
        sub = await uow.subscriptions.get(subscription_id)
        panel_ref = sub.panel_ref if sub is not None else None
        if sub is None or panel_ref is None:
            return  # gone / never provisioned — nothing to reconcile
        should_be_enabled = sub.status.is_usable
    # Panel-first, outside the DB txn (#1). A raise here re-queues via retry_on_error.
    if should_be_enabled:
        await container.remnawave_client.enable_user(panel_ref)
    else:
        await container.remnawave_client.disable_user(panel_ref)
    log.info("panel_write_retry", subscription_id=subscription_id, enabled=should_be_enabled)


@broker.task(schedule=[{"cron": "*/7 * * * *"}])
async def reconcile_disabled_subs() -> int:
    """Durable backstop: re-disable the panel users of locally-DISABLED subs whose panel user is
    still enabled (a refund/revoke that lost the race with a panel outage). panel_write_retry's
    5 no-delay retries can't survive a real outage; this sweep catches what they miss."""
    container = get_container()
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "REMNAWAVE_RESYNC_ENABLED")):
            return 0
        fixed = await container.resync.reconcile_disabled(uow)
        await uow.commit()
    return fixed


@broker.task(schedule=[{"cron": "0 */6 * * *"}])
async def check_for_updates() -> bool:
    """Every 6h: compare our build SHA to GitHub. If AUTO_UPDATE_ENABLED, install the new
    revision automatically (drops the updater marker); otherwise DM the owners a one-tap
    «Обновить» button. Acts at most once per new revision (Redis dedup)."""
    import contextlib

    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from src.infrastructure.services.updater import (
        _updater_alive,
        check_for_update,
        request_update,
    )

    container = get_container()
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "UPDATE_CHECK_ENABLED")):
            return False
        repo = str(await container.bot_config.value(uow, "UPDATE_REPO") or "")
        branch = str(await container.bot_config.value(uow, "UPDATE_BRANCH") or "main")
        auto = bool(await container.bot_config.value(uow, "AUTO_UPDATE_ENABLED"))
    info = await check_for_update(repo, branch, container.settings.app.build_sha)
    if not info.available or not info.latest:
        return False
    owners = container.settings.app.owner_ids
    token = container.settings.bot.token
    if not owners or not token:
        return False
    # Will an auto-install actually run? Only if enabled AND the updater sidecar is live. This
    # drives the re-notify cadence: a real auto-install re-arms every 6h so a FAILED install
    # retries; a manual notice (incl. auto-enabled-but-no-updater) is shown once per 30 days,
    # not nagged every 6h.
    will_auto = auto and _updater_alive()
    notify_ttl = 6 * 3600 if will_auto else 30 * 86400
    if not await container.redis.set(f"update:notified:{info.latest}", "1", nx=True, ex=notify_ttl):
        return False
    cur = info.current or "?"
    # Drop the marker (guarded by the same liveness check); falls back to the manual button when
    # the updater isn't wired up, so the operator is never left blind.
    auto_started = will_auto and request_update()
    if auto_started:
        text = (
            "🔄 <b>Автообновление запущено</b>\n\n"
            f"Текущая версия: <code>{cur}</code>\nНовая: <code>{info.latest}</code>\n"
            f"{info.message}\n\nБот снимет бэкап БД, обновится и перезапустится — займёт пару "
            "минут. Отключить автоустановку можно в кабинете: Настройки → Безопасность."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 Что нового", url=info.url)]]
        )
    else:
        head = (
            "🆕 <b>Доступно обновление бота</b>\n\n"
            if not auto
            # auto был включён, но модуль updater не подключён — обновляем вручную.
            else "🆕 <b>Доступно обновление бота</b>\n\n⚠️ Автоустановка включена, но модуль "
            "updater не подключён на этом сервере — обновите кнопкой ниже.\n\n"
        )
        text = (
            f"{head}"
            f"Текущая версия: <code>{cur}</code>\nНовая: <code>{info.latest}</code>\n"
            f"{info.message}\n\nНажмите «Обновить», и бот сам скачает и установит новую версию "
            "(снимет бэкап БД, пересоберётся, перезапустится). Займёт пару минут."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить сейчас", callback_data="upd:apply")],
                [InlineKeyboardButton(text="🔗 Что нового", url=info.url)],
            ]
        )
    bot = Bot(token=token)
    try:
        for owner_id in owners:
            with contextlib.suppress(Exception):
                await bot.send_message(owner_id, text, reply_markup=kb, parse_mode="HTML")
    finally:
        await bot.session.close()
    return True


@broker.task(schedule=[{"cron": "17 4 * * *"}])
async def resync_panel() -> int:
    """Nightly bot<->panel reconciliation (heals drift from manual panel edits)."""
    from src.infrastructure.services.reports import send_topic_report

    container = get_container()
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "REMNAWAVE_RESYNC_ENABLED")):
            return 0
        report = await container.resync.resync(uow)
        await uow.commit()
    if report.healed or report.orphaned_local:
        await send_topic_report(
            container,
            "alerts",
            f"🔄 Ночная сверка: проверено {report.checked}, "
            f"восстановлено {report.healed}, пропало из панели {report.orphaned_local}."
            + ("\n" + "\n".join(report.notes[:15]) if report.notes else ""),
        )
    return report.healed


@broker.task(schedule=[{"cron": "*/15 * * * *"}])
async def apply_grace() -> int:
    """«Спасательный круг»: a just-expired PAID subscription gets a short limited-traffic window
    instead of going dark; when the window lapses the user is turned off. Paying (renew) lifts
    grace and restores full limits. Trials never get grace. No-op unless GRACE_ENABLED.

    Runs every 15 min. Panel calls per sub are isolated (one failure doesn't abort the sweep),
    and the whole transition is idempotent: a sub already in grace isn't re-entered, and a
    cooldown stops grace being farmed on every expiry.
    """
    container = get_container()
    now = dt.datetime.now(dt.UTC)
    started: list[tuple[int, int, int, str]] = []  # (telegram_id, days, gb, plan)
    ended: list[tuple[int, str]] = []  # (telegram_id, plan)
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "GRACE_ENABLED")):
            return 0
        days = int(await container.bot_config.value(uow, "GRACE_DAYS") or 0)
        gb = int(await container.bot_config.value(uow, "GRACE_TRAFFIC_GB") or 0)
        cooldown = int(await container.bot_config.value(uow, "GRACE_COOLDOWN_DAYS") or 0)
        if days <= 0:
            return 0
        # Close lapsed windows first, then enrol the freshly expired.
        for sub in await uow.subscriptions.grace_expired(now, limit=500):
            user = await uow.users.get(sub.user_id)
            try:
                await container.subscriptions.end_grace(uow, sub)
            except Exception as exc:
                log.warning("grace end failed", sub=sub.id, error=str(exc))
                continue
            if user and user.telegram_id:
                ended.append((user.telegram_id, str((sub.plan_snapshot or {}).get("name") or "")))
        cutoff = now - dt.timedelta(days=cooldown)
        for sub in await uow.subscriptions.grace_candidates(
            now, lookback=dt.timedelta(days=2), cooldown_cutoff=cutoff, limit=500
        ):
            user = await uow.users.get(sub.user_id)
            try:
                await container.subscriptions.enter_grace(
                    uow,
                    sub,
                    days=days,
                    traffic_gb=gb,
                    telegram_id=user.telegram_id if user else None,
                )
            except Exception as exc:
                log.warning("grace enter failed", sub=sub.id, error=str(exc))
                continue
            if user and user.telegram_id:
                started.append(
                    (user.telegram_id, days, gb, str((sub.plan_snapshot or {}).get("name") or ""))
                )
        await uow.commit()
    for tg, d, g, plan in started:
        await _lifecycle_dm(container, tg, "grace_started", days=d, gb=g, plan=plan)
    for tg, plan in ended:
        await _lifecycle_dm(container, tg, "grace_ended", plan=plan)
    if started or ended:
        log.info("grace applied", entered=len(started), ended=len(ended))
    return len(started) + len(ended)


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def issue_nalogo_receipts() -> int:
    """Register unreceipted paid subscriptions as income in «Мой налог» (retry queue)."""
    import datetime as dt

    from src.infrastructure.services.nalogo import NalogoClient

    container = get_container()
    async with container.uow() as uow:
        cfg = container.bot_config
        if not bool(await cfg.value(uow, "NALOGO_ENABLED")):
            return 0
        inn = str(await cfg.value(uow, "NALOGO_INN") or "")
        token = str(await cfg.value(uow, "NALOGO_TOKEN") or "")
        service = str(await cfg.value(uow, "NALOGO_SERVICE_NAME") or "Доступ к VPN-сервису")
        if not inn or not token:
            return 0
        pending = await uow.transactions.list_unreceipted(
            newer_than=dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
        )
    if not pending:
        return 0

    client = NalogoClient(inn, token, service)
    from src.infrastructure.database.models.transaction import Transaction

    issued = 0
    for txn in pending:
        name = str((txn.plan_snapshot or {}).get("name") or service)
        # Claim the slot BEFORE the irreversible fiscal call: stamp receipt_created_at under a row
        # lock so a crash between register_income and the receipt_uuid write can't re-file next
        # run (list_unreceipted skips claimed rows). Un-claim on a transient error to retry.
        telegram_id = None
        async with container.uow() as uow:
            row = await uow.session.get(Transaction, txn.id, with_for_update=True)
            if row is None or row.receipt_created_at is not None:
                continue  # already claimed / filed by a concurrent run
            row.receipt_created_at = dt.datetime.now(dt.UTC)
            await uow.commit()
        try:
            receipt_id = await client.register_income(txn.amount_minor, name=name)
        except Exception as exc:
            # ANY failure must un-claim, not just NalogoError: register_income does res.json(),
            # which raises JSONDecodeError (not NalogoError) on a 200-with-HTML maintenance page —
            # that would escape, crash the run, and orphan the claimed row forever (never re-filed,
            # income never registered with the tax office). Un-claim + continue instead.
            log.warning("nalogo receipt deferred", payment_id=str(txn.payment_id), error=str(exc))
            async with container.uow() as uow:  # un-claim so a transient failure retries
                row = await uow.transactions.get_by_payment_id(txn.payment_id)
                if row is not None:
                    row.receipt_created_at = None
                    await uow.commit()
            continue
        async with container.uow() as uow:
            row = await uow.transactions.get_by_payment_id(txn.payment_id)
            if row is not None:
                # receipt_uuid holds the public print URL (the id is embedded in it).
                row.receipt_uuid = receipt_id[:64]
                await uow.commit()
                user = await uow.users.get(row.user_id)
                telegram_id = user.telegram_id if user else None
        issued += 1
        if len(receipt_id) > 8 and telegram_id is not None:
            await container.notifier.notify_user(telegram_id, f"🧾 Чек по оплате: {receipt_id}")
    log.info("nalogo receipts issued", count=issued)
    return issued


@broker.task(schedule=[{"cron": "*/2 * * * *"}])
async def panel_watchdog() -> str:
    """Auto-maintenance: 3 failed panel pings in a row -> maintenance ON; recovery -> OFF.

    Only the flag WE set is auto-lifted (Redis marker), so a manually enabled
    maintenance mode is never touched.
    """
    container = get_container()
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "AUTO_MAINTENANCE_ENABLED")):
            return "off"

    from src.infrastructure.services.reports import send_topic_report

    fails_key, auto_key = "panelwd:fails", "panelwd:auto"
    try:
        await container.remnawave_client.get_version()
        panel_ok = True
    except Exception:
        panel_ok = False

    if panel_ok:
        await container.redis.delete(fails_key)
        if await container.redis.get(auto_key):
            await container.redis.delete(auto_key)
            async with container.uow() as uow:
                await container.bot_config.set_values(uow, {"MAINTENANCE_MODE": False})
                await uow.commit()
            await send_topic_report(
                container, "alerts", "✅ Панель снова отвечает — техрежим снят автоматически."
            )
            return "recovered"
        return "ok"

    fails = int(await container.redis.incr(fails_key))
    await container.redis.expire(fails_key, 1800)
    if fails != 3:  # alert exactly once, on the third consecutive failure
        return f"fail#{fails}"
    async with container.uow() as uow:
        already_on = bool(await container.bot_config.value(uow, "MAINTENANCE_MODE"))
        if not already_on:
            await container.bot_config.set_values(uow, {"MAINTENANCE_MODE": True})
            await uow.commit()
            await container.redis.set(auto_key, "1", ex=86400)
    await send_topic_report(
        container,
        "alerts",
        "🚨 Панель Remnawave не отвечает 3 проверки подряд — "
        + ("включён режим техработ." if not already_on else "техрежим уже был включён вручную."),
    )
    return "maintenance"


@broker.task(schedule=[{"cron": "*/20 * * * *"}])
async def device_guard_scan() -> int:
    """Sharing detection: unique online IPs per subscription vs the device limit.

    Uses the panel's ip-control API per ONLINE node; violations go to the «alerts»
    report topic. A Redis cooldown keeps it to one alert per subscription per day.
    """
    from src.application.services.device_guard import GuardConfig
    from src.core.enums import ServerNodeStatus
    from src.infrastructure.services.reports import send_topic_report

    container = get_container()
    async with container.uow() as uow:
        cfg_svc = container.bot_config
        if not bool(await cfg_svc.value(uow, "DEVICE_GUARD_ENABLED")):
            return 0
        cfg = GuardConfig(
            max_ips=int(await cfg_svc.value(uow, "DEVICE_GUARD_MAX_IPS") or 0),
            tolerance=int(await cfg_svc.value(uow, "DEVICE_GUARD_TOLERANCE") or 0),
            action=str(await cfg_svc.value(uow, "DEVICE_GUARD_ACTION") or "alert"),
        )
        nodes = [
            str(n.node_uuid)
            for n in await uow.server_nodes.list()
            if n.status is ServerNodeStatus.ONLINE
        ]
    if not nodes:
        return 0

    usage = await container.device_guard.collect_ips(nodes)
    if not usage:
        return 0

    async with container.uow() as uow:
        violations = await container.device_guard.scan(uow, usage, cfg)
        await uow.commit()

    reported = 0
    for v in violations:
        # one alert per subscription per day — repeated scans must not spam the topic
        dedup_key = f"devguard:alerted:{v.subscription_id}"
        if not await container.redis.set(dedup_key, "1", ex=86400, nx=True):
            continue
        reported += 1
        who = f"tg {v.telegram_id}" if v.telegram_id else f"user #{v.user_id}"
        await send_topic_report(
            container,
            "alerts",
            f"👥 Похоже на шеринг подписки #{v.subscription_id} ({who})\n"
            f"Тариф: {v.plan_name} · лимит {v.limit} устройств\n"
            f"Онлайн-IP ({len(v.ips)}): {', '.join(v.ips)}\n"
            f"Действие: {v.action}",
        )
        if v.action == "disable" and v.telegram_id is not None:
            await container.notifier.notify_user(
                v.telegram_id,
                "⚠️ Подписка приостановлена: замечено одновременное использование "
                "на слишком многих устройствах. Напиши в поддержку, если это ошибка.",
            )
    log.info("device guard scan", violations=len(violations), reported=reported)
    return len(violations)


@broker.task(schedule=[{"cron": "*/15 * * * *"}])
async def sync_panel_nodes() -> int:
    """Mirror Remnawave nodes into server_nodes (screen 12 + dashboard online)."""
    container = get_container()
    async with container.uow() as uow:
        try:
            synced = await container.panel_sync.sync_nodes(uow)
        except Exception as exc:
            # repr(), not str(): a bare exception (e.g. a timeout) has an empty message and
            # used to log `error=""` — useless. repr keeps the type + any detail.
            log.warning("panel nodes sync failed", error=repr(exc))
            return -1
        await uow.commit()
    log.info("panel nodes synced", count=synced)
    return synced


def _msk_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)


def _time_matches(target_hhmm: str, now: dt.datetime, window_minutes: int = 5) -> bool:
    """True when `now` (MSK) is within [target, target+window)."""
    try:
        hh, mm = (int(x) for x in target_hhmm.split(":"))
    except ValueError:
        return False
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return target <= now < target + dt.timedelta(minutes=window_minutes)


def _human_hours(hours: int) -> str:
    """Russian noun for the {time} placeholder: '24 часа' / '1 час' / '12 часов'."""
    if hours <= 0:
        return "момент"
    if 11 <= hours % 100 <= 14:
        word = "часов"
    elif hours % 10 == 1:
        word = "час"
    elif 2 <= hours % 10 <= 4:
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


# Superseded by the hour-based send_expiry_reminders (ReminderStep ladder). Left un-scheduled
# for backward compatibility; the day-based smart_reminder screen no longer fires on its own.
@broker.task
async def send_smart_reminders() -> int:
    """Renewal reminders: users whose subscription expires in N days (config days CSV).

    Runs every 5 minutes; fires only when MSK time enters the configured window.
    Per-user daily dedup via Redis SETNX.
    """
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from sqlalchemy import select

    from src.core.enums import SubscriptionStatus
    from src.infrastructure.database.models.subscription import Subscription
    from src.infrastructure.database.models.user import User

    container = get_container()
    now_msk = _msk_now()
    async with container.uow() as uow:
        reminder = await uow.smart_reminder.get_or_create()
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        await uow.commit()
        if not reminder.enabled or not _time_matches(reminder.send_time, now_msk):
            return 0
        day_offsets = [int(x) for x in reminder.days_before.split(",") if x.strip().isdigit()]
        if not day_offsets:
            return 0

        targets: list[tuple[int, int]] = []  # (telegram_id, days_left)
        for offset in day_offsets:
            day_start = (now_msk + dt.timedelta(days=offset)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - dt.timedelta(hours=3)  # back to UTC
            day_end = day_start + dt.timedelta(days=1)
            rows = (
                await uow.session.execute(
                    select(User.telegram_id)
                    .join(Subscription, Subscription.id == User.current_subscription_id)
                    .where(
                        User.telegram_id.is_not(None),
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]
                        ),
                        Subscription.expire_at >= day_start,
                        Subscription.expire_at < day_end,
                    )
                )
            ).all()
            targets.extend((int(tg), offset) for (tg,) in rows)

    if not targets:
        return 0
    markup = None
    if reminder.button_enabled and miniapp_url.startswith("https://"):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Продлить", web_app=WebAppInfo(url=miniapp_url))]
            ]
        )
    sent = 0
    today = now_msk.strftime("%Y%m%d")
    bot = Bot(token=container.settings.bot.token)
    try:
        for tg_id, days in targets:
            if not await container.redis.set(f"reminder:{today}:{tg_id}", "1", nx=True, ex=86400):
                continue  # already reminded today
            try:
                await bot.send_message(
                    tg_id, reminder.text.replace("{days}", str(days)), reply_markup=markup
                )
                sent += 1
            except Exception:
                pass
    finally:
        await bot.session.close()
    log.info("smart reminders sent", count=sent)
    return sent


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def send_expiry_reminders() -> int:
    """Hour-based expiry reminders (screen 08): for each enabled ReminderStep, message
    subscribers whose expiry enters the step window, once per subscription per step.

    Runs every 5 min; a 15-min look-back window plus per-(step, sub-expiry) Redis SETNX
    means a step never misses or double-fires. ``hours_before == 0`` fires just after expiry.
    Text placeholders: {hours} {time} {plan}.
    """
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from sqlalchemy import select

    from src.core.enums import SubscriptionStatus
    from src.infrastructure.database.models.subscription import Subscription
    from src.infrastructure.database.models.user import User

    container = get_container()
    now = dt.datetime.now(dt.UTC)
    lookback = dt.timedelta(minutes=15)
    live = [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]
    at_expiry = [*live, SubscriptionStatus.LIMITED, SubscriptionStatus.EXPIRED]

    # (hours_before, text, button_enabled, telegram_id, expire_ts, plan_name)
    targets: list[tuple[int, str, bool, int, int, str]] = []
    async with container.uow() as uow:
        steps = [s for s in await uow.reminders.ordered() if s.enabled]
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")
        await uow.commit()
        for step in steps:
            boundary = now + dt.timedelta(hours=step.hours_before)
            rows = (
                await uow.session.execute(
                    select(User.telegram_id, Subscription.expire_at, Subscription.plan_snapshot)
                    .join(Subscription, Subscription.id == User.current_subscription_id)
                    .where(
                        User.telegram_id.is_not(None),
                        Subscription.status.in_(live if step.hours_before > 0 else at_expiry),
                        Subscription.expire_at > boundary - lookback,
                        Subscription.expire_at <= boundary,
                    )
                )
            ).all()
            for tg, exp, snap in rows:
                targets.append(
                    (
                        step.hours_before,
                        step.text,
                        step.button_enabled,
                        int(tg),
                        int(exp.timestamp()),
                        str((snap or {}).get("name") or ""),
                    )
                )

    if not targets:
        return 0
    sent = 0
    bot = Bot(token=container.settings.bot.token)
    try:
        for hours, text, button, tg_id, exp_ts, plan in targets:
            if not await container.redis.set(
                f"exprem:{hours}:{tg_id}:{exp_ts}", "1", nx=True, ex=604800
            ):
                continue  # already sent this step for this expiry
            body = (
                text.replace("{hours}", str(hours))
                .replace("{time}", _human_hours(hours))
                .replace("{plan}", plan)
            )
            markup = None
            if button and miniapp_url.startswith("https://"):
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Продлить", web_app=WebAppInfo(url=miniapp_url))]
                    ]
                )
            try:
                await bot.send_message(tg_id, body, reply_markup=markup)
                sent += 1
            except Exception:
                pass
    finally:
        await bot.session.close()
    log.info("expiry reminders sent", count=sent)
    return sent


@broker.task(schedule=[{"cron": "13 3 * * *"}])
async def snapshot_traffic() -> int:
    """Daily: record each active subscription's cumulative traffic use for the usage graph."""
    from sqlalchemy import select

    from src.core.enums import SubscriptionStatus
    from src.infrastructure.database.models.subscription import Subscription

    container = get_container()
    day = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    recorded = 0
    async with container.uow() as uow:
        subs = (
            await uow.session.scalars(
                select(Subscription).where(
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.ACTIVE,
                            SubscriptionStatus.TRIAL,
                            SubscriptionStatus.LIMITED,
                        ]
                    )
                )
            )
        ).all()
        for sub in subs:
            await uow.traffic.upsert(sub.id, day, sub.traffic_used_bytes)
            recorded += 1
        await uow.commit()
    log.info("traffic snapshots recorded", count=recorded)
    return recorded


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def send_winback_offers() -> int:
    """Win-back funnel (screen 08): N days after a subscription expired, message the user
    and grant a one-shot purchase discount (consumed by PricingService on the next buy).

    Runs every 5 minutes; an enabled step fires when MSK time enters its window and
    targets users whose current subscription expired exactly ``offset_days`` MSK-days
    ago — so each user walks the funnel rung by rung. Per-user daily dedup via Redis
    SETNX, mirroring send_smart_reminders.
    """
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    from sqlalchemy import select

    from src.core.enums import SubscriptionStatus, UserStatus
    from src.infrastructure.database.models.subscription import Subscription
    from src.infrastructure.database.models.user import User
    from src.infrastructure.database.models.winback_step import WinbackStep

    container = get_container()
    now_msk = _msk_now()
    async with container.uow() as uow:
        steps = [
            s
            for s in await uow.winback_steps.ordered()
            if s.enabled and _time_matches(s.send_time, now_msk)
        ]
        if not steps:
            return 0
        miniapp_url = str(await container.bot_config.value(uow, "SUBSCRIPTION_MINI_APP_URL") or "")

        targets: list[tuple[int, int, WinbackStep]] = []  # (user_id, telegram_id, step)
        for step in steps:
            day_start = (now_msk - dt.timedelta(days=step.offset_days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - dt.timedelta(hours=3)  # back to UTC
            day_end = day_start + dt.timedelta(days=1)
            rows = (
                await uow.session.execute(
                    select(User.id, User.telegram_id)
                    .join(Subscription, Subscription.id == User.current_subscription_id)
                    .where(
                        User.telegram_id.is_not(None),
                        User.status == UserStatus.ACTIVE,
                        Subscription.status == SubscriptionStatus.EXPIRED,
                        Subscription.expire_at >= day_start,
                        Subscription.expire_at < day_end,
                    )
                )
            ).all()
            targets.extend((int(uid), int(tg), step) for uid, tg in rows)

    if not targets:
        return 0
    markup = None
    if miniapp_url.startswith("https://"):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Вернуться", web_app=WebAppInfo(url=miniapp_url))]
            ]
        )
    sent = 0
    today = now_msk.strftime("%Y%m%d")
    bot = Bot(token=container.settings.bot.token)
    try:
        for user_id, tg_id, step in targets:
            if not await container.redis.set(f"winback:{today}:{tg_id}", "1", nx=True, ex=86400):
                continue  # one win-back message per user per day
            if step.discount_pct > 0:
                async with container.uow() as uow:
                    user = await uow.users.get(user_id)
                    if user is None:
                        continue
                    # max, not assign: don't clobber a bigger one-shot promo discount
                    user.purchase_discount_pct = max(user.purchase_discount_pct, step.discount_pct)
                    await uow.commit()
            try:
                await bot.send_message(
                    tg_id,
                    step.text.replace("{discount}", str(step.discount_pct)),
                    reply_markup=markup,
                )
                sent += 1
            except Exception:
                pass
    finally:
        await bot.session.close()
    log.info("winback offers sent", count=sent)
    return sent


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def send_holiday_promos() -> int:
    """Holiday-calendar promos (screen 08): on the day, at the configured time."""
    from aiogram import Bot
    from sqlalchemy import select

    from src.core.enums import UserStatus
    from src.infrastructure.database.models.user import User

    container = get_container()
    now_msk = _msk_now()
    async with container.uow() as uow:
        holidays = [
            h
            for h in await uow.holidays.ordered()
            if h.enabled
            and h.month == now_msk.month
            and h.day == now_msk.day
            and _time_matches(h.send_time, now_msk)
        ]
        if not holidays:
            return 0
        rows = (
            await uow.session.execute(
                select(User.telegram_id).where(
                    User.telegram_id.is_not(None), User.status == UserStatus.ACTIVE
                )
            )
        ).all()
        chat_ids = [int(tg) for (tg,) in rows]

    sent_total = 0
    bot = Bot(token=container.settings.bot.token)
    try:
        for h in holidays:
            key = f"holiday:{now_msk.year}:{h.id}"
            if not await container.redis.set(key, "1", nx=True, ex=86400 * 2):
                continue  # already sent this year
            if h.reward_type.value == "discount":
                text = f"🎉 {h.name}! Скидка -{h.value}% на продление — только сегодня!"
            elif h.reward_type.value == "days":
                text = f"🎉 {h.name}! Дарим +{h.value} дней при продлении сегодня!"
            else:
                text = f"🎉 {h.name}! Бонус {h.value / 100:.0f} ₽ на баланс за продление сегодня!"
            import asyncio

            from aiogram.exceptions import TelegramRetryAfter

            sent = 0
            for chat_id in chat_ids:
                try:
                    await bot.send_message(chat_id, text)
                    sent += 1
                except TelegramRetryAfter as exc:  # flood control — wait and retry once
                    await asyncio.sleep(exc.retry_after)
                    try:
                        await bot.send_message(chat_id, text)
                        sent += 1
                    except Exception:
                        pass
                except Exception:
                    pass

                await asyncio.sleep(0.04)
            sent_total += sent
            async with container.uow() as uow:
                row = await uow.holidays.get(h.id)
                if row is not None:
                    results = dict(row.results)
                    results[str(now_msk.year)] = {"sent": sent}
                    row.results = results
                    await uow.commit()
    finally:
        await bot.session.close()
    log.info("holiday promos sent", count=sent_total)
    return sent_total


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def scheduled_backup() -> None:
    """Fires run_backup at the configured BACKUP_TIME (daily, Redis dedup)."""
    container = get_container()
    now_msk = _msk_now()
    async with container.uow() as uow:
        enabled = bool(await container.bot_config.value(uow, "BACKUP_ENABLED"))
        at = str(await container.bot_config.value(uow, "BACKUP_TIME"))
    if not enabled or not _time_matches(at, now_msk):
        return
    if not await container.redis.set(
        f"backup:{now_msk.strftime('%Y%m%d')}", "1", nx=True, ex=86400
    ):
        return
    await run_backup.kiq()


@broker.task
async def run_backup() -> str | None:
    """Dump the Postgres DB to an encrypted zip in ./backups (pg_dump + pyzipper).

    Returns the archive path, or None when pg_dump is unavailable/failed. When the
    «backups» report topic is enabled, the archive is delivered into the report group.
    """
    import asyncio
    import os
    from pathlib import Path

    from src.infrastructure.services.backup import create_encrypted_zip, prune_old_backups

    container = get_container()
    db = container.settings.database
    backups = Path("backups")
    backups.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")
    dump_path = backups / f"db_{stamp}.sql"

    try:
        # Pass the password via PGPASSWORD env, NOT on argv (argv is world-readable via
        # /proc/<pid>/cmdline and `ps` to anything sharing the container's PID namespace).
        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--no-owner",
            "--format=plain",
            f"--file={dump_path}",
            "--host",
            db.host,
            "--port",
            str(db.port),
            "--username",
            db.user,
            db.name,
            env={**os.environ, "PGPASSWORD": db.password},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        # pg_dump binary missing (postgresql-client not installed). Fail soft as the
        # docstring promises instead of crashing the scheduled task on every run.
        log.warning("run_backup skipped: pg_dump not found — install postgresql-client")
        dump_path.unlink(missing_ok=True)
        return None
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error("run_backup pg_dump failed", stderr=stderr.decode()[:400])
        dump_path.unlink(missing_ok=True)
        return None

    async with container.uow() as uow:
        password = str(await container.bot_config.value(uow, "BACKUP_ENCRYPTION_PASSWORD") or "")
        # Never below 1 — keep=0 would slice [0:] and delete every archive, including the one
        # just made, on each run.
        keep = max(1, int(await container.bot_config.value(uow, "BACKUP_KEEP_LAST")))
    out = backups / f"backup_{stamp}.zip"
    # Local disk copy always exists (fall back to a derived key for at-rest encryption).
    create_encrypted_zip([dump_path], out, password or container.settings.app.jwt_secret[:16])
    dump_path.unlink(missing_ok=True)
    prune_old_backups(backups, keep)
    log.info("run_backup done", path=str(out))

    # Shipping the FULL database (emails, password hashes, saved-card tokens, balances) into a
    # Telegram group is only safe under a DEDICATED strong key — never the reused JWT signing
    # secret. Without BACKUP_ENCRYPTION_PASSWORD we keep the local copy but don't send it off-box.
    if not password:
        log.warning(
            "run_backup: BACKUP_ENCRYPTION_PASSWORD unset — local backup kept, DB dump NOT sent "
            "to Telegram (set a dedicated backup password to enable off-box delivery)"
        )
        return str(out)
    from src.infrastructure.services.reports import send_topic_report

    await send_topic_report(container, "backups", f"💾 Бэкап БД · {stamp}", document=out)
    return str(out)


_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAYS |= {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def _parse_weekly_schedule(raw: str) -> tuple[int | None, str]:
    """'Mon 10:00' → (0, '10:00'); bare 'HH:MM' → (None, 'HH:MM') = any day."""
    parts = raw.strip().split()
    if len(parts) == 2 and parts[0].lower()[:3] in _WEEKDAYS:
        return _WEEKDAYS[parts[0].lower()[:3]], parts[1]
    return None, parts[-1] if parts else ""


async def _summary_text(container: AppContainer, title: str, *, hours: int) -> str:
    """KPI block over the trailing window. Revenue definition mirrors the dashboard:
    external money only — balance-funded purchases are internal transfers."""
    from sqlalchemy import func, select

    from src.core.enums import SubscriptionStatus
    from src.infrastructure.database.models.subscription import Subscription
    from src.infrastructure.database.models.ticket import Ticket
    from src.infrastructure.database.models.transaction import Transaction
    from src.infrastructure.database.models.user import User
    from src.infrastructure.services.reports import fmt_amount
    from src.web.routes.admin.dashboard import _EXTERNAL_MONEY

    start = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)
    paid = (
        Transaction.status == TransactionStatus.COMPLETED,
        _EXTERNAL_MONEY,
        Transaction.completed_at >= start,
        Transaction.is_test.is_(False),
    )
    async with container.uow() as uow:
        s = uow.session
        new_users = int(
            await s.scalar(select(func.count()).select_from(User).where(User.created_at >= start))
            or 0
        )
        trials = int(
            await s.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.is_trial.is_(True), Subscription.created_at >= start)
            )
            or 0
        )
        payments = int(
            await s.scalar(select(func.count()).select_from(Transaction).where(*paid)) or 0
        )
        revenue = int(
            await s.scalar(
                select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(*paid)
            )
            or 0
        )
        active = int(
            await s.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.ACTIVE,
                            SubscriptionStatus.TRIAL,
                            SubscriptionStatus.LIMITED,
                        ]
                    )
                )
            )
            or 0
        )
        tickets = int(
            await s.scalar(
                select(func.count()).select_from(Ticket).where(Ticket.created_at >= start)
            )
            or 0
        )
    return (
        f"<b>{title}</b>\n\n"
        f"👥 Новых пользователей: <b>{new_users}</b>\n"
        f"🧪 Триалов выдано: <b>{trials}</b>\n"
        f"💳 Платежей: <b>{payments}</b> на <b>{fmt_amount(revenue)}</b>\n"
        f"📦 Активных подписок: <b>{active}</b>\n"
        f"🎫 Тикетов открыто: <b>{tickets}</b>"
    )


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def send_periodic_reports() -> int:
    """Daily/weekly summaries into the report group topics (screen 14).

    Runs every 5 minutes; an enabled topic fires when MSK time enters its schedule
    window ("21:00" / "Mon 10:00" from report_topics.schedule), once per period
    (Redis SETNX dedup). Instant topics are event-driven — see services/reports.py.
    """
    from src.infrastructure.services.reports import send_topic_report

    container = get_container()
    now_msk = _msk_now()
    async with container.uow() as uow:
        topics = {t.code: (t.enabled, t.schedule) for t in await uow.report_topics.list()}

    sent = 0
    enabled, schedule = topics.get("daily_report", (False, None))
    if (
        enabled
        and _time_matches(schedule or "21:00", now_msk)
        and await container.redis.set(f"report:daily:{now_msk:%Y%m%d}", "1", nx=True, ex=86400)
    ):
        text = await _summary_text(container, f"📊 Отчёт за сутки · {now_msk:%d.%m.%Y}", hours=24)
        if await send_topic_report(container, "daily_report", text):
            sent += 1

    enabled, schedule = topics.get("weekly_report", (False, None))
    if enabled:
        day, hhmm = _parse_weekly_schedule(schedule or "Mon 10:00")
        if (day is None or now_msk.weekday() == day) and _time_matches(hhmm, now_msk):
            iso = now_msk.isocalendar()
            key = f"report:weekly:{iso.year}{iso.week:02d}"
            if await container.redis.set(key, "1", nx=True, ex=86400 * 8):
                text = await _summary_text(
                    container, f"📈 Отчёт за неделю · {now_msk:%d.%m.%Y}", hours=24 * 7
                )
                if await send_topic_report(container, "weekly_report", text):
                    sent += 1
    if sent:
        log.info("periodic reports sent", count=sent)
    return sent


@broker.task
async def send_broadcast(broadcast_id: int) -> None:
    """Deliver a broadcast to its audience, updating live progress counters.

    Uses a bare aiogram ``Bot`` (no dispatcher) so the worker doesn't need the bot
    process. Progress is committed in batches so the cabinet's polling sees it grow;
    per-user failures (blocked bot, deactivated account) increment ``failed``.
    """
    import asyncio
    from pathlib import Path

    from aiogram import Bot
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

    from src.bot.keyboards import style_for_hex
    from src.core.enums import BroadcastMedia, BroadcastStatus
    from src.web.routes.admin.broadcasts import audience_stmt

    container = get_container()
    async with container.uow() as uow:
        b = await uow.broadcasts.get(broadcast_id)
        if b is None or b.status not in (BroadcastStatus.PENDING, BroadcastStatus.RUNNING):
            return
        b.status = BroadcastStatus.RUNNING
        b.started_at = dt.datetime.now(dt.UTC)
        chat_ids = [
            int(tg) for (tg,) in (await uow.session.execute(audience_stmt(b.audience))).all()
        ]
        text, button_enabled = b.text, b.button_enabled
        button_text, button_url = b.button_text, b.button_url
        button_action, button_color = b.button_action, b.button_color
        media, media_path, emoji_id = b.media, b.media_path, b.emoji_id
        await uow.commit()

    # Premium custom emoji renders via <tg-emoji>; falls back to the plain glyph.
    if emoji_id:
        text = f'<tg-emoji emoji-id="{emoji_id}">⭐</tg-emoji> {text}'

    markup = None
    if button_enabled and button_text and (button_url or button_action):
        kwargs: dict[str, object] = {"text": button_text}
        if button_url:
            kwargs["url"] = button_url
        else:
            kwargs["callback_data"] = f"act:{button_action}:0"
        style = style_for_hex(button_color)
        if style:
            kwargs["style"] = style
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(**kwargs)]]  # type: ignore[arg-type]
        )

    media_file = None
    if media is not BroadcastMedia.TEXT and media_path and Path(media_path).is_file():
        media_file = FSInputFile(media_path)

    async def deliver(chat_id: int) -> None:
        if media_file is not None and media is BroadcastMedia.PHOTO:
            await bot.send_photo(
                chat_id, media_file, caption=text, reply_markup=markup, parse_mode="HTML"
            )
        elif media_file is not None and media is BroadcastMedia.VIDEO:
            await bot.send_video(
                chat_id, media_file, caption=text, reply_markup=markup, parse_mode="HTML"
            )
        elif media_file is not None and media is BroadcastMedia.GIF:
            await bot.send_animation(
                chat_id, media_file, caption=text, reply_markup=markup, parse_mode="HTML"
            )
        else:
            await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    sent = failed = 0
    blocked_chat_ids: set[int] = set()  # 403 -> user stopped the bot; mark them after the run
    bot = Bot(token=container.settings.bot.token)
    try:
        for i, chat_id in enumerate(chat_ids, start=1):
            try:
                await deliver(chat_id)
                sent += 1
            except TelegramRetryAfter as exc:  # flood control — wait and retry once
                await asyncio.sleep(exc.retry_after)
                try:
                    await deliver(chat_id)
                    sent += 1
                except TelegramForbiddenError:
                    failed += 1
                    blocked_chat_ids.add(chat_id)
                except Exception:
                    failed += 1
            except TelegramForbiddenError:
                failed += 1
                blocked_chat_ids.add(chat_id)
            except Exception:
                failed += 1
            # ~28 msg/s ceiling keeps us under Telegram's 30/s global limit.
            await asyncio.sleep(0.036)
            if i % 25 == 0 or i == len(chat_ids):
                async with container.uow() as uow:
                    b = await uow.broadcasts.get(broadcast_id)
                    if b is None or b.status is BroadcastStatus.CANCELED:
                        return
                    b.sent, b.failed = sent, failed
                    await uow.commit()
    finally:
        await bot.session.close()

    if blocked_chat_ids:
        # Record who blocked the bot so the stats screen can count them and future runs can
        # skip dead chats. Cleared automatically on the user's next interaction (middleware).
        from sqlalchemy import update

        from src.infrastructure.database.models.user import User

        async with container.uow() as uow:
            await uow.session.execute(
                update(User)
                .where(User.telegram_id.in_(blocked_chat_ids))
                .values(bot_blocked_at=dt.datetime.now(dt.UTC))
            )
            await uow.commit()

    async with container.uow() as uow:
        b = await uow.broadcasts.get(broadcast_id)
        if b is not None:
            b.sent, b.failed = sent, failed
            b.status = BroadcastStatus.DONE
            b.finished_at = dt.datetime.now(dt.UTC)
            await uow.commit()
    log.info("send_broadcast done", broadcast_id=broadcast_id, sent=sent, failed=failed)


@broker.task(schedule=[{"cron": "23 */2 * * *"}])
async def process_autopay() -> int:
    """Auto-renew from balance (#2): charge the wallet and extend subscriptions that are about
    to expire and have autopay enabled. Idempotent — a renewed sub leaves the window."""
    from sqlalchemy import select

    from src.core.enums import SubscriptionStatus
    from src.infrastructure.database.base import utcnow
    from src.infrastructure.database.models.subscription import Subscription

    container = get_container()
    async with container.uow() as uow:
        if not bool(await container.bot_config.value(uow, "AUTO_RENEWAL_ENABLED")):
            return 0
        days_before = int(await container.bot_config.value(uow, "AUTO_RENEWAL_DAYS_BEFORE"))
    now = utcnow()
    horizon = now + dt.timedelta(days=max(0, days_before))
    async with container.uow() as uow:
        rows = (
            await uow.session.execute(
                select(Subscription.id).where(
                    Subscription.autopay_enabled.is_(True),
                    Subscription.status.in_(
                        [SubscriptionStatus.ACTIVE, SubscriptionStatus.LIMITED]
                    ),
                    Subscription.expire_at.is_not(None),
                    Subscription.expire_at >= now,
                    Subscription.expire_at <= horizon,
                )
            )
        ).all()
    renewed = 0
    for (sub_id,) in rows:
        try:
            if await _autopay_one(container, sub_id, horizon):
                renewed += 1
        except Exception:
            log.warning("autopay_failed", subscription_id=sub_id, exc_info=True)
    log.info("process_autopay", candidates=len(rows), renewed=renewed)
    return renewed


async def _autopay_one(container: object, subscription_id: int, horizon: dt.datetime) -> bool:
    """Charge + renew one subscription: from balance, else the saved card (opt-in).

    Returns True on a successful renewal."""
    from typing import cast

    from src.application.dto.pricing import PurchaseRequest
    from src.application.services.subscription import _plan_snapshot
    from src.core.enums import Currency, PurchaseType, SubscriptionStatus
    from src.core.enums import TransactionStatus as TS
    from src.core.enums import TransactionType as TT
    from src.infrastructure.database.base import utcnow
    from src.infrastructure.database.models.subscription import Subscription
    from src.infrastructure.database.models.transaction import Transaction
    from src.infrastructure.di import AppContainer

    c = cast(AppContainer, container)
    async with c.uow() as uow:
        # Lock the row + re-assert still-due INSIDE the lock: two overlapping process_autopay
        # runs both snapshot this sub_id; without this a slow run A renews, then run B (holding
        # the same snapshot) charges again for the same period. A already pushed expire_at past
        # the horizon, so the re-check makes B bail (double-charge guard).
        sub = await uow.session.get(Subscription, subscription_id, with_for_update=True)
        if sub is None or not sub.autopay_enabled or sub.plan_id is None:
            return False
        if (
            sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.LIMITED)
            or sub.expire_at is None
            or sub.expire_at > horizon
        ):
            return False  # already renewed this window (or no longer due)
        user = await uow.users.get(sub.user_id)
        plan = await uow.plans.get_with_durations(sub.plan_id)
        if user is None or plan is None or not plan.durations:
            return False
        duration = next(
            (d for d in plan.durations if d.days == sub.autopay_period_days), plan.durations[0]
        )
        req = PurchaseRequest(
            user_id=user.id,
            plan_id=plan.id,
            duration_days=duration.days,
            currency=Currency.RUB,
            purchase_type=PurchaseType.RENEW,
            subscription_id=sub.id,
        )
        quote = await c.pricing.quote(uow, req)
        price = quote.final.amount_minor
        if user.balance_minor < price:
            # The card path (below) may only run once a day per subscription — the task
            # fires every 2h and a declining card must not be hammered all window long.
            attempt_due = (
                sub.autopay_card_attempted_at is None
                or utcnow() - sub.autopay_card_attempted_at >= dt.timedelta(hours=20)
            )
            if not (
                sub.autopay_card_enabled
                and user.saved_payment_method_id is not None
                and attempt_due
            ):
                return False  # insufficient balance — user keeps autopay on for next window
            # Claim the daily card attempt INSIDE the row lock and commit before releasing it, so a
            # concurrent process_autopay run sees attempted_at set and bails — otherwise both pass
            # the `attempted_at IS NULL` check and charge the saved card twice for one renewal.
            sub.autopay_card_attempted_at = utcnow()
            await uow.commit()
            # Card path below (outside this uow): it owns its transaction lifecycle.
        else:
            # Guarded debit (WHERE balance >= price): the check-then-act above races a
            # concurrent checkout/withdrawal and could drive the wallet negative otherwise.
            if not await uow.users.debit_balance_guarded(user, price):
                return False  # balance dropped between read and debit — retry next window
            await uow.transactions.add(
                Transaction(
                    user_id=user.id,
                    type=TT.SUBSCRIPTION_PAYMENT,
                    status=TS.COMPLETED,
                    amount_minor=price,
                    currency=Currency.RUB,
                    purchase_type=PurchaseType.RENEW,
                    plan_snapshot=_plan_snapshot(plan),
                    pricing={"duration_days": duration.days, "subscription_id": sub.id},
                )
            )
            await c.subscriptions.renew(uow, sub, days=duration.days, telegram_id=user.telegram_id)
            await uow.commit()
            await _lifecycle_dm(
                c,
                user.telegram_id,
                "autopay_success",
                plan=str((sub.plan_snapshot or {}).get("name") or ""),
                expire=sub.expire_at.strftime("%d.%m.%Y") if sub.expire_at else "",
            )
            return True
    return await _autopay_charge_card(c, subscription_id)


async def _autopay_charge_card(c: AppContainer, subscription_id: int) -> bool:
    """Renew via the saved card: PENDING txn → charge without confirmation → the standard
    idempotent pipeline (PaymentService.process). Returns True on a completed renewal.

    A ``pending`` charge is left to the webhook/reconciler; a failed HTTP call leaves the
    txn PENDING with no external_id (same contract as the interactive gateway flow)."""
    import contextlib

    from src.application.common.payments import PaymentContext
    from src.application.dto.pricing import PurchaseRequest
    from src.core.enums import Currency, PaymentGatewayType, PurchaseType
    from src.core.exceptions import ConfigError
    from src.core.money import Money
    from src.infrastructure.database.base import utcnow
    from src.infrastructure.payments.crypto import decrypt_gateway_settings
    from src.infrastructure.payments.gateways.yookassa import YookassaGateway

    async with c.uow() as uow:
        sub = await uow.subscriptions.get(subscription_id)
        if sub is None or sub.plan_id is None:
            return False
        user = await uow.users.get(sub.user_id)
        if user is None or not user.saved_payment_method_id:
            return False
        row = await uow.payment_gateways.get_active(PaymentGatewayType.YOOKASSA)
        if row is None or PaymentGatewayType.YOOKASSA not in c.gateway_factory.supported():
            return False
        settings = decrypt_gateway_settings(c.secret_box, dict(row.settings))
        gateway = c.gateway_factory.create(PaymentGatewayType.YOOKASSA, settings)
        if not isinstance(gateway, YookassaGateway) or not gateway.recurrent_enabled:
            return False

        plan = await uow.plans.get_with_durations(sub.plan_id)
        if plan is None or not plan.durations:
            return False
        duration = next(
            (d for d in plan.durations if d.days == sub.autopay_period_days), plan.durations[0]
        )
        sub.autopay_card_attempted_at = utcnow()  # set BEFORE charging: never retry a limbo
        req = PurchaseRequest(
            user_id=user.id,
            plan_id=plan.id,
            duration_days=duration.days,
            currency=Currency.RUB,
            purchase_type=PurchaseType.RENEW,
            subscription_id=sub.id,
        )
        txn, quote = await c.purchase.start(uow, req)
        if quote.is_free:  # start() already completed + fulfilled it
            await uow.commit()
            return True
        txn.gateway_type = PaymentGatewayType.YOOKASSA
        txn.gateway_display_name = row.display_name or PaymentGatewayType.YOOKASSA.value
        txn.payment_method = "saved_card"
        await uow.commit()

        payment_id = txn.payment_id
        amount = Money(quote.final.amount_minor, txn.currency)
        title = str((txn.plan_snapshot or {}).get("name") or "VPN")
        duration_days = duration.days
        user_id, telegram_id = user.id, user.telegram_id
        method_token = user.saved_payment_method_id

    # Stored encrypted (see _store_saved_method); tolerate plaintext like gateway creds.
    if c.secret_box is not None:
        with contextlib.suppress(ConfigError):
            method_token = c.secret_box.decrypt(method_token)

    ctx = PaymentContext(
        payment_id=payment_id,
        amount=amount,
        description=f"{title} · {duration_days} дн. (автопродление)",
        user_id=user_id,
        telegram_id=telegram_id,
    )
    try:
        result = await gateway.charge_saved(ctx, method_token)
    except Exception:
        log.warning("autopay card charge failed", subscription_id=subscription_id, exc_info=True)
        await _lifecycle_dm(c, telegram_id, "autopay_failed")
        return False

    async with c.uow() as uow:
        txn2 = await uow.transactions.get_by_payment_id(payment_id)
        if txn2 is not None and result.external_id and not txn2.external_id:
            txn2.external_id = result.external_id  # reconciler can now poll this payment
            await uow.commit()

    if result.status is TransactionStatus.PENDING:
        # Not terminal yet — the webhook/reconciler finishes it through the same pipeline.
        log.info("autopay card charge pending", subscription_id=subscription_id)
        return False

    async with c.uow() as uow:
        moved = await c.payments.process(uow, payment_id=payment_id, status=result.status)
        await uow.commit()

    if result.status is TransactionStatus.COMPLETED and moved:
        expire_s = ""
        async with c.uow() as uow:
            sub2 = await uow.subscriptions.get(subscription_id)
            if sub2 is not None and sub2.expire_at:
                expire_s = sub2.expire_at.strftime("%d.%m.%Y")
        await _lifecycle_dm(c, telegram_id, "autopay_success", plan=title, expire=expire_s)
        return True
    await _lifecycle_dm(c, telegram_id, "autopay_failed")
    return False
