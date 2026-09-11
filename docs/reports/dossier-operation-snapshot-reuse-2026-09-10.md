# Dossier operation-scoped Claim snapshot reuse

## Finding

`plan_units` rebuilt the same company's ClaimIndex snapshot for every section,
classification input, market-view input, and guidance input. A single dossier
operation therefore repeated the most expensive immutable authority projection
many times even though every query belonged to one planning decision.

## Change

`prepare_company_claim_query` now captures one exact ClaimIndex projection for
one company and one SQLite connection. Dossier planning, reconstruction,
freshness evaluation, source fingerprinting, and the selected company's
guidance build reuse that context only for the duration of their operation.
There is no process-global cache. A context is rejected if passed to another
company or connection, so it cannot cross an authority or freshness boundary.

Query filtering, canonical ordering, retirement filtering, quotas, and output
records are unchanged. The compatibility test reconstructs all 12 unit inputs
through both the former repeated-read path and the shared path and requires
exact Python structure equality; legacy records without unit provenance are
covered separately.

## Verification

The focused suite passed 138 tests:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_dossier_snapshot_performance \
  tests.test_company_research_view \
  tests.test_dossier_lane \
  tests.test_dossier_unit_provenance \
  tests.test_company_dossier
```

The regression observes more than 12 Claim snapshot reads on the compatibility
path and exactly one on the operation-scoped path. It also covers empty input,
snapshot failure before any partial plan is returned, and cross-company and
cross-connection context rejection.
