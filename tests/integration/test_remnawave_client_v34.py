"""RemnawaveHttpClient against payloads shaped like Remnawave 3.4.4.

3.1 through 3.4 kept the user/hwid/connections/system routes of 3.0 intact but grew the
responses: nodes carry a numeric ``id`` next to their uuid plus ``ips``/``tags``/
``integrationUuids``, squads carry ``tags``, the stream route reports ``nextCursor``,
connection results carry ``lastSeen``. These tests pin that the extra fields change
nothing: the client still speaks the 3.0 contract and maps the same DTOs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import respx

from src.application.dto.panel import PanelUserRef
from src.core.config.remnawave import PanelAuthType, RemnawaveSettings
from src.infrastructure.remnawave.client import RemnawaveHttpClient
from src.infrastructure.remnawave.connection import build_profile
from src.infrastructure.remnawave.webhook import WebhookVerifier

BASE = "https://panel.example.com"


def _client() -> RemnawaveHttpClient:
    cfg = RemnawaveSettings(base_url=BASE, auth_type=PanelAuthType.API_KEY, token="secret")
    return RemnawaveHttpClient.from_profile(build_profile(cfg))


def _mock_metadata() -> respx.Route:
    return respx.get(f"{BASE}/api/system/metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "version": "3.4.4",
                    "build": {"time": "2026-09-12T15:00:00Z", "number": "1"},
                    "git": {
                        "backend": {"commitSha": "abc", "branch": "main", "commitUrl": ""},
                        "frontend": {"commitSha": "def", "commitUrl": ""},
                    },
                }
            },
        )
    )


def _user(panel_id: int = 101) -> dict[str, object]:
    return {
        "id": panel_id,
        "shortUuid": "q" * 16,
        "username": "sub_abcdef1234",
        "status": "ACTIVE",
        "trafficLimitBytes": 0,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": "2026-12-31T00:00:00.000Z",
        "telegramId": 4242,
        "hwidDeviceLimit": 3,
        "externalSquadUuid": None,
        # 3.2.2+ validates vlessUuid as a loose guid, not a strict RFC uuid
        "vlessUuid": "00000000-0000-0000-0000-000000000001",
        "userTraffic": {"usedTrafficBytes": "2048", "lifetimeUsedTrafficBytes": "4096"},
        "activeInternalSquads": [{"uuid": str(uuid.uuid4()), "name": "eu"}],
        "subscriptionUrl": f"{BASE}/sub/qqqq",
    }


@respx.mock
async def test_v34_version_probe_selects_v3_contract() -> None:
    _mock_metadata()
    client = _client()
    try:
        version = await client.get_version()
    finally:
        await client.aclose()
    assert (version.major, version.minor, version.patch) == (3, 4, 4)
    assert version.capabilities == frozenset({"v3_api"})


@respx.mock
async def test_v34_user_calls_keep_the_3_0_routes() -> None:
    _mock_metadata()
    get = respx.get(f"{BASE}/api/users/101").mock(
        return_value=httpx.Response(200, json={"response": _user(101)})
    )
    stream = respx.get(f"{BASE}/api/users/stream").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"users": [_user(101)], "nextCursor": None, "hasMore": False}},
        )
    )
    resolve = respx.post(f"{BASE}/api/users/resolve").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"id": 101, "username": "sub_abcdef1234", "shortUuid": "q" * 16}},
        )
    )
    revoke = respx.post(f"{BASE}/api/users/101/actions/revoke").mock(
        return_value=httpx.Response(200, json={"response": _user(101)})
    )
    client = _client()
    try:
        by_id = await client.get_user(PanelUserRef(panel_id=101))
        by_tg = await client.get_user_by_telegram_id(4242)
        revoked = await client.revoke_subscription(
            PanelUserRef(uuid=uuid.uuid4(), short_id="abcdef1234")
        )
    finally:
        await client.aclose()
    assert get.called and stream.called and revoke.called
    assert json.loads(resolve.calls.last.request.content) == {"username": "sub_abcdef1234"}
    for user in (by_id, by_tg, revoked):
        assert user is not None
        assert user.panel_id == 101
        assert user.uuid is None
        assert user.traffic_used_bytes == 2048
        assert user.device_limit == 3
        assert len(user.internal_squads) == 1


@respx.mock
async def test_v34_nodes_with_numeric_id_keep_uuid_identity() -> None:
    # 3.2.2 added `ips`, 3.3 `integrationUuids`, 3.4 a numeric `id`: the node is still
    # addressed by its uuid everywhere the bot uses it (connections by-node, squads).
    node_uuid = str(uuid.uuid4())
    respx.get(f"{BASE}/api/nodes").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": [
                    {
                        "uuid": node_uuid,
                        "id": 7,
                        "name": "DE-1",
                        "address": "de1.example.com",
                        "port": 2222,
                        "isConnected": True,
                        "isDisabled": False,
                        "isConnecting": False,
                        "countryCode": "DE",
                        "usersOnline": 5,
                        "trafficUsedBytes": 1000,
                        "tags": ["EU"],
                        "integrationUuids": [],
                        "ips": [{"ip": "192.0.2.10", "status": "INBOUND"}],
                    }
                ]
            },
        )
    )
    client = _client()
    try:
        nodes = await client.get_nodes()
    finally:
        await client.aclose()
    assert len(nodes) == 1
    assert str(nodes[0].uuid) == node_uuid
    assert nodes[0].is_online is True
    assert nodes[0].users_online == 5
    assert nodes[0].country_code == "DE"


@respx.mock
async def test_v34_squads_with_tags_still_map() -> None:
    respx.get(f"{BASE}/api/internal-squads").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "total": 1,
                    "internalSquads": [
                        {
                            "uuid": str(uuid.uuid4()),
                            "viewPosition": 0,
                            "name": "eu-west",
                            "tags": ["PREMIUM"],
                            "info": {"membersCount": 4, "inboundsCount": 1},
                            "inbounds": [],
                        }
                    ],
                }
            },
        )
    )
    client = _client()
    try:
        squads = await client.get_internal_squads()
    finally:
        await client.aclose()
    assert [(s.name, s.members_count) for s in squads] == [("eu-west", 4)]


@respx.mock
async def test_v34_connections_result_with_last_seen() -> None:
    _mock_metadata()
    node = str(uuid.uuid4())
    respx.post(f"{BASE}/api/connections/by-node/{node}").mock(
        return_value=httpx.Response(201, json={"response": {"jobId": "job-1"}})
    )
    respx.get(f"{BASE}/api/connections/by-node/job-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "isCompleted": True,
                    "isFailed": False,
                    "result": {
                        "success": True,
                        "nodeUuid": node,
                        "users": [
                            {
                                "userId": 101,
                                "ips": [{"ip": "198.51.100.1", "lastSeen": "2026-09-28T10:00:00Z"}],
                            }
                        ],
                    },
                }
            },
        )
    )
    client = _client()
    try:
        job_id = await client.start_users_ips_job(node)
        result = await client.get_users_ips_result(job_id)
    finally:
        await client.aclose()
    assert result == [("101", ["198.51.100.1"])]


def test_v34_webhook_envelope_parses_user_event() -> None:
    # 3.x sends {scope, event, timestamp, data, meta}, signed with HMAC-SHA256 of the body.
    body = json.dumps(
        {
            "scope": "user",
            "event": "user.deleted",
            "timestamp": "2026-09-28T10:00:00.000Z",
            "data": _user(101),
            "meta": None,
        }
    ).encode()
    signature = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    verifier = WebhookVerifier("s3cret")
    verifier.verify(body, {"X-Remnawave-Signature": signature})
    event = verifier.parse(body)
    assert event.event == "user.deleted"
    assert event.payload["id"] == 101
