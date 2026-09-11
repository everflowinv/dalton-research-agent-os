# ACN annual cash export proof — 2026-09-10

## Live read-only acceptance

ACN's governed 10-K settled and produced a new specification. On the next
normal model cadence, the lane published
`forecast-model-version:ff98fb9b6accc2576b2bfe8fa89a38d5:2`, schema 0.2,
with the current specification and input hashes. Its filing proof replays with
economic-invariant status `available`. Operating cash flow and capital
expenditure each bind 13 exact cash-statement quarters, and all eight forecast
free-cash-flow cells are computed from those drivers.

The check was read-only and dispatched no model or source work.

## Export defect and repair

The normal export initially refused ACN with `annual filing authority hash is
invalid`. The filing authority hashes the original closed statement-line wire,
including a `statement_lines_hash`; the export reader omitted that field when
replaying the filing hash. The immutable line rows contain enough data to
rebuild both accepted statement wire versions: the older shape without
`dimension_count` and the newer shape with it. The repair reconstructs both
closed shapes and requires exactly one to reproduce the stored filing hash.
Changed filing metadata, line count, or line bytes still fail closed.

The workbook translator also lacked three exact formulas already present in
the closed forecast-model contract: operating cash flow and capital expenditure
as revenue shares, and free cash flow as their difference. Those exact formula
strings are now supported without inspecting display labels or inferring
free-text arithmetic.

## Workbook verification

The repaired exporter produced an ACN workbook with a filing-bound August
fiscal calendar, quarterly and annual columns, OCF, CapEx, and FCF formulas.
LibreOffice recalculated it offline using an isolated profile. All 88 supported
quarterly computed cells matched the immutable model values within the
exporter's numeric tolerance, and no formula error value was present. Complete
fiscal years aggregate only four proven duration quarters. Partial boundary
years and historical FCF without four complete component quarters remain
blank with explicit gaps.

Focused verification passed 124 tests in 9.096 seconds:

```text
PYTHONPATH=src python -m unittest tests.test_fund_xlsx_export \
  tests.test_cashflow_input_foundation tests.test_model_forecast_driver \
  tests.test_company_model_inputs tests.test_company_model_forecast
```
