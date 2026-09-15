"""Hermetic payment boundary regressions; dummy signatures NEVER reach a live service."""
import asyncio
import base64
import copy
from decimal import Decimal
import json
import time
from unittest.mock import AsyncMock, patch
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import server
from x402_payment import DEFAULT_PAYEE, Facilitator, NETWORK, USDC_ASSET, configured_payee, encode_header

PAYER = "0x1111111111111111111111111111111111111111"
TX = "0x" + "ab" * 32
FAKE_HEADERS = ["Authorization", "Payment-Receipt", "Payment-Response", "X-PAYMENT", "x-payment-response"]
OPS = server.payment.operations
ROUTES = [(method, path) for path in server.payment.paths for method in ("GET", "POST", "HEAD")]


def decode_header(response, name="PAYMENT-REQUIRED"):
    return json.loads(base64.b64decode(response.headers[name]))


def request(client, operation, *, headers=None, method=None, path=None):
    method = method or operation.method
    kwargs = {"json": operation.example} if method == "POST" else {"params": operation.example}
    return client.request(method, path or operation.path, headers=headers, **kwargs)


def payload_for(client, op, *, method=None, path=None):
    challenge = decode_header(request(client, op, method=method, path=path))
    return {
        "x402Version": 2, "resource": challenge["resource"], "accepted": challenge["accepts"][0],
        "extensions": challenge.get("extensions", {}),
        "payload": {"signature": "0x" + "11" * 65, "authorization": {
            "from": PAYER, "to": server.payment.payee, "value": server.payment.amount,
            "validAfter": str(int(time.time()) - 10), "validBefore": str(int(time.time()) + 55),
            "nonce": "0x" + uuid.uuid4().hex + uuid.uuid4().hex,
        }},
    }


def business_fixture():
    service = server.payment.service.lower()
    if "gas" in service:
        return "fetch_base_fee_history", {"oldestBlock": "0x64", "baseFeePerGas": ["0x100"] * 6,
                "reward": [["0x1", "0x2", "0x3", "0x4"]] * 5, "gasUsedRatio": [0.5] * 5}
    if "shield" in service:
        return "screen_token", {"success": True, "analysis_type": "static_onchain_heuristic", "risk_score": 0}
    if "price" in service:
        return "get_market_data", {"price_usd": Decimal("100"), "source": "mock_market",
            "source_timestamp": None, "fetched_at": "2026-09-16T00:00:00+00:00",
            "dex_id": "mock", "pair_address": PAYER, "liquidity_usd": Decimal("1000000"),
            "volume_24h_usd": None, "price_change_24h_pct": None}
    if "whale" in service:
        return "fetch_onchain_whales", {"latest_block": 100, "blocks_scanned": 25,
            "whale_count": 0, "total_whale_volume_usd": 0, "largest_single_transfer_usd": 0,
            "average_whale_transfer_usd": 0, "transactions": [], "source": "base_rpc_eth_getLogs",
            "valuation_source": "stablecoin_peg_assumption", "fetched_at": "2026-09-16T00:00:00+00:00"}
    if "yield" in service:
        return "fetch_base_pools", {"pools": [], "source": "DefiLlama", "source_timestamp": None,
                "fetched_at": "2026-09-16T00:00:00+00:00"}
    return "fetch_ensideas_record", {"address": PAYER, "name": "test.base.eth", "displayName": "test.base.eth", "avatar": None}


@pytest.fixture
def boundary():
    gate = server.payment
    calls, events = [], []
    state = {"verify": {"isValid": True, "payer": PAYER},
             "settle": {"success": True, "transaction": TX, "network": NETWORK, "payer": PAYER}}

    def transport(req):
        operation = req.url.path.rsplit("/", 1)[-1]
        events.append(operation)
        calls.append((operation, json.loads(req.content)))
        value = state[operation]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json=value)

    name, data = business_fixture()

    async def business(*args, **kwargs):
        events.append("business")
        return copy.deepcopy(data)

    spy = AsyncMock(side_effect=business)
    gate._seen.clear()
    facilitator = Facilitator(url="https://facilitator.invalid", transport=httpx.MockTransport(transport))
    with patch.object(gate, "facilitator", facilitator), patch.object(server, name, spy):
        with TestClient(server.app) as client:
            yield client, spy, calls, events, state
    gate._seen.clear()


@pytest.mark.parametrize("path", ["/", "/health", "/.well-known/x402", "/openapi.json"])
def test_public_endpoints_need_no_payment(boundary, path):
    client, business, calls, _, _ = boundary
    assert client.get(path).status_code == 200
    business.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.path)
