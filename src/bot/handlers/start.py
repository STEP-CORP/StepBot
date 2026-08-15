"""/start: deep-link attribution (referral / campaign / linking / web login) + main menu."""

from __future__ import annotations

import json
from html import escape as _hesc

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.menu_render import send_main_menu
from src.core.logging import get_logger
from src.infrastructure.database.models.user import User
from src.infrastructure.di import AppContainer

log = get_logger(__name__)

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    container: AppContainer,
    db_user: User,
    db_user_created: bool,
) -> None:
    param = (command.args or "").strip()
    gift_note: str | None = None
    if param.startswith("gift_"):
        gift_note = await _claim_gift(container, db_user, param.removeprefix("gift_"))
    elif param.startswith("link_"):
        await _link_prompt(message, container, param.removeprefix("link_"))
        return  # the confirm keyboard IS the answer; the menu would push it off-screen
    elif param.startswith("weblogin_"):
        await _weblogin_prompt(message, container, param.removeprefix("weblogin_"))
        return  # the confirm keyboard IS the answer; the menu would push it off-screen
    elif param:
        await _attribute(container, db_user, param, created=db_user_created)
    if gift_note:
        await message.answer(gift_note, parse_mode="HTML")
    await send_main_menu(message, container, db_user)


async def _weblogin_prompt(message: Message, container: AppContainer, code: str) -> None:
    """t.me/<bot>?start=weblogin_<CODE> — «Войти через Telegram» on the website.

    Explicit in-bot confirmation, not auto-login: a forwarded/phished link must not log
    the victim's account into someone else's browser without them pressing the button.
    """
    from src.application.services.account_link import TG_WEBLOGIN_PREFIX

    code = code.strip()
    raw = await container.redis.get(f"{TG_WEBLOGIN_PREFIX}{code}")
    if raw is None or json.loads(raw).get("status") != "pending":
        await message.answer(
            "🌐 Ссылка входа устарела. Вернись на сайт и нажми «Войти через Telegram» ещё раз."
        )
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, это я", callback_data=f"weblogin:ok:{code}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"weblogin:no:{code}"),
            ]
        ]
    )
    await message.answer(
        "🌐 <b>Вход на сайт</b>\n\nКто-то (скорее всего ты) входит на сайт под этим "
        "Telegram-аккаунтом. Подтвердить вход?\n\n<i>Если это не ты — жми «Отмена».</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("weblogin:"))
async def weblogin_confirm(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    from src.application.services.account_link import TG_WEBLOGIN_PREFIX

    try:
        _, action, code = (cb.data or "").split(":", 2)
    except ValueError:
        await cb.answer()
        return
    key = f"{TG_WEBLOGIN_PREFIX}{code}"
    raw = await container.redis.get(key)
    if raw is None or json.loads(raw).get("status") != "pending":
        await cb.answer("Ссылка устарела", show_alert=True)
        return
    if action != "ok":
        await container.redis.delete(key)
        if isinstance(cb.message, Message):
            await cb.message.edit_text("Вход отменён.")
        await cb.answer()
        return
    # A short tail TTL: the site polls every couple of seconds, a minute is plenty.
    await container.redis.set(key, json.dumps({"status": "ok", "user_id": db_user.id}), ex=60)
    if isinstance(cb.message, Message):
        await cb.message.edit_text("✅ Вход подтверждён — вернись на сайт, кабинет уже открыт.")
    await cb.answer()
    log.info("web login confirmed", user=db_user.id)


async def _link_prompt(message: Message, container: AppContainer, code: str) -> None:
    """t.me/<bot>?start=link_<CODE> — merge the web-cabinet account into this one.

    The code was minted by the cabinet's «Привязать Telegram» button (single-use,
    15 min). Everything the web account owns — подписка, баланс, история — moves to
    the Telegram account; the site then opens the same, single account.

    Explicit in-bot confirmation, NOT an instant merge: the code is minted by whoever is
    logged into the web cabinet, so a forwarded link would otherwise absorb a STRANGER's
    web account (and its OAuth identities / sessions) into the victim's Telegram account —
    account takeover. The confirm screen shows the web account's e-mail so the person can
    tell it isn't theirs and decline.
    """
    from src.application.services.account_link import TG_LINK_PREFIX

    code = code.strip()
    raw = await container.redis.get(f"{TG_LINK_PREFIX}{code}")  # peek, don't spend yet
    try:
        web_user_id = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        web_user_id = None
    if web_user_id is None:
        await message.answer(
            "🔗 Ссылка привязки устарела. Открой кабинет на сайте и нажми "
            "«Привязать Telegram» ещё раз."
        )
        return
    async with container.uow() as uow:
        web = await uow.users.get(web_user_id)
    if web is None or web.telegram_id is not None:
        await message.answer("🔗 Ссылка привязки устарела. Запроси новую в кабинете на сайте.")
        return
    who = web.email or "аккаунт с сайта (вход через ВК/Яндекс/Google)"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Привязать", callback_data=f"acclink:ok:{code}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"acclink:no:{code}"),
            ]
        ]
    )
    await message.answer(
        f"🔗 <b>Привязка аккаунта</b>\n\nК твоему Telegram будет привязан аккаунт с сайта "
        f"(<b>{_hesc(who)}</b>). Его подписка, баланс и история переедут сюда.\n\n"
        f"<i>Если это не твой аккаунт — жми «Отмена».</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("acclink:"))
async def acclink_confirm(cb: CallbackQuery, container: AppContainer, db_user: User) -> None:
    from src.application.services.account_link import (
        TG_LINK_PREFIX,
        AccountLinkError,
        merge_web_into_telegram,
    )

    try:
        _, action, code = (cb.data or "").split(":", 2)
    except ValueError:
        await cb.answer()
        return
    if action != "ok":
        await container.redis.delete(f"{TG_LINK_PREFIX}{code}")
        if isinstance(cb.message, Message):
            await cb.message.edit_text("Привязка отменена.")
        await cb.answer()
        return
    raw = await container.redis.getdel(f"{TG_LINK_PREFIX}{code}")  # spend on confirm
    try:
        web_user_id = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        web_user_id = None
    if web_user_id is None:
        await cb.answer("Ссылка устарела", show_alert=True)
        return
    async with container.uow() as uow:
        user = await uow.users.get(db_user.id)
        if user is None:
            await cb.answer("Ошибка, попробуй ещё раз", show_alert=True)
            return
        try:
            await merge_web_into_telegram(uow, user, web_user_id)
        except AccountLinkError as exc:
            if isinstance(cb.message, Message):
                await cb.message.edit_text(f"🔗 Не получилось привязать: {exc}")
            await cb.answer()
            return
        await uow.commit()
    log.info("web account linked", user=db_user.id, web_user=web_user_id)
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            "🔗 <b>Аккаунты связаны!</b> Подписка, баланс и история с сайта теперь здесь, "
            "а на сайте ты видишь этот же аккаунт.",
            parse_mode="HTML",
        )
    await cb.answer()


