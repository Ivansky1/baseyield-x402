"""DefiLlama yield aggregation for Base, paid through x402 v2."""

from datetime import datetime, timezone
import logging
import math
import re
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

from x402_payment import PaidOperation, PaymentGate, configured_payee

PAYEE_ADDRESS = configured_payee()
PRICE_USDC = 0.01
PRICE_ATOMIC = "10000"
CHAIN_ID = "eip155:8453"
USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
YIELDS_URL = "https://yields.llama.fi/pools"
logger = logging.getLogger("baseyield")


class YieldInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    asset: str = Field("USDC", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    min_tvl: float = Field(100000, ge=0, le=1e15)


class TopPoolsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_tvl: float = Field(1000000, ge=0, le=1e15)
    limit: int = Field(10, ge=1, le=100)


class Pool(BaseModel):
    project: str
    symbol: str
    pool_id: str
    apy: Optional[float]
    apy_base: Optional[float]
    apy_reward: Optional[float]
    tvl_usd: float
    underlying_tokens: list[str]
    source_timestamp: Optional[str]


class Provenance(BaseModel):
    success: bool
    network: str
    source: str
    source_timestamp: Optional[str]
    fetched_at: str
    timestamp: str


class YieldResponse(Provenance):
    asset: str
    matched_pools_count: int
    best_apy: Optional[float]
    best_protocol: Optional[str]
    pools: list[Pool]


class TopPoolsResponse(Provenance):
    total_qualifying_pools: int
    top_pools: list[Pool]


def response_schema(model):
    """Inline local model refs so this schema also works inside Bazaar metadata."""
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})

    def inline(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(item) for key, item in value.items() if key != "$defs"}
        return [inline(item) for item in value] if isinstance(value, list) else value

    return inline(schema)


app = FastAPI(title="BaseYield Oracle x402", version="1.1.0", redirect_slashes=False,
              description="Filters and ranks DefiLlama yield snapshots for Base pools.")
payment = PaymentGate(service="BaseYield", payee=PAYEE_ADDRESS, amount=PRICE_ATOMIC, operations=[
    PaidOperation(method="GET", path="/v1/yields",
                  description="Returns DefiLlama Base yield pools matching an asset symbol, ranked by reported APY.",
                  input_schema=YieldInput.model_json_schema(),
                  output_schema=response_schema(YieldResponse),
                  example={"asset": "USDC", "min_tvl": 100000}),
    PaidOperation(method="GET", path="/v1/top-pools",
                  description="Returns DefiLlama Base pools with positive reported APY, ranked by TVL.",
                  input_schema=TopPoolsInput.model_json_schema(),
                  output_schema=response_schema(TopPoolsResponse),
                  example={"min_tvl": 1000000, "limit": 10}),
])


class UpstreamUnavailable(Exception):
    """The upstream did not provide a complete usable dataset."""


def upstream_error():
    return JSONResponse(status_code=502, content={"success": False, "error": {
        "code": "upstream_unavailable", "message": "DefiLlama yield data is unavailable."}})


def _number(value, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpstreamUnavailable()
    try:
        if not math.isfinite(value):
            raise UpstreamUnavailable()
    except OverflowError as exc:
        raise UpstreamUnavailable() from exc
    return round(float(value), 2)


def _source_timestamp(value):
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= 100:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return str(value)
    raise UpstreamUnavailable()


def normalize_pool(pool):
    for field in ("project", "symbol", "pool"):
        if not isinstance(pool.get(field), str) or not pool[field]:
            raise UpstreamUnavailable()
    tokens = pool.get("underlyingTokens")
    if tokens is None:
        tokens = []
    if not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens):
        raise UpstreamUnavailable()
    tvl = _number(pool.get("tvlUsd"))
    if tvl < 0:
        raise UpstreamUnavailable()
    return {"project": pool["project"], "symbol": pool["symbol"], "pool_id": pool["pool"],
            "apy": _number(pool.get("apy"), optional=True),
            "apy_base": _number(pool.get("apyBase"), optional=True),
            "apy_reward": _number(pool.get("apyReward"), optional=True),
            "tvl_usd": tvl, "underlying_tokens": tokens,
            "source_timestamp": _source_timestamp(pool.get("timestamp"))}


