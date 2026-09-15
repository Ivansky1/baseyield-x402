import base64
import json
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

import server
from x402_payment import encode_header

REAL_ASYNC_CLIENT = httpx.AsyncClient


def signed_headers(client, path, method="GET"):
    """Dummy authorization used only with mocked facilitator methods."""
    challenge = json.loads(base64.b64decode(client.request(method, path).headers["PAYMENT-REQUIRED"]))
    payload = {"x402Version": 2, "resource": challenge["resource"], "accepted": challenge["accepts"][0],
               "payload": {"signature": "0x" + "11" * 65, "authorization": {
                   "from": "0x" + "1" * 40, "to": server.PAYEE_ADDRESS, "value": server.PRICE_ATOMIC,
                   "validAfter": str(int(time.time()) - 10), "validBefore": str(int(time.time()) + 55),
                   "nonce": "0x" + uuid.uuid4().hex + uuid.uuid4().hex}}}
    return {"PAYMENT-SIGNATURE": encode_header(payload)}


def pool(**kwargs):
    return {"chain": "Base", "project": "test-protocol", "symbol": "USDC", "pool": "pool-1",
            "tvlUsd": 2000000, "apy": 4.1, "apyBase": 4.1, "apyReward": None, **kwargs}


def snapshot(pools):
    return {"pools": pools, "source": "DefiLlama", "source_timestamp": None,
            "fetched_at": "2026-09-16T00:00:00+00:00"}


