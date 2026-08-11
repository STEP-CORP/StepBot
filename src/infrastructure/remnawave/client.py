"""Concrete Remnawave HTTP client (httpx) implementing the RemnawaveClient protocol.

Retries transient failures with jittered backoff; raises hard on auth errors. Endpoint paths
and JSON field names are centralized in ``_PATHS`` / ``_PATHS_V3`` / the mappers below —
align them to your panel version if the smoke test reports a mismatch (docs/context/01).

Dual-contract: Remnawave 3.0 replaced the user uuid with a numeric id, renamed ip-control
to connections and dropped the by-telegram-id route. The client probes the panel version
once (``/api/system/metadata``) and routes every user-scoped call through the matching
contract, so the rest of the app never learns which panel generation it talks to.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.application.dto.panel import (
    PanelDevice,
    PanelNode,
    PanelRef,
    PanelSquad,
    PanelUser,
    PanelUserRef,
    PanelVersion,
    ProvisionSpec,
)
from src.core.constants import (
    MIN_REMNAWAVE_VERSION,
    PANEL_RETRY_ATTEMPTS,
    PANEL_RETRY_BASE_DELAY,
    PANEL_USERNAME_MAX,
    PANEL_USERNAME_PREFIX,
)
from src.core.exceptions import RemnawaveAuthError, RemnawaveError, RemnawaveTransientError
from src.core.logging import get_logger
from src.infrastructure.remnawave.connection import ConnectionProfile

log = get_logger(__name__)

_PATHS = {
    "health": "/api/system/health",
    "metadata": "/api/system/metadata",
    "stats": "/api/system/stats",
    "users": "/api/users",
    "user": "/api/users/{uuid}",
    "user_by_tg": "/api/users/by-telegram-id/{telegram_id}",
    "user_actions": "/api/users/{uuid}/actions/{action}",
    "internal_squads": "/api/internal-squads",
    "nodes": "/api/nodes",
}

# Remnawave >=3.0: users are addressed by their numeric id; ip-control became connections.
_PATHS_V3 = {
    "user": "/api/users/{id}",
    "user_actions": "/api/users/{id}/actions/{action}",
    "resolve": "/api/users/resolve",
    "users_stream": "/api/users/stream",
    "hwid_devices": "/api/hwid/devices/{id}",
    "connections_by_node": "/api/connections/by-node/{key}",
    "connections_drop": "/api/connections/drop",
}


def _unwrap(data: Any) -> Any:
    """Remnawave wraps payloads in ``{"response": ...}``; tolerate both shapes."""
    if isinstance(data, dict) and "response" in data:
        return data["response"]
    return data


def _as_ref(ref: PanelRef) -> PanelUserRef:
    """Normalize the accepted union: a bare uuid is a plain 2.x address."""
    if isinstance(ref, PanelUserRef):
        return ref
    return PanelUserRef(uuid=ref)


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _used_bytes(data: dict[str, Any]) -> int:
    """Read used traffic. Modern panels ship ``userTraffic`` as an OBJECT with
    ``usedTrafficBytes`` inside (numbers may arrive as JSON strings); older ones send it
    as a plain number; the oldest spell it ``trafficUsedBytes``/``usedTrafficBytes``
    at the top level. Missing the object keys here zeroed the counter on fresh panels —
    and the screen refresh then pinned the DB value at 0 over the webhook-written one."""
    value: Any = data.get("userTraffic")
    if isinstance(value, dict):
        value = (
            value.get("usedTrafficBytes")
            or value.get("total")
            or value.get("used")
            or value.get("usedBytes")
        )
    if not value:
        value = data.get("trafficUsedBytes") or data.get("usedTrafficBytes") or 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _numeric_id(value: Any) -> int | None:
    """The 3.0 numeric user id; tolerate numbers shipped as JSON strings."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _squad_uuids(raw: Any) -> tuple[str, ...]:
    """activeInternalSquads: 2.x ships uuid strings, 3.0 ships {uuid, name} objects."""
    out: list[str] = []
    for item in raw or []:
        value = item.get("uuid") if isinstance(item, dict) else item
        if value:
            out.append(str(value))
    return tuple(out)