def test_unpaid_challenge_matches_authoritative_requirements(boundary, op):
    client, business, calls, _, _ = boundary
    response = request(client, op)
    assert response.status_code == 402
    challenge = decode_header(response)
    assert challenge["x402Version"] == 2
    assert challenge["accepts"] == [{"scheme": "exact", "network": NETWORK, "asset": USDC_ASSET,
        "amount": server.payment.amount, "payTo": server.payment.payee, "maxTimeoutSeconds": 60,
        "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}}]
    assert "PAYMENT-RESPONSE" not in response.headers
    business.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("header", FAKE_HEADERS)
@pytest.mark.parametrize("method,path", ROUTES)
def test_fake_header_does_not_unlock_resource(boundary, header, method, path):
    client, business, calls, _, _ = boundary
    response = request(client, server.payment.paths[path], method=method, path=path, headers={header: "hello"})
    assert response.status_code == 402
    assert "PAYMENT-REQUIRED" in response.headers
    assert "PAYMENT-RESPONSE" not in response.headers
    business.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("malformed", ["garbage", "", "!!!", "e30=", "bnVsbA==", "W10=", "A" * 17000])
@pytest.mark.parametrize("op", OPS, ids=lambda op: op.path)
def test_malformed_signature_never_executes_business(boundary, malformed, op):
    client, business, calls, _, _ = boundary
    result = request(client, op, headers={"PAYMENT-SIGNATURE": malformed})
    assert result.status_code == 400
    assert "PAYMENT-RESPONSE" not in result.headers
    business.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("network", "eip155:84532"), ("amount", "1"), ("asset", PAYER),
    ("payTo", PAYER), ("scheme", "upto"), ("maxTimeoutSeconds", 3600),
])
def test_wrong_payment_requirements_rejected_before_verify(boundary, field, value):
    client, business, calls, _, _ = boundary
    payload = payload_for(client, OPS[0])
    payload["accepted"][field] = value
    result = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert result.status_code == 400
    business.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("mutation", ["resource", "no_resource", "version", "signature", "recipient", "value", "expired", "future", "nonce", "permit2"])
def test_invalid_binding_or_authorization_rejected(boundary, mutation):
    client, business, calls, _, _ = boundary
    payload = payload_for(client, OPS[0])
    auth = payload["payload"]["authorization"]
    if mutation == "resource":
        payload["resource"]["url"] = "http://testserver/v1/different"
    elif mutation == "no_resource":
        del payload["resource"]
    elif mutation == "version":
        payload["x402Version"] = 1
    elif mutation == "signature":
        payload["payload"]["signature"] = "garbage"
    elif mutation == "recipient":
        auth["to"] = PAYER
    elif mutation == "value":
        auth["value"] = "1"
    elif mutation == "expired":
        auth["validBefore"] = "1"
    elif mutation == "future":
        auth["validAfter"] = str(int(time.time()) + 999)
    elif mutation == "nonce":
        auth["nonce"] = "0x11"
    elif mutation == "permit2":
        payload["accepted"]["extra"]["assetTransferMethod"] = "permit2"
    result = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert result.status_code == 400
    business.assert_not_awaited()
    assert calls == []


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.path)
def test_verification_failure_does_not_execute_protected_logic(boundary, op):
    client, business, calls, _, state = boundary
    state["verify"] = {"isValid": False, "invalidReason": "secret-upstream-diagnostic"}
    result = request(client, op, headers={"PAYMENT-SIGNATURE": encode_header(payload_for(client, op))})
    assert result.status_code == 402
    assert result.json()["error"]["code"] == "payment_verification_failed"
    assert "secret" not in result.text
    assert [call[0] for call in calls] == ["verify"]
    business.assert_not_awaited()


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.path)
def test_verified_and_settled_payment_releases_resource(boundary, op):
    client, business, calls, events, _ = boundary
    payload = payload_for(client, op)
    result = request(client, op, headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert result.status_code == 200, result.text
    assert business.await_count > 0
    assert events[0] == "verify" and events[-1] == "settle"
    assert events.index("verify") < events.index("business") < events.index("settle")
    assert decode_header(result, "PAYMENT-RESPONSE") == {"success": True, "transaction": TX, "network": NETWORK, "payer": PAYER}
    assert result.headers["cache-control"] == "no-store"
    assert len(calls) == 2
    for _, body in calls:
        assert body["x402Version"] == 2
        assert body["paymentRequirements"] == payload["accepted"]
        assert body["paymentPayload"]["resource"]["url"] == payload["resource"]["url"]


@pytest.mark.parametrize("op", OPS, ids=lambda op: op.path)
def test_settlement_failure_never_returns_paid_content(boundary, op):
    client, business, calls, _, state = boundary
    state["settle"] = {"success": False, "errorReason": "secret-settlement-error", "network": NETWORK, "transaction": ""}
    result = request(client, op, headers={"PAYMENT-SIGNATURE": encode_header(payload_for(client, op))})
    assert result.status_code == 402
    assert result.json()["success"] is False
    assert result.json()["error"]["code"] == "payment_settlement_failed"
    assert "PAYMENT-RESPONSE" not in result.headers
    assert "secret" not in result.text
    assert business.await_count > 0
    assert [call[0] for call in calls] == ["verify", "settle"]


@pytest.mark.parametrize("value", [
    {}, {"isValid": "true"}, {"isValid": 1}, [],
    httpx.Response(200, content=b"not-json"), httpx.Response(500, content=b"secret"),
    httpx.Response(307, headers={"Location": "https://untrusted.invalid"}),
    httpx.ConnectError("secret connection failure"), httpx.ReadTimeout("secret timeout"),
])
def test_facilitator_failure_is_fail_closed_before_business(boundary, value):
    client, business, _, _, state = boundary
    state["verify"] = value
    result = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload_for(client, OPS[0]))})
    assert result.status_code == 503
    assert "PAYMENT-RESPONSE" not in result.headers
    assert "secret" not in result.text
    business.assert_not_awaited()


