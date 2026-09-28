"""Admin: bot menu constructor (screen 05) - read/replace the button tree."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.application.services.bot_config import BotConfigError
from src.bot.default_menu import DEFAULT_MENU, MENU_ACTIONS
from src.core.enums import MenuNodeKind
from src.infrastructure.database.models.menu_node import MenuNode
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import audit
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/bot-menu")


class NodeIn(BaseModel):
    # Client-side ids are opaque strings; parent refs use the same ids.
    id: str = Field(min_length=1, max_length=36)
    parent: str | None = None
    label: str = Field(min_length=1, max_length=64)
    kind: MenuNodeKind = MenuNodeKind.ACTION
    payload: str | None = Field(None, max_length=4096)
    custom_emoji_id: str | None = Field(None, max_length=32)
    color: str | None = Field(None, max_length=9)
    image_path: str | None = Field(None, max_length=512)
    is_active: bool = True
    row_index: int = Field(0, ge=0)  # buttons sharing a row_index sit side by side
    # None (field omitted by an older SPA) -> fall back to array position; an explicit value
    # (incl. 0) from the editor is honoured so reordering persists.
    order_index: int | None = Field(None, ge=0)

    @field_validator("color")
    @classmethod
    def _hex_color(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.startswith("#") and len(v) in (4, 7, 9)):
            raise ValueError("color must be #RGB/#RRGGBB/#RRGGBBAA")
        return v


class TreeIn(BaseModel):
    nodes: list[NodeIn]
    text: str | None = None  # START_MESSAGE (menu caption); None = leave as-is


def _serialize(nodes: list[MenuNode]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(n.id),
            "parent": str(n.parent_id) if n.parent_id is not None else None,
            "label": n.label,
            "kind": n.kind.value,
            "payload": n.payload,
            "custom_emoji_id": n.custom_emoji_id,
            "color": n.color,
            "image_path": n.image_path,
            "is_active": n.is_active,
            "order_index": n.order_index,
            "row_index": n.row_index,
        }
        for n in nodes
    ]


def _default_menu_rows() -> list[MenuNode]:
    """DEFAULT_MENU as fresh top-level ACTION nodes - shared by reset + first-boot seed."""
    return [
        MenuNode(
            parent_id=None,
            order_index=i,
            row_index=b.row,
            label=b.label,
            kind=MenuNodeKind.ACTION,
            payload=b.action,
            color=b.color,
        )
        for i, b in enumerate(DEFAULT_MENU)
    ]


# Top-level action sets of menus we shipped as defaults in earlier versions. A live menu
# whose top-level actions match one of these was our seed (not the owner's work), so a
# later deploy may upgrade it to the current DEFAULT_MENU. A customized menu - any other
# action set - is never touched.
_LEGACY_DEFAULT_SIGNATURES: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "cabinet",
            "buy",
            "subscription",
            "connect",
            "balance",
            "history",
            "promocode",
            "referral",
            "support",
        }
    ),
)


async def bootstrap_menu(container: AppContainer) -> None:
    """Seed the default menu on first boot; on later boots, upgrade an *unmodified* older
    default to the current one. Called from the app lifespan and safe to run on every start:
    the owner's own menu (a different action set) is left untouched.
    """
    async with container.uow() as uow:
        top = [n for n in await uow.menu_nodes.tree() if n.parent_id is None]
        current = frozenset(n.payload for n in top if n.kind is MenuNodeKind.ACTION and n.payload)
        target = frozenset(b.action for b in DEFAULT_MENU)
        # Non-empty menu that is already current OR was customized by the owner -> leave it.
        if top and (current == target or current not in _LEGACY_DEFAULT_SIGNATURES):
            return
        await uow.menu_nodes.delete_by()
        for row in _default_menu_rows():
            await uow.menu_nodes.add(row)
        await uow.commit()


# Flat config keys owned by the "Bot menu" tab (tree lives in menu_nodes; none are secrets).
MENU_CONFIG_KEYS: tuple[str, ...] = (
    "START_MESSAGE",
    "CABINET_BUTTONS",
    "CABINET_TEXT",
    "CABINET_SUB_ACTIVE",
    "CABINET_SUB_INACTIVE",
    "SCREEN_BUTTONS",
)

# Extra flat keys that belong to the tab but live outside the "menu" section:
# screen texts, text emoji, and banner switches. Banner slot keys are appended
# at call time from _BANNER_SLOTS (defined below). Together with MENU_CONFIG_KEYS
# this makes the top-level Export/Import cover the WHOLE tab, not just the menu.
_SNAPSHOT_EXTRA_KEYS: tuple[str, ...] = (
    "SCREEN_TEXTS",
    "MENU_TEXT_EMOJI",
    "CABINET_TEXT_EMOJI",
    "BANNER_ENABLED",
    "BANNER_MODE",
)


def _snapshot_config_keys() -> tuple[str, ...]:
    """Every flat bot_config key the Bot-menu tab owns - full-tab snapshot set."""
    banner_keys = tuple(k for k, _ in _BANNER_SLOTS)
    return MENU_CONFIG_KEYS + _SNAPSHOT_EXTRA_KEYS + banner_keys


@router.get("")
async def get_menu(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    from src.core.config_registry import spec

    async with container.uow() as uow:
        nodes = list(await uow.menu_nodes.tree())
        start_text = str(await container.bot_config.value(uow, "START_MESSAGE") or "")
    return {
        "nodes": _serialize(nodes),
        "text": start_text,
        "text_default": str(spec("START_MESSAGE").default or ""),
    }


async def _write_menu_tree(uow: Any, nodes: list[NodeIn]) -> None:
    """Validate + fully replace the menu tree (parents-first). Shared by save + import."""
    # Validate parent references + no cycles (parents must appear earlier or exist).
    ids = {n.id for n in nodes}
    if len(ids) != len(nodes):
        raise HTTPException(400, "duplicate node ids")
    valid_actions = {a.code for a in MENU_ACTIONS}
    for n in nodes:
        if n.parent is not None and n.parent not in ids:
            raise HTTPException(400, f"node {n.id}: unknown parent {n.parent}")
        if n.parent == n.id:
            raise HTTPException(400, f"node {n.id}: self-parent")
        # An action button with a NON-EMPTY code must point at a real bot action (catches a typo
        # right away). An empty payload is allowed: it's an unconfigured button that the bot
        # renders as a harmless no-op, and rejecting it would lock an operator out of saving a
        # menu that already contains such a placeholder (e.g. built under an older version).
        if n.kind is MenuNodeKind.ACTION and n.payload and n.payload not in valid_actions:
            raise HTTPException(400, f"кнопка «{n.label}»: неизвестное действие «{n.payload}»")

    # Insert parents-first, mapping client ids -> DB ids.
    await uow.menu_nodes.delete_by()
    id_map: dict[str, int] = {}
    pending = list(nodes)
    # Honour the editor's explicit order_index so reordering persists instead of snapping
    # back to creation order. move() assigns a unique 0..n-1 per parent (a swap), so
    # `n.order_index` is authoritative - no falsy-0 fallback (that mislaid a top-moved
    # button). array_pos is only a last resort when a client omits it entirely.
    array_pos: dict[str | None, int] = {}
    guard = 0
    while pending:
        guard += 1
        if guard > len(nodes) + 2:
            raise HTTPException(400, "menu tree contains a cycle")
        progressed = False
        rest: list[NodeIn] = []
        for n in pending:
            if n.parent is None or n.parent in id_map:
                pos = array_pos.get(n.parent, 0)
                array_pos[n.parent] = pos + 1
                row = MenuNode(
                    parent_id=id_map.get(n.parent) if n.parent else None,
                    order_index=n.order_index if n.order_index is not None else pos,
                    row_index=n.row_index,
                    label=n.label,
                    kind=n.kind,
                    payload=n.payload,
                    custom_emoji_id=n.custom_emoji_id or None,
                    color=n.color,
                    image_path=n.image_path or None,
                    is_active=n.is_active,
                )
                await uow.menu_nodes.add(row)
                id_map[n.id] = row.id
                progressed = True
            else:
                rest.append(n)
        if not progressed:
            raise HTTPException(400, "menu tree contains a cycle")
        pending = rest


@router.put("")
async def save_menu(
    body: TreeIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        await _write_menu_tree(uow, body.nodes)
        if body.text is not None:
            await container.bot_config.set_values(uow, {"START_MESSAGE": body.text})
        await audit(uow, identity, "menu.save", None, count=len(body.nodes))
        await uow.commit()
        nodes = list(await uow.menu_nodes.tree())
    return {"ok": True, "nodes": _serialize(nodes)}


@router.get("/export")
async def export_menu(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Snapshot the whole Bot-menu tab: tree + all flat tab config keys. No secrets here."""
    async with container.uow() as uow:
        nodes = list(await uow.menu_nodes.tree())
        config = {k: await container.bot_config.value(uow, k) for k in _snapshot_config_keys()}
    return {
        "kind": "stepbot-menu",
        "version": 1,
        "nodes": _serialize(nodes),
        "config": config,
    }


