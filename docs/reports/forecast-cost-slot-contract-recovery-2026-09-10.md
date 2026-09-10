# Forecast cost-slot contract recovery — 2026-09-10

The IBM forecast run was refused before publication because `build_drivers`
correctly emitted `cost_driver_slots`, while the forecast model's closed driver
wire did not admit that field. The live summary was inspected read-only; no
live state or model route was changed.

The driver wire now admits the field only when present and validates it as a
non-empty, unique list drawn from the frozen cost-driver template registry.
Explicit null, empty, scalar, unknown and duplicate slot values are refused.
Legacy drivers without cost metadata keep their original shape.

The generator identity remains `rule:trailing-carry-forward:1`: accepting the
field fixes validation and does not change forecast generation semantics. The
existing forecast invariant contract now binds the cost-slot wire rule and the
frozen cost registry hash. That contract hash is part of the lane ticket and
failure identity, so an old validation refusal receives one bounded retry while
the `model_digest` of an already-successful spec/input pair stays unchanged.

Validation covers the complete local authority path: model specification
validation, immutable specification persistence and reload, input-table
construction, forecast construction, formal forecast authority publication,
and model reload. It also covers unknown and duplicate cost-slot refusal.

Focused validation:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_cost_driver_templates \
  tests.test_model_forecast_driver \
  tests.test_company_model_forecast \
  tests.test_mission_model_forecast_lane

Ran 124 tests in 4.511s — OK
```