def _to_panel_user(data: dict[str, Any]) -> PanelUser:
    # Field names verified against a live panel (see docs/context/01): shortUuid,
    # externalSquadUuid, userTraffic — with legacy fallbacks kept for older versions.
    # 3.0 payloads carry a numeric `id` and NO uuid; one of the two must be present.
    raw_uuid = data.get("uuid")
    panel_id = _numeric_id(data.get("id"))
    if raw_uuid is None and panel_id is None:
        raise ValueError("panel user payload carries neither uuid nor numeric id")
    return PanelUser(
        uuid=uuid.UUID(str(raw_uuid)) if raw_uuid is not None else None,
        short_id=str(data.get("shortUuid") or data.get("shortId") or ""),
        username=str(data.get("username") or ""),
        is_enabled=str(data.get("status", "ACTIVE")).upper() == "ACTIVE",
        expire_at=_parse_dt(data.get("expireAt") or data.get("expire_at")),
        traffic_limit_bytes=int(data.get("trafficLimitBytes") or 0),
        traffic_used_bytes=_used_bytes(data),
        device_limit=data.get("hwidDeviceLimit"),
        subscription_url=data.get("subscriptionUrl") or data.get("subscription_url"),
        telegram_id=data.get("telegramId"),
        internal_squads=_squad_uuids(data.get("activeInternalSquads")),
        external_squad=data.get("externalSquadUuid") or data.get("activeExternalSquad"),
        tag=data.get("tag"),
        panel_id=panel_id,
    )


def _v3_expire_at(expire_at: dt.datetime) -> str:
    """3.0 rejects a PATCH with expireAt in the past (400). The closest legal intent
    for an admin-driven "expire it now" is near-now: clamp to +2 minutes so the panel
    accepts the write and expires the user almost immediately."""
    now = dt.datetime.now(dt.UTC)
    if expire_at.astimezone(dt.UTC) <= now:
        return (now + dt.timedelta(minutes=2)).isoformat()
    return expire_at.astimezone(dt.UTC).isoformat()


def _spec_payload(spec: ProvisionSpec) -> dict[str, Any]:
    # Create/update INPUT field names. Read-side names are panel-verified; the write side
    # still needs confirmation on a test panel (do not test writes on production).
    payload: dict[str, Any] = {
        "username": spec.username,
        "expireAt": spec.expire_at.astimezone(dt.UTC).isoformat(),
        "trafficLimitBytes": spec.traffic_limit_bytes,
    }
    # Send squad membership ONLY when we actually have one. An empty list REPLACES the
    # panel set with none (kicking the user off every server) — fatal on update/resync for
    # migrated subs whose local squad list is empty while the adopted panel user has real
    # squads. Empty ⇒ omit ⇒ "leave alone" (same rule as device_limit/external_squad below).
    if spec.internal_squads:
        payload["activeInternalSquads"] = list(spec.internal_squads)
    if spec.telegram_id is not None:
        payload["telegramId"] = spec.telegram_id
    # device_limit / external_squad follow the same omit ⇒ "leave alone" rule as squads,
    # EXCEPT on a plan CHANGE: the reset_* flags let the new plan actively clear a stale
    # cap/exit that the old plan set (unlimited devices ⇒ 0, no exit ⇒ null). Without them a
    # None/falsy value is omitted and the panel keeps the old value (create/renew rely on that).
    if spec.device_limit is not None:
        payload["hwidDeviceLimit"] = spec.device_limit
    elif spec.reset_device_limit:
        payload["hwidDeviceLimit"] = 0  # Remnawave: 0 == unlimited devices
    if spec.external_squad:
        payload["externalSquadUuid"] = spec.external_squad
    elif spec.reset_external_squad:
        payload["externalSquadUuid"] = None
    if spec.description:
        payload["description"] = spec.description
    # Caller-supplied passthrough fields (e.g. vlessUuid/shortUuid/status on import).
    payload.update(spec.extra)
    return payload


