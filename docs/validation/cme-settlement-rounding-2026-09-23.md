# CME final settlement rounding — 2026-09-23

Updated local code and `ZQ_Polymarket_Sizing_Model.xlsx` to apply
[CME Rule 22103](https://www.cmegroup.com/content/dam/cmegroup/rulebook/CBOT/III/22.pdf).
The modeled calendar-day average EFFR is rounded to 0.001 percentage point
(0.1 basis point), with exact ties toward positive infinity, before subtraction
from 100. Final cash settlement does not use the executable trading tick.

Scenario futures P&L and the workbook's maximum-hike stress loss now use these
rounded prices. Minimum profit and return on capital use the minimum across
the rounded 0/+25/+50 bp scenarios. Quote-implied probabilities retain their
continuous monthly-rate interpretation.

Calendar-weighted hedge quantities remain common to opportunity sizing, fees,
partial fills and persisted execution obligations. CME rounding can therefore
leave a small difference between scenario profits. The opportunity calculation
includes that difference in its minimum-profit gate; it does not assume that
all three profits are identical. Workbook checks now show the residual and an
allowance of one settlement increment per contract plus $0.01 for rounding the
total hedge shares upward. No existing execution obligations were changed.

## October parity example

Inputs: 3.88% EFFR, October 29 effective date (28/3 calendar days), five contracts,
96.100 entry price, PM asks 0.680 and 0.009, fee coefficients 0.05 and exponents 1,
$598.726 initial margin per contract, $18.20 round-trip IBKR commission, no reserve.
Sufficient liquidity at each PM ask is assumed in this comparison.

| Scenario | Final settlement | Futures P&L | Net P&L |
| --- | ---: | ---: | ---: |
| No change | 96.120 | $416.70 | $40.7182751075 |
| +25 bp | 96.096 | −$83.34 | $44.7582751075 |
| +50 bp | 96.072 | −$583.38 | $48.7882751075 |

PM fees are $5.9339748925. Committed capital is $3,345.47775, and minimum
return on capital is 1.2171139116827%. The historical production snapshot in
the workbook is retained and explicitly identified as predating this correction.
Seven broken comparison links were reconnected to the main worksheet.

## Validation

- Backend: **478 passed**, coverage **87.99%** (required 85%). Existing
  Python 3.14 / pytest-asyncio deprecation warnings remain.
- Ruff: passed for `src tests scripts`. Mypy: passed for all 39 source files.
- Regression cases cover CME's 2.5915% → 97.408 example, both sides of a tie,
  signed halfway values, October calendar weighting, scenario P&L, and a
  September case where rounding makes the +25 bp scenario the minimum.
- Workbook versus code: all six displayed settlements, three protected-scenario
  futures/net P&Ls, minimum profit, capital and return agree within 1e-8.
- Native Excel: changing EFFR to 2.5915% gives 97.408; changing it to −0.0005%
  gives 100.000. Checked saved recalculated values in a disposable copy.
- Workbook inputs, two-sheet structure and native drawing are preserved.
  PM Depth is absent. Formula-error and unavailable-result scans pass for
  the complete saved inputs.

The initial validation above preceded publication. The owner subsequently committed
and pushed `2f903038e69a1a07c20a8cf1789b844d21c674c2` and authorized deployment.
That exact release was deployed September 24 at 00:55 Taipei / September 23 at
16:55 UTC. Source hashes, rounding boundaries and all six October settlements passed
again inside the running production container. Health, readiness, authenticated
reconciliation and margin qualification passed; the engine was left disarmed.
The existing ledger, Gateway and trading settings were preserved. See the
[deployment record](../../deploy/CURRENT_DEPLOYMENT_AND_SECURITY.md#september-24-current-deployment)
and [sanitized evidence](production-cme-settlement-2026-09-24.json). Codex made no
commit or push; these deployment-documentation updates remain local.