@pytest.mark.parametrize("value", [
    {}, {"success": "true", "network": NETWORK, "transaction": TX},
    {"success": True, "network": "eip155:84532", "transaction": TX},
    {"success": True, "network": NETWORK, "transaction": ""},
    {"success": True, "network": NETWORK, "transaction": TX, "amount": "1"},
    httpx.ReadTimeout("secret settlement timeout"),
])
def test_malformed_or_uncertain_settlement_never_releases_success(boundary, value):
    client, _, _, _, state = boundary
    state["settle"] = value
    result = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload_for(client, OPS[0]))})
    assert result.status_code in (402, 503)
    assert result.json()["success"] is False
    assert "PAYMENT-RESPONSE" not in result.headers
    assert "secret" not in result.text


def test_successful_nonce_cannot_be_reused(boundary):
    client, business, calls, _, _ = boundary
    headers = {"PAYMENT-SIGNATURE": encode_header(payload_for(client, OPS[0]))}
    assert request(client, OPS[0], headers=headers).status_code == 200
    count = business.await_count
    result = request(client, OPS[0], headers=headers)
    assert result.status_code == 402
    assert result.json()["error"]["code"] == "payment_replayed"
    assert business.await_count == count
    assert len(calls) == 2


def test_missing_facilitator_configuration_does_not_unlock(boundary):
    client, business, calls, _, _ = boundary
    server.payment.facilitator.url = ""
    result = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload_for(client, OPS[0]))})
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "payment_configuration_missing"
    business.assert_not_awaited()
    assert calls == []


def test_discovery_is_canonical_and_openapi_has_operation_metadata(boundary):
    client, _, _, _, _ = boundary
    manifest = client.get("/.well-known/x402").json()
    resources = manifest["resources"]
    assert {(r["method"], r["path"]) for r in resources} == {(op.method, op.path) for op in OPS}
    assert len(resources) == len(OPS)
    schema = client.get("/openapi.json").json()
    for op in OPS:
        assert set(schema["paths"][op.path]) == {op.method.lower()}
        operation = schema["paths"][op.path][op.method.lower()]
        assert operation["x-payment-info"]["x402Version"] == 2
        assert operation["x-payment-info"]["accepts"][0]["payTo"] == server.payment.payee
        assert "402" in operation["responses"]
    for alias in server.payment.aliases:
        assert alias not in schema["paths"]
    assert manifest["payee"] == server.payment.payee


def test_cors_exposes_canonical_headers(boundary):
    client, business, calls, _, _ = boundary
    response = request(client, OPS[0], headers={"Origin": "https://agent.example"})
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "payment-required" in exposed and "payment-response" in exposed
    preflight = client.options(OPS[0].path, headers={"Origin": "https://agent.example",
        "Access-Control-Request-Method": OPS[0].method, "Access-Control-Request-Headers": "PAYMENT-SIGNATURE"})
    assert preflight.status_code == 200
    business.assert_not_awaited()
    assert calls == []


def test_default_payee_and_override_precedence():
    with patch.dict("os.environ", {}, clear=True):
        assert configured_payee() == DEFAULT_PAYEE
        assert configured_payee("PAYMENT_RECIPIENT") == DEFAULT_PAYEE
    with patch.dict("os.environ", {"PAYEE_ADDRESS": PAYER, "PAYMENT_RECIPIENT": DEFAULT_PAYEE}, clear=True):
        assert configured_payee("PAYMENT_RECIPIENT") == PAYER
    with patch.dict("os.environ", {"PAYMENT_RECIPIENT": PAYER}, clear=True):
        assert configured_payee("PAYMENT_RECIPIENT") == PAYER
    with patch.dict("os.environ", {"PAYEE_ADDRESS": "garbage"}, clear=True):
        with pytest.raises(ValueError):
            configured_payee()


