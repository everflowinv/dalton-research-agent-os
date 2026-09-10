# Earnings-season scheduling fairness — 2026-09-10

The earnings coordinator previously remembered one in-process `_last_batch`
selected from the globally most urgent occurrence. A refused or partial batch
could suppress that occurrence while the selector kept returning it, leaving
later occurrences and companies behind the same gate. A restart discarded the
memory and could pay for the same failed batch again.

The coordinator now schedules one exact company, occurrence, and window. Its
durable failure key binds the active mission, verifier contracts, and a
company-scoped fingerprint built from the same authorities used by the paid
context: the occurrence, current forecast, theses, recent Claims, guidance
profile and consensus; previews add open debates, while calibrations add model
inputs, reconciliation rows, market events, and governed source keys. The child
receives explicit `--company-ref`, `--occurrence-ref`, and `--window` filters.

Held occurrences are skipped while later eligible occurrences remain runnable.
Input changes move only that occurrence's key. Content refusals retain bounded
durable holds across restart; dependency reasons continue through the shared
failure classifier and probe policy. Waiting and ungranted windows do not spend
or enter the failure budget. Existing per-call, run, event-pool, producer,
verifier, and human-governance behavior remains in the child.

Validation:

```text
PYTHONPATH=src:. python3 -m unittest \
  tests.test_mission_earnings_season_lane tests.test_earnings_season_cli \
  tests.test_earnings_season -q
```

All 114 tests passed. The new cases cover first-occurrence refusal followed by a
different company, restart persistence without another dispatch, affected-only
input and model-configuration release, more than eight held windows without
hiding the ninth, one-read orphan handling, and exact launcher targeting. No
live state, network, broker, or paid model call was used.
