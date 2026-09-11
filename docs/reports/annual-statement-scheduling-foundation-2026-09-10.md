# Annual statement scheduling foundation — 2026-09-10

## Production defect

The live statement authority contained eight 10-Q filings for each mission
company and no 10-K filings. Every recorded statement dispatch was also a
10-Q. The connector, child CLI, immutable dispatch contract, and statement
authority already admit both forms, but the controller always constructed its
coordinator with the `10-Q` default. The separate SEC filings discovery plan
can discover a 10-K document; it does not publish statement rows, so this gap
could not repair itself.

## Change

The installed lane now schedules `10-K` and `10-Q`, in that order, while
retaining one child at a time. Coverage, failed-attempt budgets, and
configuration holds are evaluated independently for each company and form.
An annual failure therefore cannot consume or hold the quarterly path.

The selected forms and desired filing counts are explicit CLI configuration:
`--statement-lane-form` is repeatable and
`--statement-lane-filing-limit FORM=N` sets an exact target for that form.
The installed default requests one 10-K and retains the existing
specification-derived 10-Q depth. Existing single-form coordinator callers and
old dispatch/ticket identities remain valid.

No source governance, public connector behavior, mission budget, or existing
statement/model record is changed.

## Verification

Focused tests cover annual-first backfill, transition to quarterly ingestion,
form-scoped company and configuration failures, configurable targets, legacy
single-form behavior, immutable statement dispatches, and installed contract
selection. The final focused command and result are recorded with the commit.
