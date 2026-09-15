import base64
import json
import unittest
from fastapi.testclient import TestClient
from server import app, PAYEE_ADDRESS, CHAIN_ID, PRICE_ATOMIC, USDC_ASSET

class TestBaseYieldServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root_endpoint(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["service"], "BaseYield Oracle")
        self.assertEqual(data["network"], "Base Mainnet (8453)")
        self.assertEqual(data["payee"], PAYEE_ADDRESS)

    def test_health_endpoint(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "healthy")

    def test_manifest_endpoint(self):
        resp = self.client.get("/.well-known/x402")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["currency"], "USDC")
        self.assertEqual(data["payee"], PAYEE_ADDRESS)
        self.assertGreater(len(data["resources"]), 0)

    def test_yields_requires_402_without_payment(self):
        resp = self.client.get("/v1/yields?asset=USDC")
        self.assertEqual(resp.status_code, 402)
        self.assertIn("Payment-Required", resp.headers)
        
        b64_header = resp.headers["Payment-Required"]
        header_json = json.loads(base64.b64decode(b64_header).decode("utf-8"))
        self.assertEqual(header_json["x402Version"], 2)
        self.assertEqual(header_json["accepts"][0]["payTo"], PAYEE_ADDRESS)
        self.assertEqual(header_json["accepts"][0]["amount"], PRICE_ATOMIC)
        self.assertEqual(header_json["accepts"][0]["maxTimeoutSeconds"], 300)

    def test_top_pools_requires_402_without_payment(self):
        resp = self.client.get("/v1/top-pools?min_tvl=1000000")
        self.assertEqual(resp.status_code, 402)
        self.assertIn("Payment-Required", resp.headers)
        
        data = resp.json()
        self.assertEqual(data["x402Version"], 2)
        self.assertEqual(data["accepts"][0]["payTo"], PAYEE_ADDRESS)

    def test_openapi_schema_contains_x_payment_info(self):
        resp = self.client.get("/openapi.json")
        self.assertEqual(resp.status_code, 200)
        schema = resp.json()
        self.assertIn("x-payment-info", schema)
        self.assertEqual(schema["x-payment-info"]["protocols"][0]["x402"]["version"], 2)

if __name__ == "__main__":
    unittest.main()
