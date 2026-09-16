"""Local x402 v2 exact/USDC gate using the official SDK wire models and client.

Verification delegates cryptography, balance and authorization-state checks to a
trusted facilitator. EIP-3009 signs the transfer, NOT the resource URL. Buffering
prevents releasing data until settlement; the token's nonce prevents re-spend.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from fastapi.openapi.utils import get_openapi
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements, ResourceInfo
from x402.schemas import SettleResponse, VerifyResponse

DEFAULT_PAYEE = "0xb5aFc89b57Fa8270bB7261348179D28099BEa2a0"
NETWORK = "eip155:8453"
USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
HEX32 = re.compile(r"0x[0-9a-fA-F]{64}\Z")
UINT = re.compile(r"(?:0|[1-9][0-9]{0,77})\Z")
LOGGER = logging.getLogger("x402.payment")


def configured_payee(legacy_env=None):
    value = os.getenv("PAYEE_ADDRESS")
    if value is None and legacy_env:
        value = os.getenv(legacy_env)
    value = DEFAULT_PAYEE if value is None else value
    if not ADDRESS.fullmatch(value) or int(value, 16) == 0:
        raise ValueError("PAYEE_ADDRESS must be a nonzero EVM address")
    return value


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON number")


def _loads(value):
    return json.loads(value, object_pairs_hook=_json_pairs, parse_constant=_reject_constant)


def encode_header(value):
    return base64.b64encode(json.dumps(value, separators=(",", ":"), allow_nan=False).encode()).decode()


def _wire(model):
    return model.model_dump(by_alias=True, exclude_none=True)


@dataclass(frozen=True)
class PaidOperation:
    method: str
    path: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object"})
    output_schema: dict = field(default_factory=lambda: {"type": "object"})
    example: dict = field(default_factory=dict)

    def extensions(self):
        """Bazaar info/schema convention from the official v2 extension spec."""
        key = "queryParams" if self.method == "GET" else "body"
        info = {"type": "http", "method": self.method, key: self.example}
        properties = {
            "type": {"const": "http"}, "method": {"const": self.method},
            key: self.input_schema,
        }
        required = ["type", "method", key]
        if key == "body":
            info["bodyType"] = "json"
            properties["bodyType"] = {"const": "json"}
            required.append("bodyType")
        return {"bazaar": {
            "info": {"input": info, "output": {"type": "json"}},
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object", "required": ["input"],
                "properties": {
                    "input": {"type": "object", "properties": properties,
                              "required": required, "additionalProperties": False},
                    "output": {"type": "object", "properties": {
                        "type": {"const": "json"}, "example": self.output_schema}},
                },
            },
        }}


class PaymentFailure(Exception):
    def __init__(self, code, message, status=402, *, challenge=True, details=None):
        self.code, self.message, self.status = code, message, status
        self.challenge = challenge
        self.details = details or {}
        super().__init__(code)


@dataclass
class PaymentReservation:
    expires_at: int
    outcome: PaymentFailure | None = None


def uncertain_settlement(transaction=None):
    details = {"network": NETWORK, "retry_new_payment": False,
               "action": "reconcile_before_new_payment"}
    if transaction is not None:
        details["transaction"] = transaction
    return PaymentFailure(
        "payment_settlement_pending" if transaction else "payment_settlement_unknown",
        "Settlement is not confirmed. Reconcile the authorization before making another payment.",
        503, challenge=False, details=details)


class Facilitator:
    """Official SDK client with strict responses and bounded, nonredirecting HTTP."""
    def __init__(self, url=None, transport=None):
        self.url = os.getenv("X402_FACILITATOR_URL", "") if url is None else url
        self.url = self.url.rstrip("/")
        self.transport = transport  # httpx.MockTransport in tests only
        self.bearer_token = os.getenv("X402_FACILITATOR_BEARER_TOKEN", "")
        if "\r" in self.bearer_token or "\n" in self.bearer_token:
            raise ValueError("Invalid facilitator bearer token")
        if self.url:
            parsed = urlsplit(self.url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("X402_FACILITATOR_URL must be an HTTPS URL without credentials")

    async def _call(self, operation, payload, requirements):
        if not self.url:
            raise PaymentFailure("payment_configuration_missing", "Payment service is not configured.", 503)

        async def validate_response(response):
            # The SDK logs this opaque facilitator sidechannel at INFO. It is
            # unused here and must never leak facilitator internals into logs.
            response.headers.pop("EXTENSION-RESPONSES", None)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 65536:
                    raise ValueError("Facilitator response too large")
            # SDK reads response.json() after this hook.
            response._content = bytes(content)
            data = _loads(content)
            key = "isValid" if operation == "verify" else "success"
            if not isinstance(data, dict) or type(data.get(key)) is not bool:
                raise ValueError("Invalid facilitator response")

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False,
                trust_env=False, transport=self.transport,
                headers={"Authorization": "Bearer " + self.bearer_token} if self.bearer_token else {},
                event_hooks={"response": [validate_response]},
            ) as client:
                sdk = HTTPFacilitatorClient(FacilitatorConfig(url=self.url, http_client=client))
                result = await getattr(sdk, operation)(payload, requirements)
                return _wire(result)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, RecursionError):
            # Neither upstream response bodies nor signed authorizations are logged.
            if operation == "settle":
                raise uncertain_settlement() from None
            raise PaymentFailure("payment_service_unavailable", "Payment service is unavailable.", 503) from None

    async def verify(self, payload, requirements):
        return await self._call("verify", payload, requirements)

    async def settle(self, payload, requirements):
        return await self._call("settle", payload, requirements)


class PaymentGate:
    def __init__(self, *, service, payee, amount, operations, aliases=None):
        if not ADDRESS.fullmatch(payee) or int(payee, 16) == 0:
            raise ValueError("Invalid payment recipient")
        if not isinstance(amount, str) or not UINT.fullmatch(amount) or int(amount) <= 0:
            raise ValueError("Payment amount must be positive atomic units")
        self.service, self.payee, self.amount = service, payee, amount
        self.operations = list(operations)
        self.paths = {op.path: op for op in self.operations}
        self.aliases = aliases or {}
        if len(self.paths) != len(self.operations):
            raise ValueError("Duplicate paid operation")
        for alias, canonical in self.aliases.items():
            self.paths[alias] = self.paths[canonical]
        self.public_base_url = os.getenv("X402_PUBLIC_BASE_URL", "").rstrip("/")
        if self.public_base_url:
            p = urlsplit(self.public_base_url)
            if (p.scheme != "https" or not p.hostname or p.username or p.password
                    or p.query or p.fragment or p.path):
                raise ValueError("X402_PUBLIC_BASE_URL must be an HTTPS origin")
        self.facilitator = Facilitator()
        self.requirements = PaymentRequirements(
            scheme="exact", network=NETWORK, asset=USDC_ASSET, amount=amount,
            pay_to=payee, max_timeout_seconds=60,
            extra={"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"},
        )
        self._seen = {}
        self._lock = threading.Lock()

    def resource_url(self, request, operation, *, include_query=True):
        base = self.public_base_url or str(request.base_url).rstrip("/")
        query = request.url.query if include_query else ""
        return base + operation.path + ("?" + query if query else "")

    def build_payment_required(self, request, operation):
        canonical = request.url.path == operation.path and request.method == operation.method
        return _wire(PaymentRequired(
            x402_version=2, resource=ResourceInfo(
                url=self.resource_url(request, operation), description=operation.description,
                mime_type="application/json", service_name=self.service),
            accepts=[self.requirements], extensions=operation.extensions() if canonical else {},
        ))

    def error_response(self, request, operation, failure):
        headers = {"Cache-Control": "no-store"}
        if failure.challenge:
            headers["PAYMENT-REQUIRED"] = encode_header(self.build_payment_required(request, operation))
        return JSONResponse(
            {"success": False, "error": {"code": failure.code, "message": failure.message,
                                          **failure.details}},
            status_code=failure.status,
            headers=headers,
        )

    def parse_payment(self, request, operation):
        values = request.headers.getlist("payment-signature")
        if not values:
            raise PaymentFailure("payment_required", "A verified x402 v2 payment is required.")
        try:
            if len(values) != 1 or len(values[0]) > 16384:
                raise ValueError("Invalid payment header")
            raw = _loads(base64.b64decode(values[0], validate=True))
            if not isinstance(raw, dict) or type(raw.get("x402Version")) is not int or raw["x402Version"] != 2:
                raise ValueError("Unsupported version")
            payload = PaymentPayload.model_validate(raw, strict=True)
            if not payload.resource or payload.resource.url != self.resource_url(request, operation):
                raise ValueError("Resource mismatch")
            expected, accepted = _wire(self.requirements), _wire(payload.accepted)
            for key in ("asset", "payTo"):
                if not ADDRESS.fullmatch(accepted[key]):
                    raise ValueError("Invalid address")
                accepted[key], expected[key] = accepted[key].lower(), expected[key].lower()
            if accepted != expected:
                raise ValueError("Requirements mismatch")
            auth = payload.payload["authorization"]
            signature = payload.payload["signature"]
            if (not isinstance(signature, str) or len(signature) > 8194
                    or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){0,4096}", signature)):
                raise ValueError("Invalid signature encoding")
            if not isinstance(auth, dict):
                raise ValueError("Invalid authorization")
            for key in ("from", "to"):
                if not isinstance(auth.get(key), str) or not ADDRESS.fullmatch(auth[key]) or int(auth[key], 16) == 0:
                    raise ValueError("Invalid address")
            for key in ("value", "validAfter", "validBefore"):
                if not isinstance(auth.get(key), str) or not UINT.fullmatch(auth[key]) or int(auth[key]) >= 2**256:
                    raise ValueError("Invalid authorization integer")
            now = int(time.time())
            if (auth["to"].lower() != self.payee.lower() or auth["value"] != self.amount
                    or int(auth["validAfter"]) >= now or int(auth["validBefore"]) <= now
                    or int(auth["validBefore"]) > now + self.requirements.max_timeout_seconds + 5
                    or not isinstance(auth.get("nonce"), str) or not HEX32.fullmatch(auth["nonce"])):
                raise ValueError("Authorization mismatch")
            # Forward authoritative requirements and discovery, never client-modified metadata.
            payload.accepted = self.requirements.model_copy(deep=True)
            payload.resource = ResourceInfo(
                url=self.resource_url(request, operation), description=operation.description,
                mime_type="application/json", service_name=self.service)
            payload.extensions = (operation.extensions() if request.url.path == operation.path
                                  and request.method == operation.method else {})
            return payload
        except (ValueError, TypeError, KeyError, binascii.Error, RecursionError):
            raise PaymentFailure("invalid_payment", "Payment payload is invalid for this request.", 400) from None

    def reserve(self, payload):
        auth = payload.payload["authorization"]
        key = (auth["from"].lower(), auth["nonce"].lower())
        with self._lock:
            now = time.time()
            self._seen = {k: reservation for k, reservation in self._seen.items()
                          if reservation.expires_at > now}
            if key in self._seen:
                if self._seen[key].outcome is not None:
                    raise self._seen[key].outcome
                raise PaymentFailure("payment_replayed",
                    "Payment authorization has already been submitted. Reconcile before paying again.",
                    409, challenge=False, details={"retry_new_payment": False})
            if len(self._seen) >= 10000:
                raise PaymentFailure("payment_capacity_exceeded", "Payment service is temporarily busy.", 503)
            self._seen[key] = PaymentReservation(int(auth["validBefore"]) + 5)
        return key

    def release(self, key):
        with self._lock:
            self._seen.pop(key, None)

    def remember_outcome(self, key, failure):
        with self._lock:
            if key in self._seen:
                self._seen[key].outcome = failure

    async def require_verified_payment(self, payload):
        data = await self.facilitator.verify(payload, self.requirements)
        try:
            result = VerifyResponse.model_validate(data, strict=True)
        except (ValueError, TypeError):
            raise PaymentFailure("payment_service_unavailable", "Invalid payment service response.", 503) from None
        if not result.is_valid or result.invalid_reason:
            raise PaymentFailure("payment_verification_failed", "Payment could not be verified.")
        if result.payer and result.payer.lower() != payload.payload["authorization"]["from"].lower():
            raise PaymentFailure("payment_verification_failed", "Payment could not be verified.")

    async def settle_payment(self, payload):
        data = await self.facilitator.settle(payload, self.requirements)
        try:
            result = SettleResponse.model_validate(data, strict=True)
        except (ValueError, TypeError):
            raise uncertain_settlement() from None
        if result.error_reason == "settlement_pending":
            if (not result.success and result.network == NETWORK and HEX32.fullmatch(result.transaction)
                    and (not result.payer or result.payer.lower() == payload.payload["authorization"]["from"].lower())
                    and (result.amount is None or result.amount == self.amount)):
                raise uncertain_settlement(result.transaction)
            raise uncertain_settlement()
        if not result.success or result.error_reason:
            if result.transaction or result.success or result.network != NETWORK:
                raise uncertain_settlement()
            raise PaymentFailure("payment_settlement_failed", "Payment settlement could not be confirmed.")
        if (result.network != NETWORK or not HEX32.fullmatch(result.transaction)
                or (result.payer and result.payer.lower() != payload.payload["authorization"]["from"].lower())
                or (result.amount is not None and result.amount != self.amount)):
            raise uncertain_settlement()
        # Only the settlement receipt's public fields belong in the buyer response.
        receipt = {"success": True, "transaction": result.transaction, "network": result.network}
        if result.payer:
            receipt["payer"] = result.payer
        if result.amount is not None:
            receipt["amount"] = result.amount
        return receipt

    @staticmethod
    def attach_payment_response(start, settlement):
        headers = [(k, v) for k, v in start.get("headers", [])
                   if k.lower() not in (b"payment-response", b"cache-control")]
        headers.extend([(b"payment-response", encode_header(settlement).encode()),
                        (b"cache-control", b"no-store")])
        start["headers"] = headers

    def manifest(self, request):
        resources = []
        for op in self.operations:
            resources.append({
                "resource": self.resource_url(request, op, include_query=False),
                "type": "http", "method": op.method, "path": op.path,
                "x402Version": 2, "description": op.description,
                "accepts": [_wire(self.requirements)], "extensions": op.extensions(),
                "inputSchema": op.input_schema, "outputSchema": op.output_schema,
            })
        return {"service": self.service, "version": "2.0.0", "x402Version": 2,
                "description": "; ".join(op.description for op in self.operations),
                "network": NETWORK, "payee": self.payee, "asset": USDC_ASSET,
                "price_usdc": str(Decimal(self.amount) / Decimal(1000000)),
                "provenance": {"type": "self_declared", "ownership_verified": False},
                "resources": resources}

    def install(self, app):
        async def invalid_request(request, exc):
            return JSONResponse({"success": False, "error": {
                "code": "invalid_request", "message": "Request parameters are invalid."}}, status_code=422)
        app.add_exception_handler(RequestValidationError, invalid_request)
        app.add_middleware(PaymentMiddleware, gate=self)
        # Outermost CORS includes challenges and failures from the payment middleware.
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
                           allow_headers=["Content-Type", "PAYMENT-SIGNATURE"],
                           expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE"])

        def openapi():
            if app.openapi_schema:
                return app.openapi_schema
            schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            for op in self.operations:
                operation = schema["paths"][op.path][op.method.lower()]
                # x-payment-info is a service OpenAPI extension, NOT a core x402 standard.
                operation["x-payment-info"] = {"protocol": "x402", "x402Version": 2,
                    "accepts": [_wire(self.requirements)], "extensions": op.extensions()}
                operation["responses"]["402"] = {"description": "Payment required or payment failed",
                    "headers": {"PAYMENT-REQUIRED": {"schema": {"type": "string"},
                        "description": "Base64 encoded x402 v2 PaymentRequired"}}}
                operation["responses"]["200"] = {"description": op.description,
                    "headers": {"PAYMENT-RESPONSE": {"schema": {"type": "string"},
                        "description": "Base64 settlement receipt, successful settlement only"}},
                    "content": {"application/json": {"schema": copy.deepcopy(op.output_schema)}}}
                if op.method == "POST":
                    operation["requestBody"] = {"required": bool(op.input_schema.get("required")),
                        "content": {"application/json": {"schema": copy.deepcopy(op.input_schema)}}}
                else:
                    operation["parameters"] = [{"name": key, "in": "query",
                        "required": key in op.input_schema.get("required", []), "schema": value}
                        for key, value in op.input_schema.get("properties", {}).items()]
            app.openapi_schema = schema
            return schema
        app.openapi = openapi


class PaymentMiddleware:
    def __init__(self, app, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") not in self.gate.paths:
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        op = self.gate.paths[scope["path"].rstrip("/")]
        gate = self.gate
        started, state, key, settling = time.monotonic(), "required", None, False
        try:
            if request.method == "OPTIONS":
                return await JSONResponse({}, status_code=204)(scope, receive, send)
            if request.method == "HEAD":
                raise PaymentFailure("payment_required", "Use the canonical method with an x402 payment.")
            if request.method not in ("GET", "POST"):
                raise PaymentFailure("method_not_allowed", "Use the canonical resource method.", 405)
            payload = gate.parse_payment(request, op)
            key = gate.reserve(payload)
            await gate.require_verified_payment(payload)
            state = "verified"
            messages, size = [], 0
            # Read within a bounded budget BEFORE framework parsing, so the
            # framework cannot swallow the limit exception into an opaque 400.
            body = bytearray()
            deadline = time.monotonic() + 10
            while True:
                try:
                    message = await asyncio.wait_for(receive(), max(0.001, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    raise PaymentFailure("request_timeout", "Request body timed out.", 408) from None
                if message["type"] == "http.disconnect":
                    raise PaymentFailure("request_disconnected", "Request was interrupted.", 400)
                body.extend(message.get("body", b""))
                if len(body) > 65536:
                    raise PaymentFailure("request_too_large", "Request body exceeds 64 KiB.", 413)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            async def buffered_send(message):
                nonlocal size
                size += len(message.get("body", b""))
                if size > 2 * 1024 * 1024:
                    raise PaymentFailure("resource_response_too_large", "Resource response exceeds the limit.", 500)
                messages.append(message.copy())

            await self.app(scope, bounded_receive, buffered_send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            if 200 <= start["status"] < 300:
                settling = True
                settlement = await gate.settle_payment(payload)
                gate.attach_payment_response(start, settlement)
                state = "settled"
            else:
                state = "resource_failed"
            for message in messages:
                await send(message)
        except PaymentFailure as failure:
            state = failure.code
            if key is not None and settling:
                gate.remember_outcome(key, failure)
            await gate.error_response(request, op, failure)(scope, receive, send)
        except Exception:
            failure = uncertain_settlement() if settling else PaymentFailure(
                "resource_unavailable", "Resource is temporarily unavailable.", 500)
            state = failure.code
            if key is not None and settling:
                gate.remember_outcome(key, failure)
            await gate.error_response(request, op, failure)(scope, receive, send)
        finally:
            if key is not None and not settling:
                gate.release(key)
            LOGGER.info(json.dumps({"service": gate.service, "endpoint": op.path,
                "payment_state": state, "facilitator_status": state,
                "latency_ms": round((time.monotonic() - started) * 1000)}))