class TestPublicAndPayment(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_public_endpoints(self):
        for path in ("/", "/health", "/.well-known/x402", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/").json()["payee"], server.PAYEE_ADDRESS)

    def test_only_two_canonical_discovery_resources(self):
        resources = self.client.get("/.well-known/x402").json()["resources"]
        self.assertEqual(len(resources), 2)
        self.assertEqual({item["method"] for item in resources}, {"GET"})
        paths = self.client.get("/openapi.json").json()["paths"]
        for path in ("/v1/yields", "/v1/top-pools"):
            self.assertEqual(set(paths[path]), {"get"})
            self.assertIn("402", paths[path]["get"]["responses"])
            self.assertIn("parameters", paths[path]["get"])

    def test_unpaid_challenge(self):
        for path in ("/v1/yields", "/v1/top-pools"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 402)
            challenge = json.loads(base64.b64decode(response.headers["PAYMENT-REQUIRED"]))
            self.assertEqual(challenge["x402Version"], 2)
            expected = challenge["accepts"][0]
            self.assertEqual(expected["network"], server.CHAIN_ID)
            self.assertEqual(expected["asset"], server.USDC_ASSET)
            self.assertEqual(expected["amount"], "10000")
            self.assertEqual(expected["payTo"], server.PAYEE_ADDRESS)

    def test_fake_header_does_not_unlock_resource(self):
        """Keep arbitrary payment-looking headers from ever restoring the old bypass."""
        with patch("server.fetch_base_pools", new_callable=AsyncMock) as business:
            for header in ("Payment-Response", "x-payment-response", "Authorization", "Payment-Receipt", "X-PAYMENT"):
                for path in ("/v1/yields", "/v1/top-pools"):
                    for method in ("GET", "POST", "HEAD"):
                        with self.subTest(header=header, path=path, method=method):
                            response = self.client.request(method, path, headers={header: "hello"}, json={} if method == "POST" else None)
                            self.assertEqual(response.status_code, 402)
                            self.assertNotIn("PAYMENT-RESPONSE", response.headers)
            business.assert_not_awaited()

    def test_malformed_payment_signature_never_calls_business(self):
        with patch("server.fetch_base_pools", new_callable=AsyncMock) as business:
            for value in ("hello", "e30=", "bnVsbA=="):
                response = self.client.get("/v1/yields", headers={"PAYMENT-SIGNATURE": value})
                self.assertEqual(response.status_code, 400)
            business.assert_not_awaited()

    def test_self_test_does_not_expose_paid_data(self):
        with patch("server.fetch_base_pools", new_callable=AsyncMock) as business:
            response = self.client.get("/self-test")
            self.assertEqual(response.json()["status"], "configuration_only")
            self.assertFalse(response.json()["live_upstream_tested"])
            business.assert_not_awaited()

    def test_verified_payment_with_failed_upstream_never_settles(self):
        with patch.object(server.payment.facilitator, "verify", AsyncMock(return_value={"isValid": True})) as verify, \
             patch.object(server.payment.facilitator, "settle", new_callable=AsyncMock) as settle, \
             patch("server.fetch_base_pools", AsyncMock(side_effect=server.UpstreamUnavailable())) as business:
            for path in ("/v1/yields", "/v1/top-pools"):
                for method in ("GET", "POST"):
                    with self.subTest(path=path, method=method):
                        headers = signed_headers(self.client, path, method)
                        response = self.client.request(method, path, headers=headers, json={} if method == "POST" else None)
                        self.assertEqual(response.status_code, 502)
                        self.assertFalse(response.json()["success"])
                        self.assertEqual(response.json()["error"]["code"], "upstream_unavailable")
                        self.assertNotIn("PAYMENT-RESPONSE", response.headers)
            self.assertEqual(verify.await_count, 4)
            self.assertEqual(business.await_count, 4)
            settle.assert_not_awaited()

    def test_invalid_paid_input_is_sanitized_422_without_upstream_or_settlement(self):
        with patch.object(server.payment.facilitator, "verify", AsyncMock(return_value={"isValid": True})), \
             patch.object(server.payment.facilitator, "settle", new_callable=AsyncMock) as settle, \
             patch("server.fetch_base_pools", new_callable=AsyncMock) as business:
            for body in ('{"min_tvl":NaN}', '{"min_tvl":Infinity}', '{"min_tvl":-1}', '{"asset":""}', '{broken'):
                with self.subTest(body=body):
                    headers = signed_headers(self.client, "/v1/yields", "POST")
                    response = self.client.post("/v1/yields", headers={**headers, "Content-Type": "application/json"}, content=body)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["error"]["code"], "invalid_request")
                    self.assertNotIn("PAYMENT-RESPONSE", response.headers)
            path = "/v1/yields?min_tvl=NaN"
            response = self.client.get(path, headers=signed_headers(self.client, path))
            self.assertEqual(response.status_code, 422)
            business.assert_not_awaited()
            settle.assert_not_awaited()

    def test_bounded_finite_inputs(self):
        for value in (-1, float("nan"), float("inf"), 1e16):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                server.YieldInput(min_tvl=value)
        for value in (0, 101, -1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                server.TopPoolsInput(limit=value)
        for asset in ("", "X" * 33, " ", "https://example.com"):
            with self.subTest(asset=asset), self.assertRaises(ValidationError):
                server.YieldInput(asset=asset)


class TestYieldData(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, payload, status=200):
        client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)))
        with patch("server.httpx.AsyncClient", return_value=client):
            return await server.fetch_base_pools()

    async def test_upstream_snapshot_provenance_and_base_filter(self):
        data = await self.fetch({"status": "success", "timestamp": "2026-09-15T20:00:00Z",
                                 "data": [pool(), pool(chain="Ethereum")]})
        self.assertEqual(len(data["pools"]), 1)
        self.assertEqual(data["source"], "DefiLlama")
        self.assertEqual(data["source_timestamp"], "2026-09-15T20:00:00Z")
        self.assertIn("fetched_at", data)

    async def test_successful_empty_data_is_not_upstream_failure(self):
        data = await self.fetch({"data": []})
        self.assertEqual(data["pools"], [])
        with patch("server.fetch_base_pools", AsyncMock(return_value=data)):
            result = await server.yield_data(server.YieldInput())
        self.assertTrue(result["success"])
        self.assertEqual(result["matched_pools_count"], 0)
        self.assertIsNone(result["best_apy"])

    async def test_upstream_failure_is_explicit_not_empty_success(self):
        for payload, status in (({}, 200), ({"data": None}, 200), ({"data": []}, 503),
                                ({"status": "error", "data": []}, 200), ({"data": ["bad"]}, 200)):
            with self.subTest(payload=payload, status=status), self.assertRaises(server.UpstreamUnavailable):
                await self.fetch(payload, status)
        with patch("server.fetch_base_pools", AsyncMock(side_effect=server.UpstreamUnavailable())):
            response = await server.yield_data(server.YieldInput())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["error"]["code"], "upstream_unavailable")
        self.assertFalse(json.loads(response.body)["success"])

    async def test_malformed_upstream_json_is_failure(self):
        client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json")))
        with patch("server.httpx.AsyncClient", return_value=client), self.assertRaises(server.UpstreamUnavailable):
            await server.fetch_base_pools()

    async def test_timeout_is_failure(self):
        def handler(request):
            raise httpx.ReadTimeout("secret", request=request)

        client = REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        with patch("server.httpx.AsyncClient", return_value=client), self.assertRaises(server.UpstreamUnavailable):
            await server.fetch_base_pools()

    async def test_null_apy_components_stay_null(self):
        with patch("server.fetch_base_pools", AsyncMock(return_value=snapshot([pool(apyBase=None, apyReward=None)]))):
            result = await server.yield_data(server.YieldInput())
        self.assertIsNone(result["pools"][0]["apy_base"])
        self.assertIsNone(result["pools"][0]["apy_reward"])
        self.assertEqual(result["best_apy"], 4.1)

    async def test_missing_apy_is_not_fabricated_as_zero(self):
        with patch("server.fetch_base_pools", AsyncMock(return_value=snapshot([pool(apy=None)]))):
            result = await server.yield_data(server.YieldInput())
        self.assertIsNone(result["pools"][0]["apy"])
        self.assertIsNone(result["best_apy"])

    async def test_nonfinite_or_missing_tvl_fails(self):
        for value in (None, float("inf"), float("nan"), -1, "unknown", 10 ** 400):
            with self.subTest(value=value), self.assertRaises(server.UpstreamUnavailable):
                server.normalize_pool(pool(tvlUsd=value))

    async def test_malformed_underlying_tokens_are_not_replaced_with_empty_list(self):
        for value in (False, 0, {}, ""):
            with self.subTest(value=value), self.assertRaises(server.UpstreamUnavailable):
                server.normalize_pool(pool(underlyingTokens=value))

    async def test_symbol_matching_does_not_conflate_eth_and_weth(self):
        with patch("server.fetch_base_pools", AsyncMock(return_value=snapshot([pool(symbol="WETH"), pool(symbol="ETH-USDC")]))):
            result = await server.yield_data(server.YieldInput(asset="ETH"))
        self.assertEqual(result["matched_pools_count"], 1)
        self.assertEqual(result["pools"][0]["symbol"], "ETH-USDC")

    async def test_top_pools_sort_tvl_and_respect_limit(self):
        data = snapshot([pool(pool="low", tvlUsd=1000000), pool(pool="high", tvlUsd=3000000), pool(apy=None)])
        with patch("server.fetch_base_pools", AsyncMock(return_value=data)):
            result = await server.top_pools_data(server.TopPoolsInput(limit=1))
        self.assertEqual(result["total_qualifying_pools"], 2)
        self.assertEqual(result["top_pools"][0]["pool_id"], "high")
        self.assertEqual(result["source"], "DefiLlama")


if __name__ == "__main__":
    unittest.main()
