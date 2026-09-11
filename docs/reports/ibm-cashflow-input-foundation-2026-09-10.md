# IBM cash-flow input foundation — 2026-09-10

## Result

The model input builder now reads operating cash flow and capital expenditure
from a closed concept registry on the cash-flow statement. It no longer
requires those facts to be disguised as expense lines. Cash inputs are usable
only when they are dimension-free, use one unit, have an unambiguous concept,
and form at least four consecutive quarters. Capital expenditure uses the
positive-outflow convention required by the existing free-cash-flow equation.

Forecast model schema 0.2 adds the `cash_flow` driver kind and retains exact
filing forms and cumulative operands. A derived quarter records both filed
periods, values, units, accessions, and forms. Formal publication rereads those
exact rows from statement authority and rejects a changed statement, form,
period, value, unit, accession, dimension, or derived arithmetic. Legacy 0.1
models retain their old closed shape and remain readable without rewriting
their bytes or hashes.

## IBM evidence

A read-only copy of the deployed authority contained 16 un-dimensioned USD
cash-statement rows for each supported IBM concept:

- `us-gaap:NetCashProvidedByUsedInOperatingActivities`
- `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`

The current copy correctly remains `incomplete_quarter_series`: it has 10-Q
cumulative facts but lacks the annual rows needed to derive Q4 for 2023, 2024,
and 2025. Evidence is retained at
`/tmp/dalton-ibm-cashflow-input-audit/result.json`. The implementation does not
invent those quarters or silently extrapolate free cash flow. Once an approved
10-K is recorded, the same deterministic normalizer derives Q4 as FY minus 9M
and binds both operands.

## Verification

The authority round-trip fixture records three cumulative 10-Q filings and one
10-K in a real `DaltonStore`, builds model inputs, publishes a formal forecast,
rereads it, and verifies computed free cash flow. It also rejects forged forms,
operand periods, statements, missing operands, malformed operands, wrong
differences, ambiguous concepts, mixed units, dimensions, negative capex, and
incomplete quarter sequences.

Command:

```text
PYTHONPATH=src /Users/everflow/Projects/dalton-research-agent-os/.venv/bin/python -m unittest tests.test_cashflow_input_foundation tests.test_company_model_inputs tests.test_model_forecast_driver tests.test_company_model_series tests.test_company_model_forecast tests.test_mission_model_forecast_lane tests.test_forecast_sensitivity tests.test_fund_xlsx_export
```

Result: 214 tests passed in 8.760 seconds. One pre-existing test emitted an
unclosed SQLite `ResourceWarning`; it did not fail the run.


Root integration additionally checks a model whose two operands are real
immutable database rows and whose arithmetic is exact, but which falsely
labels FY minus Q1 as Q4. Publication rejects it specifically because the
period chain does not replay. The integrated input, series, driver, mission
forecast and XLSX regression passes 164 tests in 9.306 seconds. An earlier
command used the nonexistent test_company_model_forecast_lane name and was
corrected to test_mission_model_forecast_lane; it was a test command error,
not a product failure. Independent review also replayed one actual live
schema-0.1 model through old and new validators: all 183,423 canonical bytes
remain identical (SHA-256
681b88a7ba59c1b151ba2e24452d93db25b259b786537620b4411759d0a52f47).
