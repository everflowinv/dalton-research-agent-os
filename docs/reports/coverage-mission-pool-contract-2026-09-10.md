# Coverage mission pool contract — 2026-09-10

Coverage mission versions may now carry an optional signed `budget.pools`
object with exactly `coverage`, `event_response`, `adhoc`, and `maintenance`
USD caps. Each cap must be a finite non-negative JSON number and their sum may
not exceed `max_daily_cost_usd`. The JSON Schema exposes the same closed shape.

Missions without `pools` retain the legacy three-key budget wire unchanged, so
their canonical content hashes do not change. The shared pool authority keeps
its default split for those missions. A mission that publishes explicit caps
flows through `pool_caps`; the event judgement pool reads the signed
`event_response` amount.

Focused verification:

`PYTHONPATH=src python3 -m unittest tests.test_coverage_mission tests.test_budget_pools tests.test_event_judgement`

passed 150 tests in 4.733 seconds, including the string/bool/non-finite pool
boundary cases. No live mission was created or signed.
