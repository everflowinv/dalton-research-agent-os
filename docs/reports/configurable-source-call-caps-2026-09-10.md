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

AlphaEngine bounded probes now have a separate optional signed mission field,
`max_alphaengine_probe_calls_24h`. Omission preserves the prior 30-call
default. The effective probe cap is the smaller of that value and the
mission's total AlphaEngine cap, so the narrower lane can never enlarge the
source-wide allowance.

New inquiry admissions carry the exact mission version ref/hash into their
loop and probe WorkOrder. The writer resolves that immutable mission and
passes the effective cap to the executor; callers cannot supply a cap over
RPC. Legacy work recovers its exact mission through the hash-bound loop and
the stored mission research plan rather than guessing the active mission.

`PYTHONPATH=src python3 -m unittest tests.test_coverage_mission tests.test_bounded_alphaengine_probe tests.test_research_task tests.test_bounded_planner_loop`
passed 77 tests in 2.698 seconds. Tests cover signed publication, total-cap
validation, low and high executor limits, admission-to-WorkOrder propagation,
writer hash refusal, and legacy exact-plan recovery.