@pytest.mark.parametrize("method,path", [(m, p) for m, p in ROUTES if m != "HEAD"])
def test_compatibility_routes_require_real_verify_and_settle(boundary, method, path):
    client, business, calls, _, _ = boundary
    op = server.payment.paths[path]
    payload = payload_for(client, op, method=method, path=path)
    response = request(client, op, method=method, path=path, headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert response.status_code == 200, response.text
    assert business.await_count > 0
    assert [c[0] for c in calls] == ["verify", "settle"]
    assert "PAYMENT-RESPONSE" in response.headers
    if method != op.method or path != op.path:
        assert calls[0][1]["paymentPayload"]["extensions"] == {}


def test_duplicate_signature_headers_do_not_unlock(boundary):
    client, business, calls, _, _ = boundary
    encoded = encode_header(payload_for(client, OPS[0]))
    response = request(client, OPS[0], headers=[("PAYMENT-SIGNATURE", encoded), ("PAYMENT-SIGNATURE", encoded)])
    assert response.status_code == 400
    business.assert_not_awaited()
    assert not calls


def test_duplicate_json_fields_do_not_unlock(boundary):
    client, business, calls, _, _ = boundary
    payload = json.dumps(payload_for(client, OPS[0]))
    encoded = base64.b64encode(('{"x402Version":2,' + payload[1:]).encode()).decode()
    response = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encoded})
    assert response.status_code == 400
    business.assert_not_awaited()
    assert not calls


def test_oversized_verified_body_is_413_without_business_or_settlement(boundary):
    client, business, calls, _, _ = boundary
    payload = payload_for(client, OPS[0], method="POST")
    response = client.post(OPS[0].path, content=b"x" * 70000,
        headers={"PAYMENT-SIGNATURE": encode_header(payload), "Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "PAYMENT-RESPONSE" not in response.headers
    business.assert_not_awaited()
    assert [c[0] for c in calls] == ["verify"]


def test_facilitator_sidechannel_is_not_logged_or_exposed(boundary, caplog):
    client, _, _, _, state = boundary
    secret = "PRIVATE_FACILITATOR_INTERNAL_SENTINEL"
    state["verify"] = httpx.Response(200, json={"isValid": True, "payer": PAYER},
        headers={"EXTENSION-RESPONSES": encode_header({"internal": secret})})
    with caplog.at_level("INFO"):
        response = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload_for(client, OPS[0]))})
    assert response.status_code == 200
    assert secret not in caplog.text
    assert secret not in response.text
    assert secret not in json.dumps(decode_header(response, "PAYMENT-RESPONSE"))


def test_client_cannot_modify_discovery_metadata(boundary):
    client, _, calls, _, _ = boundary
    payload = payload_for(client, OPS[0])
    payload["resource"].update({"description": "untrusted-client-description",
        "serviceName": "imposter", "tags": ["untrusted"], "iconUrl": "https://untrusted.invalid/icon"})
    payload["extensions"] = {"bazaar": {"info": {"input": {"method": "DELETE"}}}}
    response = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert response.status_code == 200
    for _, body in calls:
        resource = body["paymentPayload"]["resource"]
        assert resource["description"] == OPS[0].description
        assert resource["serviceName"] == server.payment.service
        assert "iconUrl" not in resource and "tags" not in resource
        assert body["paymentPayload"]["extensions"] == OPS[0].extensions()


def test_smart_wallet_signature_length_is_verified_by_facilitator(boundary):
    client, business, calls, _, state = boundary
    payload = payload_for(client, OPS[0])
    payload["payload"]["signature"] = "0x1234"
    state["verify"] = {"isValid": False}
    response = request(client, OPS[0], headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert response.status_code == 402
    assert [c[0] for c in calls] == ["verify"]
    business.assert_not_awaited()


def test_concurrent_nonce_replay_does_not_execute_twice(boundary):
    client, business, calls, _, _ = boundary
    headers = {"PAYMENT-SIGNATURE": encode_header(payload_for(client, OPS[0]))}

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original = server.payment.facilitator.verify

        async def delayed_verify(*args):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=2)
            return await original(*args)

        with patch.object(server.payment.facilitator, "verify", delayed_verify):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://testserver") as ac:
                first = asyncio.create_task(request(ac, OPS[0], headers=headers))
                await asyncio.wait_for(entered.wait(), timeout=2)
                try:
                    second = await request(ac, OPS[0], headers=headers)
                    assert second.status_code == 402
                    assert second.json()["error"]["code"] == "payment_replayed"
                    business.assert_not_awaited()
                finally:
                    release.set()
                assert (await first).status_code == 200
        assert [c[0] for c in calls] == ["verify", "settle"]

    asyncio.run(exercise())
