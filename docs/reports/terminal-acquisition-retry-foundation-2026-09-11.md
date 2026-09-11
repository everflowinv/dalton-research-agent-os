# Terminal acquisition retry foundation — 2026-09-11

Public-web fetch results already distinguish transient failures from terminal
provider or adapter refusals with the closed `error.retryable` boolean.  The
fetch child previously reduced that field to diagnostic prose, and the mission
authority then selected every old `acquisition_failed` row for another paid
attempt.  A repeated HTTP 403 therefore remained eligible forever.

The child summary now carries the boolean unchanged.  Mission settlement stores
it in an additive nullable column, and automatic retry excludes only an exact
stored false value.  True remains retryable.  Null remains unknown and preserves
the existing bounded restart recovery for legacy rows, missing tickets, and
children that did not produce a typed result.  No text is parsed and no old row
is reclassified or rewritten.

The terminal disposition is copied when a document is carried into a new
mission version.  A terminal URL therefore stays held rather than receiving a
new call merely because mission metadata changed.  A newly discovered URL has
a new row and starts with no failure disposition.  Manual governance can still
change the source plan; this patch adds no retry authorization or budget.

Validation used the real coordinator settlement and selection path with fake
local launchers.  It proves terminal failures remain idle across both elapsed
intervals and coordinator restarts, while transient and unknown legacy failures
remain selectable.  The public-web fetch and mission-source-discovery modules
ran 36 tests successfully; the coverage-mission authority module ran 18 tests
successfully.  No external request or live-state mutation was made.
