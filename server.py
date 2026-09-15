"""
BaseYield Oracle — Real-Time DeFi Lending & Staking Yield Oracle for Base Mainnet.

Aggregates real-time lending rates, staking APYs, and liquidity pool yields across
Aave v3, Morpho Blue, Moonwell, Aerodrome, and Compound on Base (Chain ID 8453).
Payable via x402 micro-payments ($0.01 USDC on Base).
"""

import base64
from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field

# Constants & Configuration
PAYEE_ADDRESS = os.getenv("PAYEE_ADDRESS", "0xb5aFc89b57Fa8270bB7261348179D28099BEa2a0")
PRICE_USDC = 0.01
PRICE_ATOMIC = "10000"  # 0.01 USDC (6 decimals = 10,000 atomic units)
CHAIN_ID = "eip155:8453"
USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

app = FastAPI(
    title="BaseYield Oracle x402",
    description="Real-Time DeFi Lending and Staking Yield Oracle for Base Mainnet, payable via x402.",
    version="1.0.0",
    redirect_slashes=False,
    contact={
        "name": "BaseYield Oracle",
        "email": "ivansky.dev@gmail.com",
        "url": "https://github.com/Ivansky1/baseyield-x402",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def make_x402_challenge(resource_url: str, description: str = "BaseYield Oracle API Access") -> Dict[str, Any]:
    """Generate canonical x402 v2 challenge payload passing 100% of discovery checks."""
    return {
        "x402Version": 2,
        "version": 2,
        "resource": {
            "url": resource_url,
            "description": f"{description} (${PRICE_USDC:.2f} USDC)",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": CHAIN_ID,
                "asset": USDC_ASSET,
                "amount": PRICE_ATOMIC,
                "maxAmountRequired": PRICE_ATOMIC,
                "payee": PAYEE_ADDRESS,
                "payTo": PAYEE_ADDRESS,
                "maxTimeoutSeconds": 300,
                "description": f"{description} (${PRICE_USDC:.2f} USDC)",
                "extra": {
                    "name": "USD Coin",
                    "version": "2",
                    "assetTransferMethod": "eip3009",
                },
            }
        ],
        "extensions": {
            "bazaar": {
                "info": {
                    "name": "BaseYield DeFi Oracle",
                    "description": "Real-time lending APY, pool yields, and TVL metrics across Base protocols.",
                    "input": {
                        "type": "object",
                        "properties": {
                            "asset": {
                                "type": "string",
                                "description": "Asset symbol (e.g. USDC, WETH, ETH, CBBTC)",
                                "default": "USDC",
                            },
                            "min_tvl": {
                                "type": "number",
                                "description": "Minimum TVL in USD to filter (default: 500000)",
                                "default": 500000,
                            },
                        },
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "network": {"type": "string"},
                            "asset": {"type": "string"},
                            "best_apy": {"type": "number"},
                            "pools": {"type": "array"},
                            "timestamp": {"type": "string"},
                        },
                    },
                },
                "schema": {
                    "properties": {
                        "input": {
                            "properties": {
                                "queryParams": {
                                    "type": "object",
                                    "properties": {
                                        "asset": {"type": "string", "default": "USDC"},
                                        "min_tvl": {"type": "number", "default": 500000},
                                    },
                                },
                                "body": {
                                    "type": "object",
                                    "properties": {
                                        "asset": {"type": "string", "default": "USDC"},
                                        "min_tvl": {"type": "number", "default": 500000},
                                    },
                                },
                            }
                        },
                        "output": {
                            "properties": {
                                "example": {
                                    "type": "object",
                                    "properties": {
                                        "network": {"type": "string"},
                                        "asset": {"type": "string"},
                                        "best_apy": {"type": "number"},
                                        "protocol": {"type": "string"},
                                        "timestamp": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    }


def build_402_response(resource_url: str, description: str = "BaseYield Oracle API Access") -> JSONResponse:
    challenge = make_x402_challenge(resource_url, description)
    challenge_b64 = base64.b64encode(json.dumps(challenge).encode("utf-8")).decode("utf-8")
    return JSONResponse(
        status_code=402,
        content=challenge,
        headers={
            "Payment-Required": challenge_b64,
            "Access-Control-Expose-Headers": "Payment-Required",
        },
    )


async def fetch_base_pools() -> List[Dict[str, Any]]:
    """Query live DefiLlama yield pool data for Base Mainnet."""
    url = "https://yields.llama.fi/pools"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"User-Agent": "BaseYield-Oracle/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                return [p for p in data.get("data", []) if p.get("chain") == "Base"]
    except Exception:
        pass
    return []


# Public Endpoints
@app.get("/", summary="API Index & Service Info")
async def root():
    return {
        "service": "BaseYield Oracle",
        "version": "1.0.0",
        "network": "Base Mainnet (8453)",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "manifest": "/.well-known/x402",
        "endpoints": {
            "yields": "/v1/yields",
            "top_pools": "/v1/top-pools",
        },
        "price_usd": f"${PRICE_USDC:.2f}",
        "payee": PAYEE_ADDRESS,
        "status": "online",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health", summary="Health Check")
async def health():
    return {
        "status": "healthy",
        "chain_id": 8453,
        "payee": PAYEE_ADDRESS,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/.well-known/x402", summary="x402 Discovery Manifest")
async def x402_manifest(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "version": 1,
        "name": "BaseYield Oracle",
        "description": "Real-Time DeFi Lending & Staking Yield Oracle for Base Mainnet.",
        "network": CHAIN_ID,
        "price": f"${PRICE_USDC:.2f}",
        "currency": "USDC",
        "payee": PAYEE_ADDRESS,
        "ownershipProofs": [PAYEE_ADDRESS],
        "resources": [
            {
                "type": "http",
                "method": "GET",
                "url": f"{base_url}/v1/yields",
                "description": "Compare lending APY and pool yields for a specific asset on Base.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
            {
                "type": "http",
                "method": "POST",
                "url": f"{base_url}/v1/yields",
                "description": "Compare lending APY and pool yields for a specific asset on Base.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
            {
                "type": "http",
                "method": "GET",
                "url": f"{base_url}/v1/top-pools",
                "description": "Fetch top verified DeFi yield pools on Base sorted by TVL and APY.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
            {
                "type": "http",
                "method": "POST",
                "url": f"{base_url}/v1/top-pools",
                "description": "Fetch top verified DeFi yield pools on Base sorted by TVL and APY.",
                "price": f"${PRICE_USDC:.2f}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": CHAIN_ID,
                        "asset": USDC_ASSET,
                        "amount": PRICE_ATOMIC,
                        "payee": PAYEE_ADDRESS,
                        "payTo": PAYEE_ADDRESS,
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
        ],
    }


# Paid Endpoint: /v1/yields
@app.api_route("/v1/yields", methods=["GET", "POST", "HEAD"], summary="Compare Asset Yields on Base (x402 Paid)")
async def get_yields(
    request: Request,
    asset: Optional[str] = "USDC",
    min_tvl: Optional[float] = 100000.0,
    x_payment_response: Optional[str] = Header(None, alias="x-payment-response"),
    payment_response: Optional[str] = Header(None, alias="payment-response"),
):
    if request.method == "HEAD":
        return build_402_response(str(request.url), "BaseYield Asset Comparison")

    if request.method == "POST":
        try:
            body = await request.json()
            asset = body.get("asset", asset)
            min_tvl = float(body.get("min_tvl", min_tvl))
        except Exception:
            pass

    has_payment = bool(x_payment_response or payment_response)
    if not has_payment:
        return build_402_response(str(request.url), f"BaseYield Asset Comparison ({asset})")

    clean_asset = asset.strip().upper()
    all_pools = await fetch_base_pools()

    # Filter by symbol matching asset and TVL
    matched = []
    for p in all_pools:
        sym = (p.get("symbol") or "").upper()
        tvl = float(p.get("tvlUsd") or 0.0)
        if clean_asset in sym and tvl >= min_tvl:
            matched.append({
                "project": p.get("project"),
                "symbol": p.get("symbol"),
                "pool_id": p.get("pool"),
                "apy": round(float(p.get("apy") or 0.0), 2),
                "apy_base": round(float(p.get("apyBase") or 0.0), 2),
                "apy_reward": round(float(p.get("apyReward") or 0.0), 2),
                "tvl_usd": round(tvl, 2),
                "underlying_tokens": p.get("underlyingTokens", []),
            })

    # Sort by APY descending
    matched.sort(key=lambda x: x["apy"], reverse=True)
    best_pool = matched[0] if matched else None

    return {
        "success": True,
        "network": "Base Mainnet (8453)",
        "asset": clean_asset,
        "matched_pools_count": len(matched),
        "best_apy": best_pool["apy"] if best_pool else 0.0,
        "best_protocol": best_pool["project"] if best_pool else None,
        "pools": matched[:15],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Paid Endpoint: /v1/top-pools
@app.api_route("/v1/top-pools", methods=["GET", "POST", "HEAD"], summary="Top DeFi Pools on Base (x402 Paid)")
async def top_pools(
    request: Request,
    min_tvl: Optional[float] = 1000000.0,
    limit: Optional[int] = 10,
    x_payment_response: Optional[str] = Header(None, alias="x-payment-response"),
    payment_response: Optional[str] = Header(None, alias="payment-response"),
):
    if request.method == "HEAD":
        return build_402_response(str(request.url), "BaseYield Top Pools")

    if request.method == "POST":
        try:
            body = await request.json()
            min_tvl = float(body.get("min_tvl", min_tvl))
            limit = int(body.get("limit", limit))
        except Exception:
            pass

    has_payment = bool(x_payment_response or payment_response)
    if not has_payment:
        return build_402_response(str(request.url), f"BaseYield Top Pools (TVL >= ${min_tvl:,.0f})")

    all_pools = await fetch_base_pools()
    filtered = []
    for p in all_pools:
        tvl = float(p.get("tvlUsd") or 0.0)
        apy = float(p.get("apy") or 0.0)
        if tvl >= min_tvl and apy > 0.0:
            filtered.append({
                "project": p.get("project"),
                "symbol": p.get("symbol"),
                "pool_id": p.get("pool"),
                "apy": round(apy, 2),
                "tvl_usd": round(tvl, 2),
            })

    # Sort by TVL descending
    filtered.sort(key=lambda x: x["tvl_usd"], reverse=True)

    return {
        "success": True,
        "network": "Base Mainnet (8453)",
        "total_qualifying_pools": len(filtered),
        "top_pools": filtered[:limit],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Self-test endpoint
@app.get("/self-test", summary="Test yield data connectivity")
async def self_test():
    pools = await fetch_base_pools()
    return {
        "status": "ok",
        "base_pools_detected": len(pools),
        "payee_configured": PAYEE_ADDRESS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="BaseYield Oracle x402",
        version="1.0.0",
        description="Real-Time DeFi Lending & Staking Yield Oracle for Base Mainnet (Chain ID 8453), payable via x402.",
        routes=app.routes,
    )
    openapi_schema["x-payment-info"] = {
        "protocols": [
            {
                "x402": {
                    "version": 2,
                    "network": CHAIN_ID,
                    "asset": USDC_ASSET,
                    "payee": PAYEE_ADDRESS,
                    "price_usd": PRICE_USDC,
                }
            }
        ]
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8004, reload=True)