async def fetch_base_pools() -> dict:
    """Fetch a real upstream snapshot; a failed fetch is never an empty dataset."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            response = await client.get(YIELDS_URL, headers={"User-Agent": "BaseYield-Oracle/1.1"})
        response.raise_for_status()
        payload = response.json()
        if (not isinstance(payload, dict) or not isinstance(payload.get("data"), list)
            or payload.get("status", "success") != "success"):
            raise UpstreamUnavailable()
        pools = []
        for pool in payload["data"]:
            if not isinstance(pool, dict) or not isinstance(pool.get("chain"), str):
                raise UpstreamUnavailable()
            if pool["chain"] == "Base":
                # Validate now so malformed Base rows cannot masquerade as zero matches.
                normalize_pool(pool)
                pools.append(pool)
        return {"pools": pools, "source": "DefiLlama",
                "source_timestamp": _source_timestamp(payload.get("timestamp")),
                "fetched_at": datetime.now(timezone.utc).isoformat()}
    except (httpx.HTTPError, ValueError, TypeError, KeyError, UpstreamUnavailable) as exc:
        logger.warning("upstream_request_failed", extra={"service": "BaseYield", "upstream": "DefiLlama"})
        raise UpstreamUnavailable() from exc


def provenance(snapshot):
    return {"success": True, "network": "Base Mainnet (8453)", "source": snapshot["source"],
            "source_timestamp": snapshot["source_timestamp"], "fetched_at": snapshot["fetched_at"],
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/", summary="API index")
async def root():
    return {"service": "BaseYield Oracle", "version": "1.1.0", "network": "Base Mainnet (8453)",
            "docs": "/docs", "openapi": "/openapi.json", "manifest": "/.well-known/x402",
            "endpoints": {"yields": "/v1/yields", "top_pools": "/v1/top-pools"},
            "price_usd": "$0.01", "payee": PAYEE_ADDRESS, "status": "online", "source": "DefiLlama"}


@app.get("/health")
async def health():
    return {"status": "healthy", "chain_id": 8453, "payee": PAYEE_ADDRESS}


@app.get("/.well-known/x402")
async def manifest(request: Request):
    return payment.manifest(request)


async def yield_data(inputs: YieldInput):
    try:
        snapshot = await fetch_base_pools()
        clean_asset = inputs.asset.upper()
        matched = []
        for raw in snapshot["pools"]:
            # Match symbol components, so ETH does not silently include WETH/stETH.
            if clean_asset not in re.split(r"[-/+]", raw["symbol"].upper()):
                continue
            pool = normalize_pool(raw)
            if pool["tvl_usd"] >= inputs.min_tvl:
                matched.append(pool)
        matched.sort(key=lambda pool: (pool["apy"] is not None, pool["apy"] or 0), reverse=True)
        best = next((pool for pool in matched if pool["apy"] is not None), None)
        return {**provenance(snapshot), "asset": clean_asset, "matched_pools_count": len(matched),
                "best_apy": best["apy"] if best else None, "best_protocol": best["project"] if best else None,
                "pools": matched[:15]}
    except UpstreamUnavailable:
        return upstream_error()


@app.get("/v1/yields", response_model=YieldResponse)
async def get_yields(asset: str = Query("USDC", min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$"),
                     min_tvl: float = Query(100000, ge=0, le=1e15, allow_inf_nan=False)):
    return await yield_data(YieldInput(asset=asset, min_tvl=min_tvl))


@app.post("/v1/yields", include_in_schema=False)
async def post_yields(inputs: YieldInput):
    return await yield_data(inputs)


async def top_pools_data(inputs: TopPoolsInput):
    try:
        snapshot = await fetch_base_pools()
        filtered = []
        for raw in snapshot["pools"]:
            pool = normalize_pool(raw)
            if pool["tvl_usd"] >= inputs.min_tvl and pool["apy"] is not None and pool["apy"] > 0:
                filtered.append(pool)
        filtered.sort(key=lambda pool: pool["tvl_usd"], reverse=True)
        return {**provenance(snapshot), "total_qualifying_pools": len(filtered), "top_pools": filtered[:inputs.limit]}
    except UpstreamUnavailable:
        return upstream_error()


@app.get("/v1/top-pools", response_model=TopPoolsResponse)
async def top_pools(min_tvl: float = Query(1000000, ge=0, le=1e15, allow_inf_nan=False),
                    limit: int = Query(10, ge=1, le=100)):
    return await top_pools_data(TopPoolsInput(min_tvl=min_tvl, limit=limit))


@app.post("/v1/top-pools", include_in_schema=False)
async def post_top_pools(inputs: TopPoolsInput):
    return await top_pools_data(inputs)


@app.get("/self-test", summary="Configuration diagnostics; no live data request")
async def self_test():
    return {"status": "configuration_only", "payee_configured": PAYEE_ADDRESS,
            "facilitator_configured": bool(payment.facilitator.url),
            "live_upstream_tested": False, "source": "DefiLlama"}


payment.install(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8004)
