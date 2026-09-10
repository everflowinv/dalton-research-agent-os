# Controlled redrive WAL recovery — 2026-09-10

## Result

Controlled-redrive `apply` now opens the exact existing Scheduler and budget
authorities with SQLite `mode=rw` and holds those connections across strict
read-only revalidation and both append-only writes. This permits SQLite to
provision and retain transient WAL/SHM sidecars during the explicitly writable
operator action.

`prepare` remains strictly read-only and continues to refuse a WAL database
whose sidecars are absent. `apply` validates the reviewed candidate hash and
content hash before any writable open. Its writable opener rejects in-memory,
missing, unreadable, and non-SQLite targets and never permits SQLite to create
an authority database.

## Regression evidence

Command:

```text
PYTHONPATH=src:. python3 -m unittest tests.test_controlled_failure_redrive -v
```

Result: 12 tests passed in 0.216 seconds.

The new cases close both authority owners so SQLite removes their sidecars,
confirm strict `prepare` refuses, and prove `apply` completes once and replays
as a duplicate. They also prove a bad candidate hash creates no sidecars and a
missing budget path remains absent.

No live authority, service, broker, or model call was used.
