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

## Actual-state benchmark

An offline benchmark was run after the R7b deployment completed, using the
current live Core only as a read-only source and the exact R8 candidate source
at commit `86ad4dc7c21b707e077fd1af04aee7ceafdd66cc`. R8 had not been deployed;
the measurement therefore isolates the code-path change against a coherent
snapshot of the then-current R7b authority data.

The source database was opened through `readonly_sqlite.connect_read_only` and
copied with SQLite's Backup API into an owner-only temporary database. Both
paths ran against that same copy. No scheduler or router database was copied,
no model was called, and no live authority was written. The temporary Core copy
and its directory were removed automatically after the comparison; a follow-up
search found no remaining `dalton-dossier-r8-benchmark-*` path.

Across the latest four Dossiers, all 48 reconstructed unit inputs had identical
canonical structures, SHA-256 hashes, and byte lengths:

| Company ref | Units | Canonical bytes | Legacy snapshots | Shared snapshots | Legacy seconds | Shared seconds | Speed-up |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `company:sec-cik:0000051143` | 12 | 140,624 | 37 | 1 | 27.438602 | 2.036094 | 13.476x |
| `company:sec-cik:0001352010` | 12 | 202,923 | 37 | 1 | 32.318545 | 4.777926 | 6.764x |
| `company:sec-cik:0001467373` | 12 | 184,064 | 37 | 1 | 29.384257 | 3.448050 | 8.522x |
| `company:sec-cik:001688568` | 12 | 186,736 | 37 | 1 | 29.417208 | 2.958958 | 9.942x |

The aggregate fell from 148 snapshot reads and 118.558612 seconds to four
snapshot reads and 13.221028 seconds: about 8.97x faster, with an 89% reduction
in elapsed time. These are measurements of reconstruction on this snapshot,
not a claim about end-to-end model latency.

The owner-only JSON receipt is outside the repository at
`dossier-snapshot-performance/results/r8-actual-20260911T0322Z.json` in the
private activation packet. Its SHA-256 is
`cb890ac8d76e8769851704d3ad1b9baab52191692a648afcb1e82de1f218ac52`.
