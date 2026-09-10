# Forecast-model filing proof — 2026-09-10

`ForecastModelAuthority.publish` now stores an append-only filing proof beside
each newly published model when the caller supplies typed statement rows from
the statement authority. The ForecastModel JSON contract and every historical
model byte remain unchanged.

The proof binds the model version and content hash, company, model input hash,
the exact statement line refs and their canonical digest, and the complete
economic-invariant report. Before writing, publication verifies that every
ingest belongs to the model company, every model history accession belongs to
those ingests, and every historical concept/period/value equals an exact filed
row. A mismatch reports company, metric, period, value, and accessions.

`ForecastModelAuthority.filing_proof(model_version_ref)` validates canonical
JSON, its content hash, every indexed identity column, and the bound model. It
then reloads the exact immutable statement lines, checks their digest, and
recomputes the economic-invariant report. Old model versions and callers that
did not supply typed statement rows return no proof and cannot be treated as
reconciled.

Validation:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_company_model_forecast tests.test_model_forecast_driver \
  tests.test_economic_invariants
```

The focused suite passed 148 tests. Coverage includes a real Store statement
ingest through model publication and proof replay, unknown ingest and wrong
accession refusal, wrong historical value refusal with diagnostic context, and
tampered proof hash refusal.
