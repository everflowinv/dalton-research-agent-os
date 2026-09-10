# Cockpit worker policy read lock fix — 2026-09-10

Production DebateMap and ZeroBase children failed together while constructing
their per-call scheduler. `Scheduler._ensure_policy()` always entered a
`BEGIN IMMEDIATE` transaction, even when the requested immutable policy already
existed with the same semantic hash. The transaction then committed a read-only
check and could lose to another child's reserved write lock.

The scheduler now performs the existing-policy lookup before opening a write
transaction. A matching hash returns immediately. A different hash retains the
existing immutable-version conflict. A missing policy still enters the original
transaction, rechecks after acquiring the lock, and inserts once; the existing
five-second SQLite busy timeout remains in force for that necessary write.
Fresh database schema/bootstrap behavior is unchanged.

The concurrency regression creates the policy, holds `BEGIN IMMEDIATE` from a
separate connection, and reopens a Scheduler with that exact policy. The reopen
succeeds while the writer lock remains held and the database still contains one
policy row. The immutable-policy conflict regression also remains green.

```text
PYTHONPATH=src python3 -m unittest tests.test_scheduler \
  tests.test_cockpit_model_fallback
Ran 29 tests in 0.600s — OK
```

No live scheduler, worker, broker, or model was used.
