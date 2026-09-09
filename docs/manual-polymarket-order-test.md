# Manual Polymarket order test

This operator-run script uses `PolymarketAdapter.prepare_hedge_limit()` and
`PolymarketAdapter.post_prepared_hedge()` (the two steps inside `submit_hedge_limit()`) to place
one real BUY limit order for five shares and `PolymarketAdapter.cancel_order()` to cancel a
specified order. It is separate from the strategy engine and does not connect to
IBKR. Do not run it alongside a strategy trading the same account: these manual
orders are recorded in a separate journal, outside the coordinator's ledger.

The script was tested with mocked venue calls only. No real order was submitted
during development. The paper script remains available for local simulation.

## Configuration

Use a separate local `.env.order-test` if the engine should retain its current
configuration. `--env-file` selects it; the script never edits environment files.
Real placement and cancellation require the following settings in the selected file:

```dotenv
RUN_MODE=LIMITED_LIVE
LIVE_TRADING_ENABLED=true
POLYMARKET_ORDER_SUBMISSION_ENABLED=true
SIMULATE_POLYMARKET_FILLS=false
POLYMARKET_POST_ONLY=false
```

The signing key, signer, funder and remaining application settings must also be
present. Authentication uses the existing adapter and credential diagnostic.
The wallet must already be deployed. A successful authentication check does not
prove order acceptance. No approvals or wallet setup transactions are requested.

## Preview

From the `code` directory in PowerShell:

```powershell
$env:PYTHONPATH='src'
.venv/Scripts/python.exe scripts/test_polymarket_order.py --env-file .env.order-test preview --leg INC50PLUS --outcome YES --best-ask --max-price 0.10
```

This example selects the configured September 50bp+ increase YES token. The 0.10
cap is an example, not a recommended price. Choose your own maximum price.
Preview checks the configured market mapping and current order book, and does
not authenticate or submit an order. To choose a fixed limit, replace
`--best-ask` with `--price YOUR_PRICE`.

## Place five shares

Only run this command yourself when you intend to submit a real order:

```powershell
.venv/Scripts/python.exe scripts/test_polymarket_order.py --env-file .env.order-test place --leg INC50PLUS --outcome YES --best-ask --max-price 0.10 --test-id september-test-001 --confirm-real-money
```

Placement keeps the adapter's market mapping, eligibility, price cap, collateral
and authorization checks. The eligibility policy supports Hong Kong (`HK`) and
the Netherlands (`NL`). A successful eligibility request must identify one of
these countries. The venue's `blocked` field is informational: `true`, `false`,
or an absent/invalid flag does not affect submission. Failed requests, invalid
response objects, and missing or unsupported countries still stop placement. The script
checks the current tick size and minimum size.
If the venue requires more than five shares, it stops; it never raises the size.
The price cap is per share and excludes any venue fees. The balance check and
reported maximum order notional use five times the selected limit price.

Polymarket's geographic-restrictions documentation, checked on 2026-09-10, lists
the Netherlands as close-only on the frontend while stating that the API is not
restricted. The operator-selected application policy treats the `blocked` flag as
informational for all supported deployment countries; this is not a guarantee
that the venue will accept an order. Venue rejections remain failures.
Source: [Polymarket geographic restrictions](https://docs.polymarket.com/api-reference/geoblock).

The order is GTC. A limit at the observed best ask may fill immediately; if the
book changes, the order may remain open. The script does not reprice or chase.
The adapter's `immediately_matched_shares` field is intentionally zero for real
orders; verify fills from the venue order/trade history, not that field.

## Cancel a specific order

Use the order ID returned by placement and a new journal ID:

```powershell
.venv/Scripts/python.exe scripts/test_polymarket_order.py --env-file .env.order-test cancel --order-id YOUR_ORDER_ID --test-id september-cancel-001 --confirm-real-money
```

Cancellation is confirmed only if the venue lists the order as canceled. A
non-confirmed cancellation is reported as such and exits with a failure status.
Cancellation cannot reverse fills that already occurred.

## Results and interrupted attempts

Each mutation requires a unique `--test-id`. Its JSON journal is stored under
`runtime/manual-order-tests/`; existing IDs cannot be reused. Journals contain
the selected order details, authentication responses with credentials redacted,
the eligibility country, raw parsed `blocked` value and decision reason,
and the normalized adapter result. Signed order payloads are never logged.
SDK HTTP rejections also include the HTTP status, venue code and message with
credentials/signatures redacted. The failed phase identifies whether preparation
failed before order submission or a submission/cancellation request failed.
Timeouts, server failures and duplicate-order responses keep an unknown outcome.

The script calls placement once and never retries a failed submission. If a
timeout or interrupted process leaves `SUBMISSION_PENDING` or `OUTCOME_UNKNOWN`,
inspect Polymarket's open orders and trade history before using another test ID.
A new test ID is a new order, not an idempotent retry. The coordinator does not
manage these manual orders; a GTC order remains your responsibility until filled
or canceled. Exit 0 means preview succeeded, the venue accepted the order, or
cancellation was confirmed. Other results exit nonzero.
