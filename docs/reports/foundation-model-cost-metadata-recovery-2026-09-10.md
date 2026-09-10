# Company-model cost metadata recovery — 2026-09-10

Cost-bound company specifications already included a validated
`cost_driver_template` in their immutable wire and content hash. The
CoverageMission authority persisted only fixed legacy fields, however, and
dropped that metadata. Reading the same specification back therefore made
`build_model_inputs` refuse every cost-bound company. The forecast selector
caught those refusals and returned an empty pending list, which the lane
misreported as every company having a current model.

The authority table now has nullable `metadata_json`. New specifications store
the exact validated cost-template binding there and merge only that closed key
on read. Existing rows migrate with NULL, retaining their bytes, identifiers,
and content hashes; missing historical metadata is not inferred. The model-spec
task contract moved to `task:company-model-spec:0.3` and binds the new authority
projection, so unchanged company state can receive a legitimate new immutable
specification rather than colliding with the old incomplete task answer.

When no model can proceed because its specification cannot build inputs,
`pending_companies` now raises a company-labelled blocked prerequisite instead
of returning an empty list. The lane therefore reports `unavailable` with the
specific company and cost-metadata reason rather than claiming all models are
current. Valid companies are still allowed to proceed before a blocked one is
reported on a later tick.

Validation:

```text
PYTHONPATH=src:. python3 -m unittest \
  tests.test_company_model_spec tests.test_cost_driver_templates \
  tests.test_company_model_forecast tests.test_mission_model_forecast_lane -q
```

All 97 tests passed. The authority round-trip test builds real model inputs and
a forecast driver from the restored template metadata. Migration, immutable
legacy identity, same-state/new-task versioning, and explicit blocked-lane
reporting are covered. No live database or model call was used.