class RemnawaveHttpClient:
    """Async httpx client. Construct via :meth:`from_profile` (or DI)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        profile: ConnectionProfile,
        profile_loader: Callable[[], Awaitable[ConnectionProfile]] | None = None,
    ) -> None:
        self._http = http
        self._profile = profile
        self._profile_loader = profile_loader
        self._profile_lock = asyncio.Lock()
        # API generation: 2 or 3, probed lazily from the panel version. None = not yet known.
        self._mode: int | None = None
        self._mode_lock = asyncio.Lock()
        # short_id -> numeric 3.0 user id, filled by resolve calls for pre-3.0 subscriptions.
        self._id_cache: dict[str, int] = {}

    @classmethod
    def from_profile(
        cls,
        profile: ConnectionProfile,
        *,
        timeout: float = 15.0,
        profile_loader: Callable[[], Awaitable[ConnectionProfile]] | None = None,
    ) -> RemnawaveHttpClient:
        client = httpx.AsyncClient(
            base_url=profile.base_url,
            headers=profile.headers,
            cookies=profile.cookies,
            verify=profile.verify,
            timeout=timeout,
        )
        return cls(client, profile=profile, profile_loader=profile_loader)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def reload_profile(self, profile: ConnectionProfile) -> None:
        """Apply a new connection profile without restarting the process."""
        async with self._profile_lock:
            if profile == self._profile:
                return
            replacement = httpx.AsyncClient(
                base_url=profile.base_url,
                headers=profile.headers,
                cookies=profile.cookies,
                verify=profile.verify,
                timeout=self._http.timeout,
            )
            previous = self._http
            self._http = replacement
            self._profile = profile
            self._mode = None
            self._id_cache.clear()
            await previous.aclose()

    async def _ensure_current_profile(self) -> None:
        if self._profile_loader is None:
            return
        try:
            profile = await self._profile_loader()
            await self.reload_profile(profile)
        except Exception as exc:
            # Keep using the last known-good client when the config database is temporarily
            # unavailable. The request itself will still report a real panel/transport error.
            log.warning("remnawave profile refresh failed", error=str(exc))

    # --- low-level request with retry -------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        await self._ensure_current_profile()
        last_exc: Exception | None = None
        for attempt in range(1, PANEL_RETRY_ATTEMPTS + 1):
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:  # timeouts, connection errors
                last_exc = RemnawaveTransientError(str(exc))
            else:
                if resp.status_code in (401, 403):
                    raise RemnawaveAuthError(f"panel rejected credentials ({resp.status_code})")
                if resp.status_code >= 500:
                    last_exc = RemnawaveTransientError(f"panel {resp.status_code}")
                elif resp.status_code >= 400:
                    raise RemnawaveError(f"panel {resp.status_code}: {resp.text[:200]}")
                else:
                    return _unwrap(resp.json()) if resp.content else None
            # backoff before retrying a transient failure
            if attempt < PANEL_RETRY_ATTEMPTS:
                delay = PANEL_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
        raise last_exc or RemnawaveTransientError("panel request failed")

    # --- protocol methods -------------------------------------------------
    async def get_version(self) -> PanelVersion:
        # /api/system/metadata reports the version on every 2.8+ AND 3.x backend, while
        # /health stopped carrying it (3.0 health is runtime metrics only). Probe metadata
        # first; fall back to health for panels that predate the metadata route.
        raw = ""
        try:
            data = await self._request("GET", _PATHS["metadata"])
            if isinstance(data, dict):
                raw = str(data.get("version") or "")
        except RemnawaveAuthError:
            raise  # bad credentials — never mask this as "no version"
        except RemnawaveError:
            pass
        if not raw:
            data = await self._request("GET", _PATHS["health"])
            if isinstance(data, dict):
                raw = str(data.get("version") or data.get("appVersion") or "")
        parts = [int(p) for p in raw.split(".")[:3] if p.isdigit()]
        known = bool(parts)  # did we actually read a version string?
        while len(parts) < 3:
            parts.append(0)
        major, minor, patch = parts[0], parts[1], parts[2]
        caps: set[str] = set()
        # Capability-probe, not a pin: only assume the legacy behaviour when the panel
        # *told us* it's old. Newer backends (v2) don't expose a version here — an
        # unreadable version must NOT be treated as pre-2.8 (that added happ_encrypt,
        # which 2.x rejects). Unknown → assume modern.
        if known and (major, minor, patch) < MIN_REMNAWAVE_VERSION:
            caps.add("happ_encrypt")  # removed in 2.8.0
        if major >= 3:
            # 3.0 replaced the REST contract: numeric user ids instead of uuids,
            # ip-control renamed to connections, by-telegram-id dropped, etc.
            caps.add("v3_api")
        if known:
            self._mode = 3 if major >= 3 else 2
        return PanelVersion(
            raw=raw or "0.0.0", major=major, minor=minor, patch=patch, capabilities=frozenset(caps)
        )

    async def _is_v3(self) -> bool:
        """Resolve the API generation once; every user-scoped call routes through this.

        A probe that ERRORS leaves the mode unknown (the actual call will fail and be
        retried later) — only a SUCCESSFUL probe with no readable version pins 2.x, since
        every 3.0 backend does report its version via /metadata while some live 2.x
        panels expose none at all.
        """
        if self._mode is None:
            async with self._mode_lock:
                if self._mode is None:
                    await self.get_version()
                    if self._mode is None:
                        self._mode = 2
        return self._mode >= 3

    def _v2_uuid(self, ref: PanelRef) -> uuid.UUID:
        panel_uuid = _as_ref(ref).uuid
        if panel_uuid is None:
            raise RemnawaveError("panel user reference carries no 2.x uuid")
        return panel_uuid

    async def _v3_id(self, ref: PanelRef) -> int:
        """Numeric 3.0 user id for a ref; re-resolves it for uuid-only subscriptions.

        A sub provisioned on 2.x knows only its uuid — which a 3.0 panel no longer
        accepts anywhere. POST /users/resolve maps username (bot-created users are
        ``sub_<short_id>``) or the panel shortUuid (imported subs stored it as their
        short_id) back to the numeric id; the answer is cached per client instance
        and persisted by the webhook backfill / resync paths upstream.
        """
        r = _as_ref(ref)
        if r.panel_id is not None:
            return r.panel_id
        if r.short_id:
            cached = self._id_cache.get(r.short_id)
            if cached is not None:
                return cached
            username = f"{PANEL_USERNAME_PREFIX}{r.short_id}"[:PANEL_USERNAME_MAX]
            for body in ({"username": username}, {"shortUuid": r.short_id}):
                try:
                    data = await self._request("POST", _PATHS_V3["resolve"], json=body)
                except (RemnawaveAuthError, RemnawaveTransientError):
                    raise  # panel unreachable / creds bad — not "user unknown"
                except RemnawaveError:
                    continue  # not found under this key — try the next one
                panel_id = _numeric_id(data.get("id")) if isinstance(data, dict) else None
                if panel_id is not None:
                    self._id_cache[r.short_id] = panel_id
                    return panel_id
        raise RemnawaveError("panel v3: cannot resolve the numeric user id from this reference")

    async def create_user(self, spec: ProvisionSpec) -> PanelUser:
        data = await self._request("POST", _PATHS["users"], json=_spec_payload(spec))
        return _to_panel_user(dict(data))

    async def update_user(self, ref: PanelRef, spec: ProvisionSpec) -> PanelUser:
        if await self._is_v3():
            # 3.0 PATCHes the collection with the numeric id in the body — and refuses
            # an expireAt in the past, so "expire immediately" is clamped to near-now.
            payload = _spec_payload(spec) | {"id": await self._v3_id(ref)}
            payload["expireAt"] = _v3_expire_at(spec.expire_at)
            data = await self._request("PATCH", _PATHS["users"], json=payload)
            return _to_panel_user(dict(data))
        # Backend v2 updates a user via PATCH /api/users with the uuid IN THE BODY —
        # PATCH /api/users/{uuid} 404s. (Verified against a live 2.x panel.)
        payload = _spec_payload(spec) | {"uuid": str(self._v2_uuid(ref))}
        data = await self._request("PATCH", _PATHS["users"], json=payload)
        return _to_panel_user(dict(data))

    async def get_user(self, ref: PanelRef) -> PanelUser | None:
        try:
            if await self._is_v3():
                path = _PATHS_V3["user"].format(id=await self._v3_id(ref))
            else:
                path = _PATHS["user"].format(uuid=self._v2_uuid(ref))
            data = await self._request("GET", path)
        except RemnawaveError:
            return None
        return _to_panel_user(dict(data)) if data else None

    async def get_user_by_telegram_id(self, telegram_id: int) -> PanelUser | None:
        try:
            if await self._is_v3():
                # by-telegram-id is gone in 3.0; the stream route filters by telegramId.
                data = await self._request(
                    "GET",
                    _PATHS_V3["users_stream"],
                    params={"telegramId": str(telegram_id), "size": 25},
                )
                data = data.get("users") if isinstance(data, dict) else data
            else:
                data = await self._request(
                    "GET", _PATHS["user_by_tg"].format(telegram_id=telegram_id)
                )
        except RemnawaveError:
            return None
        if not data:
            return None
        if isinstance(data, list):
            return _to_panel_user(dict(data[0])) if data else None
        return _to_panel_user(dict(data))

    async def _action(self, ref: PanelRef, action: str) -> Any:
        if await self._is_v3():
            path = _PATHS_V3["user_actions"].format(id=await self._v3_id(ref), action=action)
        else:
            path = _PATHS["user_actions"].format(uuid=self._v2_uuid(ref), action=action)
        return await self._request("POST", path)

    async def enable_user(self, ref: PanelRef) -> None:
        await self._action(ref, "enable")

    async def disable_user(self, ref: PanelRef) -> None:
        await self._action(ref, "disable")

    async def delete_user(self, ref: PanelRef) -> None:
        if await self._is_v3():
            path = _PATHS_V3["user"].format(id=await self._v3_id(ref))
        else:
            path = _PATHS["user"].format(uuid=self._v2_uuid(ref))
        await self._request("DELETE", path)

    async def reset_traffic(self, ref: PanelRef) -> None:
        await self._action(ref, "reset-traffic")

    async def revoke_subscription(self, ref: PanelRef) -> PanelUser:
        data = await self._action(ref, "revoke")
        return _to_panel_user(dict(data))

    async def drop_connections(self, ref: PanelRef) -> None:
        if await self._is_v3():
            # Dedicated connections API in 3.0 (the per-user action was removed).
            try:
                await self._request(
                    "POST",
                    _PATHS_V3["connections_drop"],
                    json={
                        "dropBy": {"by": "userIds", "userIds": [await self._v3_id(ref)]},
                        "targetNodes": {"target": "allNodes"},
                    },
                )
            except RemnawaveError as exc:
                # A panel with zero connected nodes 404s here ("Connected nodes not
                # found", seen live on 3.0.0). Nothing to drop IS the desired end
                # state — swallowing keeps link-reset/device-guard flows quiet.
                if "connected nodes not found" not in str(exc).lower():
                    raise
            return
        await self._action(ref, "drop-connections")

    async def get_devices(self, ref: PanelRef) -> list[PanelDevice]:
        """HWID devices of one panel user (uuid path on 2.x, numeric id on 3.0)."""
        if await self._is_v3():
            data = await self._request(
                "GET", _PATHS_V3["hwid_devices"].format(id=await self._v3_id(ref))
            )
        else:
            data = await self._request("GET", f"/api/hwid/devices/{self._v2_uuid(ref)}")
        raw = data.get("devices") if isinstance(data, dict) else data
        devices: list[PanelDevice] = []
        for item in raw or []:
            if not isinstance(item, dict) or not item.get("hwid"):
                continue
            devices.append(
                PanelDevice(
                    hwid=str(item["hwid"]),
                    platform=item.get("platform"),
                    device_model=item.get("deviceModel") or item.get("model"),
                    created_at=item.get("createdAt"),
                )
            )
        return devices

    async def delete_device(self, ref: PanelRef, hwid: str) -> None:
        """Unbind one HWID (POST /api/hwid/devices/delete — panel-verified route)."""
        if await self._is_v3():
            body: dict[str, Any] = {"userId": await self._v3_id(ref), "hwid": hwid}
        else:
            body = {"userUuid": str(self._v2_uuid(ref)), "hwid": hwid}
        await self._request("POST", "/api/hwid/devices/delete", json=body)

    async def start_users_ips_job(self, node_uuid: str) -> str:
        """Kick the panel's online-IP collection for a node.

        2.x calls this ip-control; 3.0 renamed the module to connections. Same
        job-then-poll flow on both.
        """
        if await self._is_v3():
            path = _PATHS_V3["connections_by_node"].format(key=node_uuid)
        else:
            path = f"/api/ip-control/fetch-users-ips/{node_uuid}"
        data = await self._request("POST", path)
        return str((data or {}).get("jobId") or "")

    async def get_users_ips_result(self, job_id: str) -> list[tuple[str, list[str]]] | None:
        """None while the job is running; [(userId, [ips])] when completed."""
        if await self._is_v3():
            path = _PATHS_V3["connections_by_node"].format(key=job_id)
        else:
            path = f"/api/ip-control/fetch-users-ips/result/{job_id}"
        data = await self._request("GET", path)
        data = data or {}
        if not data.get("isCompleted"):
            return None
        if data.get("isFailed"):
            return []
        users = (data.get("result") or {}).get("users") or []
        out: list[tuple[str, list[str]]] = []
        for u in users:
            ips = [str(i.get("ip")) for i in (u.get("ips") or []) if i.get("ip")]
            if u.get("userId"):
                out.append((str(u["userId"]), ips))
        return out

    async def get_internal_squads(self) -> list[PanelSquad]:
        data = await self._request("GET", _PATHS["internal_squads"])
        items = data.get("internalSquads", data) if isinstance(data, dict) else data
        return [
            PanelSquad(
                uuid=uuid.UUID(str(s["uuid"])),
                name=str(s.get("name") or ""),
                # 2.x: top-level membersCount; 3.0 nests it under info.
                members_count=int(
                    s.get("membersCount") or (s.get("info") or {}).get("membersCount") or 0
                ),
            )
            for s in (items or [])
        ]

    async def get_nodes(self) -> list[PanelNode]:
        data = await self._request("GET", _PATHS["nodes"])
        items = data if isinstance(data, list) else data.get("nodes", []) if data else []
        return [
            PanelNode(
                uuid=uuid.UUID(str(n["uuid"])),
                name=str(n.get("name") or ""),
                is_online=bool(n.get("isConnected") or n.get("isOnline")),
                country_code=(n.get("countryCode") or None),
                address=(n.get("address") or None),
                users_online=int(n.get("usersOnline") or 0),
                traffic_used_bytes=int(n.get("trafficUsedBytes") or 0),
                is_disabled=bool(n.get("isDisabled")),
            )
            for n in items
        ]
