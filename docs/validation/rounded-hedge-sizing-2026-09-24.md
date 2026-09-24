# Rounded-settlement hedge sizing — local validation, September 24, 2026

Status: local changes only. Not committed, pushed, or deployed. The earlier CME
settlement-rounding deployment remains the running server version.

## Behavior

New orders derive each binary hedge ratio from the difference between the
CME-rounded no-change and corresponding hike settlements, multiplied by the
$4,167 ZQ point value. Batch share quantities round upward to 0.01 shares after
multiplication by the contract count. Settlement rounding itself is unchanged.

The repository saves full-precision ratios and `CME_ROUNDED_SETTLEMENT_V1` with
the order intent before submission. Fill obligations and residual order checks
use the saved ratios; subsequent EFFR refreshes cannot resize an existing order.
Each fill receives the difference between the new and previous cumulative
rounded share totals, preventing extra rounding across split fills or restarts.
Malformed saved ratios fail closed.

Pre-upgrade orders and historical inventory records without a settlement basis
retain their original calendar-weighted sizing. New inventory evidence can
include `hedge_pre_meeting_effr_percent` to validate the rounded-settlement hedge.
There is no retroactive rebalancing of holdings or modification of past fills.

## Excel parity

`ZQ Hedge Model!B25:B26` hold the exact per-contract ratios, and `B38:B39` round
the selected batch quantities once. `J53:J55`, costs and return on capital update
through their existing formulas. The reconciliation tolerance now covers only
0.01-share rounding. Existing user inputs and the editable margin are preserved.

For October 2026, EFFR 3.88%, five contracts entered at 96.10, PM asks 0.54/0.009,
fee coefficient 0.05 with exponent 1, initial margin $598.726 per contract and
round-trip IBKR commission $18.20:

| Quantity | Result |
| --- | ---: |
| Rounded settlements: no change / +25 / +50 | 96.120 / 96.096 / 96.072 |
| Exact per-contract ratios | 100.008 / 200.016 |
| Batch shares | 500.04 / 1,000.08 |
| PM premium | $279.02232 |
| PM transaction cost | $6.656482476 |
| Net P&L in each modeled outcome | $112.821197524 |
| Committed capital | $3,272.65232 |
| Return on capital | 3.4473933218% |

The Excel and Python results agree across 20 combinations of rate bases and
contract counts, including rates around a CME rounding tie. Native Excel
recalculation and saving also preserve these results. Other batch sizes can
leave differences below $0.01 because binary shares trade at 0.01 precision.
Actual settlement still depends on realized daily EFFR; the 50+ binary does not
fully hedge hikes larger than the modeled +50 bp scenario.

## Preview rectangle

Deleting the DrawingML shape alone allowed Excel to recreate it from an obsolete
VML master shape associated with the two comments. The workbook now keeps those
comments as direct VML note rectangles without that master. Native Excel
open/calculate/save verification retains both comment texts and creates no
DrawingML shapes. PM Depth remains removed.

## Checks

Validation covers payoff/fee sizing, missing EFFR, residual quantities, saved
ratio integrity, duplicate executions, split fills across database restarts,
legacy orders and inventory compatibility. Ruff and strict mypy pass.
The final full suite passes all 491 tests, with 88.02% coverage against the
repository's 85% requirement.