class MenuImportIn(BaseModel):
    nodes: list[NodeIn] | None = None  # None = leave the tree untouched
    config: dict[str, Any] | None = None  # only snapshot keys are applied; others ignored


@router.post("/import")
async def import_menu(
    body: MenuImportIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Restore a Bot-menu snapshot: replace the tree and/or the menu config keys."""
    applied: list[str] = []
    node_count = 0
    async with container.uow() as uow:
        if body.config:
            snapshot_keys = set(_snapshot_config_keys())
            changes = {k: v for k, v in body.config.items() if k in snapshot_keys}
            if changes:
                try:
                    applied = await container.bot_config.set_values(uow, changes)
                except BotConfigError as exc:
                    raise HTTPException(400, str(exc)) from exc
        if body.nodes is not None:
            await _write_menu_tree(uow, body.nodes)
            node_count = len(body.nodes)
        await audit(uow, identity, "menu.import", None, nodes=node_count, keys=applied)
        await uow.commit()
        nodes = list(await uow.menu_nodes.tree())
    return {"ok": True, "nodes": node_count, "applied": applied, "tree": _serialize(nodes)}


@router.get("/actions")
async def list_actions() -> dict[str, Any]:
    """Catalogue of bot actions a button can point at - feeds the constructor's dropdown."""
    return {
        "actions": [
            {
                "code": a.code,
                "label_ru": a.label_ru,
                "label_en": a.label_en,
                "needs_subscription": a.needs_subscription,
            }
            for a in MENU_ACTIONS
        ]
    }


_BANNER_SLOTS: tuple[tuple[str, str], ...] = (
    ("BANNER_DEFAULT", "По умолчанию"),
    ("BANNER_MENU", "Меню"),
    ("BANNER_BUY", "Покупка"),
    ("BANNER_CABINET", "Кабинет"),
    ("BANNER_SUBSCRIPTION", "Подписка"),
    ("BANNER_TRAFFIC", "Трафик"),
    ("BANNER_BALANCE", "Баланс"),
    ("BANNER_REFERRAL", "Рефералка"),
    ("BANNER_SUPPORT", "Поддержка"),
    ("BANNER_TRIAL", "Триал"),
)


@router.get("/banners")
async def get_banners(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """«Картинки бота»: on/off, mode (one | per_screen), and each screen's image ref."""
    async with container.uow() as uow:
        cfg = container.bot_config
        enabled = bool(await cfg.value(uow, "BANNER_ENABLED"))
        mode = str(await cfg.value(uow, "BANNER_MODE") or "one")
        slots = [
            {"key": k, "label": label, "value": str(await cfg.value(uow, k) or "")}
            for k, label in _BANNER_SLOTS
        ]
    return {"enabled": enabled, "mode": mode, "slots": slots}


class BannersIn(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(None, pattern="^(one|per_screen)$")
    slots: dict[str, str] | None = None  # {BANNER_KEY: ref}


@router.put("/banners")
async def save_banners(
    body: BannersIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    valid_keys = {k for k, _ in _BANNER_SLOTS}
    changes: dict[str, Any] = {}
    if body.enabled is not None:
        changes["BANNER_ENABLED"] = body.enabled
    if body.mode is not None:
        changes["BANNER_MODE"] = body.mode
    for k, v in (body.slots or {}).items():
        if k in valid_keys:
            changes[k] = v  # empty string clears the slot -> falls back to default
    async with container.uow() as uow:
        if changes:
            await container.bot_config.set_values(uow, changes)
            await audit(uow, identity, "menu.banners", None, keys=",".join(changes))
            await uow.commit()
    return {"ok": True}


@router.get("/texts")
async def get_texts(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Editable bot texts: the main-menu greeting and the «Личный кабинет» templates, with the
    effective value (an empty override falls back to the built-in default) + placeholder lists."""
    from src.bot.cabinet_text import (
        DEFAULT_CABINET_TEXT,
        DEFAULT_SUB_ACTIVE,
        DEFAULT_SUB_INACTIVE,
        MAIN_PLACEHOLDERS,
        SUB_PLACEHOLDERS,
    )

    async with container.uow() as uow:
        cfg = container.bot_config
        main_menu = str(await cfg.value(uow, "START_MESSAGE") or "")
        cabinet = str(await cfg.value(uow, "CABINET_TEXT") or "") or DEFAULT_CABINET_TEXT
        sub_active = str(await cfg.value(uow, "CABINET_SUB_ACTIVE") or "") or DEFAULT_SUB_ACTIVE
        sub_inactive = (
            str(await cfg.value(uow, "CABINET_SUB_INACTIVE") or "") or DEFAULT_SUB_INACTIVE
        )
        menu_emoji = str(await cfg.value(uow, "MENU_TEXT_EMOJI") or "")
        cabinet_emoji = str(await cfg.value(uow, "CABINET_TEXT_EMOJI") or "")
    return {
        "main_menu": main_menu,
        "cabinet": cabinet,
        "cabinet_sub_active": sub_active,
        "cabinet_sub_inactive": sub_inactive,
        "menu_emoji": menu_emoji,
        "cabinet_emoji": cabinet_emoji,
        "placeholders": {
            "cabinet": ["{" + p + "}" for p in MAIN_PLACEHOLDERS],
            "sub": ["{" + p + "}" for p in SUB_PLACEHOLDERS],
        },
        "defaults": {
            "cabinet": DEFAULT_CABINET_TEXT,
            "cabinet_sub_active": DEFAULT_SUB_ACTIVE,
            "cabinet_sub_inactive": DEFAULT_SUB_INACTIVE,
        },
    }


class TextsIn(BaseModel):
    main_menu: str | None = Field(None, max_length=4096)
    cabinet: str | None = Field(None, max_length=4096)
    cabinet_sub_active: str | None = Field(None, max_length=2048)
    cabinet_sub_inactive: str | None = Field(None, max_length=2048)
    menu_emoji: str | None = Field(None, max_length=128)
    cabinet_emoji: str | None = Field(None, max_length=128)


@router.put("/texts")
async def save_texts(
    body: TextsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if body.main_menu is not None:
        changes["START_MESSAGE"] = body.main_menu
    if body.cabinet is not None:
        changes["CABINET_TEXT"] = body.cabinet
    if body.cabinet_sub_active is not None:
        changes["CABINET_SUB_ACTIVE"] = body.cabinet_sub_active
    if body.cabinet_sub_inactive is not None:
        changes["CABINET_SUB_INACTIVE"] = body.cabinet_sub_inactive
    if body.menu_emoji is not None:
        changes["MENU_TEXT_EMOJI"] = body.menu_emoji
    if body.cabinet_emoji is not None:
        changes["CABINET_TEXT_EMOJI"] = body.cabinet_emoji
    async with container.uow() as uow:
        if changes:
            await container.bot_config.set_values(uow, changes)
            await audit(uow, identity, "menu.texts", None, keys=",".join(changes))
            await uow.commit()
    return {"ok": True}


@router.get("/cabinet")
async def get_cabinet_buttons(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Cabinet buttons in owner order: built-ins (on/off) + custom, with actions and defaults."""
    from src.bot.cabinet_menu import CABINET_BUTTONS, cabinet_columns, cabinet_mode, parse_config
    from src.bot.screen_text import (
        CABINET_SUB_ACTIVE_DEFAULT,
        CABINET_SUB_INACTIVE_DEFAULT,
        cabinet_sub_placeholders,
        default_text_for,
        placeholders_for,
        sample_for,
    )

    async with container.uow() as uow:
        raw = str(await container.bot_config.value(uow, "CABINET_BUTTONS") or "")
        cab_text = str(await container.bot_config.value(uow, "CABINET_TEXT") or "")
        cab_sub_active = str(await container.bot_config.value(uow, "CABINET_SUB_ACTIVE") or "")
        cab_sub_inactive = str(await container.bot_config.value(uow, "CABINET_SUB_INACTIVE") or "")
    items = parse_config(raw)
    catalogue = {b.key: b for b in CABINET_BUTTONS}
    seen = {it["key"] for it in items}
    # Append built-ins the owner never listed, disabled, so the UI can re-enable them.
    for b in CABINET_BUTTONS:
        if b.key not in seen:
            items.append(
                {
                    "key": b.key,
                    "label": b.label,
                    "action": None,
                    "icon": None,
                    "color": None,
                    "enabled": False,
                    "custom": False,
                    "row": None,
                }
            )
    return {
        "buttons": [
            {
                "key": it["key"],
                "label": it["label"],
                "icon": it["icon"],
                "color": it["color"],
                "enabled": it["enabled"],
                "custom": it["custom"],
                "action": it["action"],
                "btype": it.get("btype", "action"),
                "url": it.get("url", ""),
                "stext": it.get("stext", ""),
                "row": it.get("row"),
                "gated": (not it["custom"]) and catalogue[it["key"]].gate is not None,
                "default_label": None if it["custom"] else catalogue[it["key"]].label,
            }
            for it in items
        ],
        "buttons_default": [
            {
                "key": b.key,
                "label": b.label,
                "icon": None,
                "color": None,
                "enabled": True,
                "custom": False,
                "action": None,
                "row": None,
                "gated": b.gate is not None,
                "default_label": b.label,
            }
            for b in CABINET_BUTTONS
        ],
        "per_row_default": 2,
        "layout_default": "uniform",
        "per_row": cabinet_columns(raw),
        "layout": cabinet_mode(raw),
        "text": cab_text,
        "text_default": default_text_for("cabinet"),
        "text_sample": sample_for("cabinet"),
        "text_placeholders": placeholders_for("cabinet"),
        "sub_active": cab_sub_active,
        "sub_active_default": CABINET_SUB_ACTIVE_DEFAULT,
        "sub_inactive": cab_sub_inactive,
        "sub_inactive_default": CABINET_SUB_INACTIVE_DEFAULT,
        "sub_placeholders": cabinet_sub_placeholders(),
    }


class CabinetButtonIn(BaseModel):
    key: str
    label: str = ""
    enabled: bool = True
    action: str | None = None
    # custom-button kind: action (default) | link | miniapp | screen | back
    btype: str | None = None
    url: str | None = None  # https/tg target for link/miniapp custom buttons
    stext: str | None = None  # sub-screen body for screen custom buttons
    row: int | None = None  # physical row index for the custom layout, else None
    icon: str | None = None  # numeric premium-emoji custom_emoji_id, or None
    color: str | None = None  # #RGB/#RRGGBB/#RRGGBBAA -> Bot API button style, or None

    @field_validator("color")
    @classmethod
    def _hex_color(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.startswith("#") and len(v) in (4, 7, 9)):
            raise ValueError("color must be #RGB/#RRGGBB/#RRGGBBAA")
        return v


class CabinetButtonsIn(BaseModel):
    items: list[CabinetButtonIn] | None = None  # full model: rename + custom buttons
    order: list[str] | None = None  # legacy: enabled built-in keys, in display order
    per_row: int | None = None  # buttons per row in the cabinet keyboard (1..3), default 2
    layout: str | None = None  # "custom" to honour per-item row, else "uniform"
    text: str | None = None  # cabinet caption template ({placeholders}); None = leave as-is
    sub_active: str | None = None  # {подписка} active-state template; None = leave as-is
    sub_inactive: str | None = None  # {подписка} no-subscription template; None = leave as-is


@router.put("/cabinet")
async def save_cabinet_buttons(
    body: CabinetButtonsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    import json as _json

    from src.bot.cabinet_menu import CABINET_BUTTONS, parse_cabinet_buttons

    if body.items is not None:
        by_key = {b.key: b for b in CABINET_BUTTONS}
        valid_actions = {a.code for a in MENU_ACTIONS}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for it in body.items:
            key = (it.key or "").strip()
            if not key or key in seen:
                continue
            icon = (it.icon or "").strip()
            if icon and not (icon.isdigit() and 1 <= len(icon) <= 24):
                raise HTTPException(
                    status_code=422, detail=f"button {key}: icon must be a numeric custom_emoji_id"
                )
            if key in by_key:
                label = (it.label or "").strip() or by_key[key].label
                row: dict[str, Any] = {"key": key, "label": label, "enabled": bool(it.enabled)}
            else:
                label = (it.label or "").strip()
                if not label:
                    raise HTTPException(status_code=422, detail=f"button {key}: label required")
                btype = (it.btype or "action").strip().lower()
                if btype not in ("action", "link", "miniapp", "screen"):
                    btype = "action"
                if btype == "action":
                    action = (it.action or "").strip().lower()
                    if action not in valid_actions:
                        raise HTTPException(
                            status_code=422, detail=f"button {key}: unknown action {action!r}"
                        )
                    row = {
                        "key": key,
                        "label": label,
                        "action": action,
                        "enabled": bool(it.enabled),
                    }
                elif btype == "link":
                    url = (it.url or "").strip()
                    if not (url.startswith("https://") or url.startswith("tg://")):
                        raise HTTPException(
                            status_code=422,
                            detail=f"button {key}: link needs an https:// or tg:// URL",
                        )
                    row = {
                        "key": key,
                        "label": label,
                        "btype": btype,
                        "url": url,
                        "enabled": bool(it.enabled),
                    }
                elif btype == "miniapp":
                    # No per-button URL: opens the global SUBSCRIPTION_MINI_APP_URL at render.
                    row = {"key": key, "label": label, "btype": btype, "enabled": bool(it.enabled)}
                else:  # screen
                    row = {
                        "key": key,
                        "label": label,
                        "btype": btype,
                        "stext": (it.stext or ""),
                        "enabled": bool(it.enabled),
                    }
            if icon:
                row["icon"] = icon
            color = (it.color or "").strip()  # already hex-validated by the pydantic model
            if color:
                row["color"] = color
            if isinstance(it.row, int) and it.row >= 0:
                row["row"] = it.row
            out.append(row)
            seen.add(key)
        pr = body.per_row if body.per_row in (1, 2, 3) else 2
        layout = "custom" if body.layout == "custom" else "uniform"
        value = _json.dumps({"per_row": pr, "layout": layout, "items": out}, ensure_ascii=False)
    else:
        order = body.order or []
        value = ",".join(parse_cabinet_buttons(",".join(order)))  # validate + drop unknowns

    async with container.uow() as uow:
        values = {"CABINET_BUTTONS": value}
        if body.text is not None:
            values["CABINET_TEXT"] = body.text
        if body.sub_active is not None:
            values["CABINET_SUB_ACTIVE"] = body.sub_active
        if body.sub_inactive is not None:
            values["CABINET_SUB_INACTIVE"] = body.sub_inactive
        await container.bot_config.set_values(uow, values)
        await audit(uow, identity, "menu.cabinet_buttons", None, value=value)
        await uow.commit()
    return {"ok": True}


class ScreenButtonIn(BaseModel):
    key: str
    label: str = ""
    enabled: bool = True
    custom: bool = False
    action: str | None = None
    url: str | None = None
    icon: str | None = None
    color: str | None = None
    row: int | None = None

    @field_validator("color")
    @classmethod
    def _hex_color(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.startswith("#") and len(v) in (4, 7, 9)):
            raise ValueError("color must be #RGB/#RRGGBB/#RRGGBBAA")
        return v


class ScreenButtonsIn(BaseModel):
    items: list[ScreenButtonIn]
    per_row: int | None = None
    layout: str | None = None
    text: str | None = None  # screen caption override; None = leave as-is, "" = clear


@router.get("/screens")
async def get_screens(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Editable built-in screens with their buttons (defaults merged with saved overrides)."""
    from src.bot.screen_buttons import SAFE_SCREENS, load_screen_configs, load_screen_texts
    from src.bot.screen_text import default_text_for, placeholders_for, sample_for

    async with container.uow() as uow:
        raw = str(await container.bot_config.value(uow, "SCREEN_BUTTONS") or "")
        raw_txt = str(await container.bot_config.value(uow, "SCREEN_TEXTS") or "")
    all_cfg = load_screen_configs(raw)
    all_txt = load_screen_texts(raw_txt)
    screens: list[dict[str, Any]] = []
    for sdef in SAFE_SCREENS:
        cfg = all_cfg.get(sdef.key) or {}
        items_cfg = cfg.get("items")
        if not isinstance(items_cfg, list):
            items_cfg = []
        defaults = {b.key: b for b in sdef.buttons}
        buttons: list[dict[str, Any]] = []
        seen: set[str] = set()
        for it in items_cfg:
            key = str(it.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            if bool(it.get("custom")):
                buttons.append(
                    {
                        "key": key,
                        "label": str(it.get("label") or ""),
                        "default_label": None,
                        "color": it.get("color"),
                        "icon": it.get("icon"),
                        "enabled": bool(it.get("enabled", True)),
                        "custom": True,
                        "action": it.get("action"),
                        "url": it.get("url"),
                        "row": it.get("row"),
                    }
                )
            else:
                d = defaults.get(key)
                buttons.append(
                    {
                        "key": key,
                        "label": str(it.get("label") or "") or (d.label if d else ""),
                        "default_label": d.label if d else None,
                        "color": it.get("color"),
                        "icon": it.get("icon"),
                        "enabled": bool(it.get("enabled", True)),
                        "custom": False,
                        "action": None,
                        "url": None,
                        "row": it.get("row"),
                    }
                )
        for b in sdef.buttons:
            if b.key not in seen:
                buttons.append(
                    {
                        "key": b.key,
                        "label": b.label,
                        "default_label": b.label,
                        "color": None,
                        "icon": None,
                        "enabled": True,
                        "custom": False,
                        "action": None,
                        "url": None,
                        "row": None,
                    }
                )
        per_row = cfg.get("per_row") if cfg.get("per_row") in (1, 2, 3) else 1
        layout = "custom" if cfg.get("layout") == "custom" else "uniform"
        screens.append(
            {
                "key": sdef.key,
                "title_ru": sdef.title_ru,
                "title_en": sdef.title_en,
                "buttons": buttons,
                "per_row": per_row,
                "layout": layout,
                "text": all_txt.get(sdef.key, ""),
                "text_default": default_text_for(sdef.key),
                "text_placeholders": placeholders_for(sdef.key),
                "text_sample": sample_for(sdef.key),
                "buttons_default": [
                    {
                        "key": b.key,
                        "label": b.label,
                        "default_label": b.label,
                        "color": None,
                        "icon": None,
                        "enabled": True,
                        "custom": False,
                        "action": None,
                        "url": None,
                        "row": None,
                    }
                    for b in sdef.buttons
                ],
                "per_row_default": 1,
                "layout_default": "uniform",
            }
        )
    return {"screens": screens}


@router.put("/screens/{screen_key}")
async def save_screen_buttons(
    screen_key: str,
    body: ScreenButtonsIn,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    import json as _json

    from src.bot.screen_buttons import SCREENS_BY_KEY, load_screen_configs, load_screen_texts

    sdef = SCREENS_BY_KEY.get(screen_key)
    if sdef is None:
        raise HTTPException(status_code=404, detail=f"unknown screen {screen_key!r}")
    valid_actions = {a.code for a in MENU_ACTIONS}
    default_keys = {b.key for b in sdef.buttons}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in body.items:
        key = (it.key or "").strip()
        if not key or key in seen:
            continue
        icon = (it.icon or "").strip()
        if icon and not (icon.isdigit() and 1 <= len(icon) <= 24):
            raise HTTPException(status_code=422, detail=f"button {key}: icon must be numeric")
        color = (it.color or "").strip()
        if it.custom:
            label = (it.label or "").strip()
            if not label:
                raise HTTPException(status_code=422, detail=f"button {key}: label required")
            action = (it.action or "").strip().lower()
            url = (it.url or "").strip()
            row: dict[str, Any] = {
                "key": key,
                "custom": True,
                "label": label,
                "enabled": bool(it.enabled),
            }
            if url:
                if not url.startswith(("http://", "https://", "tg://")):
                    raise HTTPException(status_code=422, detail=f"button {key}: bad url")
                row["url"] = url
            elif action:
                if action not in valid_actions:
                    raise HTTPException(
                        status_code=422, detail=f"button {key}: unknown action {action!r}"
                    )
                row["action"] = action
            else:
                raise HTTPException(status_code=422, detail=f"button {key}: needs action or url")
        else:
            if key not in default_keys:
                continue  # unknown system button - silently ignore
            row = {"key": key, "enabled": bool(it.enabled)}
            label = (it.label or "").strip()
            if label:
                row["label"] = label
        if color:
            row["color"] = color
        if icon:
            row["icon"] = icon
        if isinstance(it.row, int) and it.row >= 0:
            row["row"] = it.row
        out.append(row)
        seen.add(key)
    pr = body.per_row if body.per_row in (1, 2, 3) else 1
    layout = "custom" if body.layout == "custom" else "uniform"
    async with container.uow() as uow:
        raw = str(await container.bot_config.value(uow, "SCREEN_BUTTONS") or "")
        all_cfg = load_screen_configs(raw)
        all_cfg[screen_key] = {"per_row": pr, "layout": layout, "items": out}
        value = _json.dumps(all_cfg, ensure_ascii=False)
        values = {"SCREEN_BUTTONS": value}
        if body.text is not None:
            raw_txt = str(await container.bot_config.value(uow, "SCREEN_TEXTS") or "")
            all_txt = load_screen_texts(raw_txt)
            if body.text.strip():
                all_txt[screen_key] = body.text
            else:
                all_txt.pop(screen_key, None)
            values["SCREEN_TEXTS"] = _json.dumps(all_txt, ensure_ascii=False)
        await container.bot_config.set_values(uow, values)
        await audit(uow, identity, "menu.screen_buttons", None, screen=screen_key)
        await uow.commit()
    return {"ok": True}


@router.post("/reset-default")
async def reset_default(
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Replace the menu with the built-in default - a real, editable starting menu."""
    async with container.uow() as uow:
        await uow.menu_nodes.delete_by()
        for row in _default_menu_rows():
            await uow.menu_nodes.add(row)
        await audit(uow, identity, "menu.reset_default", None, count=len(DEFAULT_MENU))
        await uow.commit()
        nodes = list(await uow.menu_nodes.tree())
    return {"ok": True, "nodes": _serialize(nodes)}
