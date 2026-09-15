# BaseYield Oracle (x402)

Real-Time **DeFi Lending & Staking Yield Oracle** for **Base Mainnet (Chain ID 8453)**, payable via **x402 micro-payments** ($0.01 USDC).

## Features
- **Multi-Protocol Aggregation**: Live lending APY and pool yields across Aave v3, Morpho Blue, Moonwell, Aerodrome, and Compound on Base.
- **Yield Comparison**: Compare rates for USDC, WETH, cbBTC, and stablecoins to optimize treasury and agent allocations.
- **Full x402 Protocol Compliance**: Implements HTTP 402 challenge, `Payment-Required` base64 header, discovery manifest `/.well-known/x402`, and OpenAPI 3.1 with `x-payment-info`.

## Endpoints

### Public Endpoints (Free)
- `GET /` — API catalog & status
- `GET /health` — Service health check
- `GET /.well-known/x402` — Standard x402 discovery manifest
- `GET /docs` — Swagger UI API documentation
- `GET /self-test` — Live test of Base yield feeds

### Paid Endpoints ($0.01 USDC via x402)
- `GET /v1/yields?asset=USDC` (or `POST`) — Compare yields for an asset on Base
- `GET /v1/top-pools?min_tvl=1000000` (or `POST`) — Fetch top verified yield pools on Base

## License
MIT
