# Truthful source-lane exhaustion

Market price, consensus, and catalyst-calendar coordinators previously returned
their successful resting sentences after iterating the mission universe even
when every company had been skipped by a durable failure decision. In the live
shape, five `not_permitted` rows therefore appeared beside “every company is
current” or “every diary has been read”.

The shared `exhausted_by_failures` projection now returns `held` with bounded
counts when the completed scan contains `held`, `parked`, `terminal`, or
`not_permitted` entries. It leaves each lane's existing `skipped` records,
failure details, permission controls, scheduling, source calls, and budgets
unchanged. If all skips are genuine resting conditions such as `current`,
`recently_refreshed`, or `asked_today`, each lane retains its prior `idle`
status and successful sentence.

Validation:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_lane_exhaustion \
  tests.test_mission_market_price_lane \
  tests.test_mission_consensus_lane \
  tests.test_mission_catalyst_lane \
  tests.test_lane_failure_classes \
  tests.test_cockpit_ops_panels
```

177 tests passed. The three real coordinators now cover an all-permission-held
universe; existing tests retain the true-current and successful cadence cases.
No live state, permission, source, or budget was changed.
