# Forecast integration fixes F1–F3

Date: 2026-09-10  
Branch: `resume-forecast-fixes`  
Baseline: `694471c`

## Result

- F1: event judgement accepts an optional `outside_band_reason`, validates it as a substantive reason, and converts it to the forecast assumption's `outside_band.reason`. The prompt tells the judging model when the field is required. Earnings calibration only actualizes filed values, so it has no outside-band judgement to carry. Thesis revision candidates preserve the already validated `forecast_change` object and need no additional conversion.
- F2: `run_company_forecast` now supplies all stored normalized statement rows, including dimension-derived breakdown rows, to `ForecastModelAuthority.publish`. A segment sum mismatch therefore refuses the forecast before either the model version or forecast lines are published.
- F3: the cockpit renders valuation refusals beside the valuation position, forecast and sensitivity refusals in the model card, and all model-page refusals above the missing model table values. Each block includes the output label and complete reasons/findings supplied by the wire.

## Verification

- Focused: `PYTHONPATH=$PWD/src python3 -m unittest tests.test_economic_invariants tests.test_event_judgement tests.test_company_model_forecast tests.test_cockpit_wave1`
- Result: 222 tests passed in 18.504 seconds. Python emitted one existing unclosed SQLite `ResourceWarning`; it did not fail the run.
- Full: `PYTHONPATH=$PWD/src python3 -m unittest discover -s tests -t .`
- Result: running at the time of the implementation commit; complete output is retained in `full-test-resume-forecast.log` and this report will be updated when it finishes.

## Limits

The cockpit HTML test verifies refusal placement and field consumption statically. The existing cockpit-plane tests exercise the live wire shape and complete refusal findings. No browser screenshot test exists for this server-rendered page.
