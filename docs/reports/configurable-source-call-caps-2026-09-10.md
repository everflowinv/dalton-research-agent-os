# Configurable source-call caps — 2026-09-10

The versioned Guidepoint discovery plan already signs
`budget.max_calls_per_tick`, but validation and runtime both silently capped it
at the code default of three. The plan value may now range up to the existing
plan daily maximum. Runtime still takes the minimum of the governed connector
daily limit, the plan daily limit, remaining calls, and the signed per-tick
limit. Plans that omit customization continue to default to three.

`PYTHONPATH=src python3 -m unittest tests.test_guidepoint_lane` passed 32 tests
in 3.549 seconds. The new test publishes a plan with seven calls per tick and
shows that its remaining daily limit remains twenty.

No connector, network, live configuration, or live authority was called or
changed.
