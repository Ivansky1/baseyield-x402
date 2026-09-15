# BaseYield Oracle (x402 v2)

Filters and ranks **DefiLlama yield snapshots for Base Mainnet pools**. Each paid request costs **0.01 USDC** (`10000` atomic units).

## What the data means

- Data comes from `https://yields.llama.fi/pools`. This service filters the Base rows and ranks reported APY/TVL; it does not independently calculate protocol returns or verify pool safety.
- Every result includes `source: "DefiLlama"`, `fetched_at`, and `source_timestamp`. Source timestamps remain `null` when the upstream provides none; fetching a snapshot does not establish when the underlying values were measured. Pool timestamps are retained when supplied.
- Missing APY values/components remain `null`, not fabricated zero returns. Zero matching pools is a valid successful snapshot; no matching APY yields `best_apy: null`.
- Upstream timeouts, HTTP errors, malformed JSON and invalid Base rows return HTTP 502 with `success: false` and `error.code: "upstream_unavailable"`. A failed data request is never reported as an empty list of opportunities.
- Asset matching compares symbol components (`USDC` matches `USDC-WETH`; `ETH` does not silently match `WETH` or `stETH`). Symbols are labels, not verified token identity. Rates and pool coverage depend on the upstream snapshot and may change; APY is not a guarantee or recommendation.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/yields` | Match an asset, rank by APY; `asset=USDC`, `min_tvl=100000`; returns up to 15 pools |
| GET | `/v1/top-pools` | Positive-APY pools ranked by TVL; `min_tvl=1000000`, `limit=10` |

`min_tvl` must be finite and between 0 and 1e15; `limit` is 1–100; `asset` is 1–32 alphanumeric characters plus `.`, `_` or `-`. Hidden POST compatibility routes accept the same parameters in a JSON object and use the same payment gate. HEAD receives a challenge without fetching data. Only the two canonical GET operations are advertised.

Public routes: `/`, `/health`, `/docs`, `/openapi.json`, `/.well-known/x402`. `/self-test` reports configuration only (`live_upstream_tested: false`); it does not expose paid pool counts or claim live connectivity.

## Payment configuration

| Setting | Value |
|---|---|
| Protocol | x402 v2, `exact`, EIP-3009 |
| Network | `eip155:8453` (Base Mainnet) |
| Asset | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Default payee | `0xb5aFc89b57Fa8270bB7261348179D28099BEa2a0` |
| Price | `10000` atomic units = 0.01 USDC |

Environment variables:

- `PAYEE_ADDRESS`: override the payee consistently in runtime, challenges and discovery.
- `X402_FACILITATOR_URL`: **required for paid operation**; configure a trusted HTTPS facilitator that supports x402 v2 Base Mainnet USDC. No mainnet facilitator is silently chosen, and missing configuration never permits unverified payments.
- `X402_PUBLIC_BASE_URL`: optional fixed public deployment URL for consistent resource URLs behind proxies.
- `X402_FACILITATOR_BEARER_TOKEN`: optional operator-provisioned token for facilitators accepting bearer authentication. Automatic CDP JWT generation/refresh is not implemented.

The upstream data URL is fixed in code; public request parameters cannot select arbitrary upstream URLs.

## x402 request flow

1. An unpaid request receives HTTP 402 with the canonical challenge as base64 JSON in **`PAYMENT-REQUIRED`** and a machine-readable error body.
2. An x402 v2 client signs an EIP-3009 authorization for the offered network, asset, exact amount and payee, then retries with base64 JSON in **`PAYMENT-SIGNATURE`**.
3. The local payment module validates the payload and expected requirements, including resource metadata, and calls the configured facilitator `/verify`. Only successful verification permits the DefiLlama request.
4. A successful business response stays buffered until `/settle` succeeds. Only then is the paid JSON released with **`PAYMENT-RESPONSE`**.

`Authorization`, `Payment-Receipt`, `Payment-Response`, `x-payment-response` and `X-PAYMENT` are not payment proof. No legacy header compatibility is enabled. Business errors are not settled; verification/settlement failures never produce a paid success. Both canonical response headers are exposed through CORS.

Unpaid example:

```bash
curl -i 'http://localhost:8004/v1/yields?asset=USDC'
```

Use the [official Python client examples](https://github.com/x402-foundation/x402/tree/main/examples/python/clients) to sign the returned challenge. An arbitrary header value or a receipt from a prior settlement cannot replace the signed authorization.

### Protocol and operational limits

- EIP-3009 signs transfer authorization fields, **not the resource URL**. The server checks submitted resource metadata and requirements, but does not claim cryptographic URI binding. On-chain nonce consumption prevents successful reuse of a settled authorization, and settlement must succeed before paid results are released.
- Concurrent workers can perform duplicate data requests before one settlement wins. No persistent result cache/recovery service is added. A lost connection after settlement may mean payment occurred without delivery; check the transaction before retrying with a new authorization.
- Facilitator trust, chain support, deployment origin/proxy configuration and upstream availability remain deployment responsibilities. Tests mock external services and spend no USDC; live facilitator/on-chain readiness is not asserted.
- `/.well-known/x402` is a service discovery manifest. Canonical challenges include the official Bazaar extension, and OpenAPI publishes per-operation payment details. External crawler listing is not guaranteed.

References: [x402 v2 specification](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md), [HTTP transport](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/http.md), [Bazaar extension](https://github.com/x402-foundation/x402/blob/main/specs/extensions/bazaar.md), [DefiLlama yield methodology/schema](https://github.com/DefiLlama/yield-server/blob/master/README.md).

## Run and test

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
python -m uvicorn server:app --host 0.0.0.0 --port 8004
python -m pytest -q
python -m compileall -q .
```

Tests include `test_fake_header_does_not_unlock_resource` for all canonical/compatibility methods, mocked verification/settlement, malformed payment payloads, empty data versus upstream failure, provenance, null APY handling, symbol matching and bounded inputs. No funded wallet is required. Existing `vercel.json` remains the deployment entry point.

## License

MIT

## Deployment verification notes

The payment dependency is pinned to official `x402==2.23.0`. Tests use a mocked
facilitator: they prove the local payment boundary, not live settlement. Configure
`X402_FACILITATOR_URL` to a trusted HTTPS facilitator that supports exact payments
on `eip155:8453`. No mainnet facilitator is assumed. If the provider requires
short-lived CDP JWTs, supply a maintained authentication adapter/token provisioning;
this service does not generate CDP JWTs automatically.

`X402_PUBLIC_BASE_URL` should be the deployed HTTPS origin. A settlement timeout
can be ambiguous after broadcast: reconcile the authorization/chain before paying
again. The local nonce guard is bounded and process-local; on-chain authorization
consumption and facilitator verification remain necessary across replicas/restarts.
No distributed rate limiter or durable payment/recovery database is introduced.
Request bodies are limited to 64 KiB, buffered responses to 2 MiB. HEAD is discovery
only (402); it never verifies, executes a paid operation, or settles.

CI runs the entire pytest suite on Python 3.12, including
`test_fake_header_does_not_unlock_resource`, on pushes and pull requests. This
workflow must be enabled and configured as a required check in repository hosting
to block merges; merely adding the workflow does not change branch protections.