async def _claim_gift(container: AppContainer, db_user: User, code: str) -> str:
    """t.me/<bot>?start=gift_<CODE> — apply the promocode right from the deep-link.

    Reuses the promocode engine wholesale: per-user unique activation, limits,
    expiry, and instant rewards (balance / days / subscription / discount).
    """
    from src.application.services.promo import PromoError
    from src.core.exceptions import DomainError

    async with container.uow() as uow:
        user = await uow.users.get(db_user.id)
        if user is None:
            return "Ошибка, попробуй ещё раз."
        try:
            reward = await container.promo.apply(uow, user, code.strip().upper())
        except PromoError as exc:
            return f"🎁 Не получилось активировать подарок: {exc}"
        except DomainError:
            # DURATION/SUBSCRIPTION rewards provision on the panel; a momentary panel blip
            # raises RemnawaveError (a DomainError, not a PromoError). Don't let it escape
            # cmd_start and blow away the whole /start with a generic error — the uow rolls
            # back, the code isn't consumed, the user can retry.
            log.warning("gift panel error", user=db_user.id, code=code[:16])
            return "🎁 Не удалось активировать подарок: панель недоступна. Попробуй чуть позже."
        await uow.commit()
    log.info("gift claimed", user=db_user.id, code=code[:16], reward=reward.value)
    return "🎁 <b>Подарок активирован!</b> Загляни в «Личный кабинет»."


async def _attribute(container: AppContainer, db_user: User, param: str, *, created: bool) -> None:
    """ref_<code> -> referred_by; anything else -> campaign start_param (first touch only)."""
    async with container.uow() as uow:
        user = await uow.users.get(db_user.id)
        if user is None:
            return
        if param.startswith("ref_"):
            if user.referred_by_id is None and created:
                # bind() creates the Referral row the commission engine reads
                # (reward_on_topup) — setting referred_by_id alone pays nobody.
                referral = await container.referrals.bind(uow, user, param.removeprefix("ref_"))
                if referral is not None:
                    log.info("referral attributed", user=user.id, referrer=referral.referrer_id)
        elif param.startswith("partner_"):
            # A reseller/affiliate link (?start=partner_<code>). Attribute the new user to the
            # partner's own account so they earn the standard referral commission through the
            # tested engine — the link used to be silently ignored and paid nobody (PART-1).
            if user.referred_by_id is None and created:
                partner = await uow.partners.by_code(param.removeprefix("partner_").lower())
                if partner is not None and partner.enabled and partner.telegram_id:
                    owner = await uow.users.get_by_telegram_id(partner.telegram_id)
                    if owner is not None and owner.id != user.id:
                        referral = await container.referrals.bind(uow, user, owner.referral_code)
                        if referral is not None:
                            log.info("partner attributed", user=user.id, partner=partner.id)
        elif user.campaign_id is None:
            campaign = await uow.campaigns.find_one(start_param=param, is_active=True)
            if campaign is not None:
                user.campaign_id = campaign.id
                if campaign.promo_group_id is not None:
                    from src.infrastructure.database.models.promo_group import UserPromoGroup

                    # Serialize with PromoService: a promocode for the same group can be
                    # redeemed concurrently, and the membership PK is composite. Relying on
                    # the autoflush of `user.campaign_id` to take this row lock would break
                    # the moment someone reorders these lines.
                    await uow.users.lock_for_update(user.id)
                    existing = await uow.session.get(
                        UserPromoGroup,
                        {"user_id": user.id, "promo_group_id": campaign.promo_group_id},
                    )
                    if existing is None:
                        uow.session.add(
                            UserPromoGroup(user_id=user.id, promo_group_id=campaign.promo_group_id)
                        )
                log.info("campaign attributed", user=user.id, campaign=campaign.id)
        await uow.commit()
