# Investment Memo model-stage bridge — 2026-09-10

This slice closes the deterministic stage gap before Investment Memo without
adding an authority or a model call.

`model_stage_readiness.py` evaluates the Playbook's three Industry Model and
three Company Model exit questions from their existing immutable products.
Each stored JSON record passes its authority's complete closed-shape and hash
validator before use; a forged or drifted row cannot become stage evidence.
Industry readiness requires source-bound drafted framework blocks, a computed
cross-company comparison with actual cells, and an explicit high-frequency
data gap/source entry. Company readiness requires historical driver coverage
without unavailable results, assumptions with both `because` and refs, and a
fully computed sensitivity projection whose consensus bridge is available and
non-empty. Missing tables, records, or fields fail closed.

`mission_model_stage_lane.py` is an in-process deterministic lane. It considers
only a company whose folded stage state already places it at `industry_model`
or `company_model`; consequently it cannot cross the Deep Insight human gate.
It writes `entered` once and writes `gate_passed` only when every explicit
check passes. An incomplete check remains `waiting`. It deliberately does not
write `gate_failed`: the current stage contract treats a decision as settled
until a human-approved reopen, while ordinary model inputs are expected to
improve automatically. Keeping the stage entered makes a later framework,
forecast, sensitivity, or consensus version recover on the next tick without
weakening the gate or losing the earlier waiting evidence in tick history.

Stage records bind the active mission version but readiness is selected from
the mission's folded cross-version stage state. A mission version roll while a
stage is waiting therefore continues legally under the new active version.
Stable idempotency keys prevent duplicate `entered` rows; after a pass the
existing ladder advances `next_stage`, so the lane becomes idle for that
stage. The lane uses the mission's configured automation principal and refuses
when `stage_record` is absent.

The registry entry has no installer flag or child launcher. Its only
prerequisite is the already-open Core database, and all work is local SQLite
reads plus existing `CoverageMissionAuthority.record_stage` writes. It runs
after framework/model/sensitivity producers and does not alter their schemas,
budgets, or AlphaEngine configuration.

Validation:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_model_stage_bridge tests.test_industry_framework \
  tests.test_industry_framework_lane tests.test_company_model_forecast \
  tests.test_forecast_sensitivity tests.test_mission_stage \
  tests.test_coverage_mission tests.test_lane_registry tests.test_service \
  tests.test_bounded_planner_driver
```

Result: 322 tests passed. Focused cases cover every readiness dimension,
waiting-to-pass recovery across a mission version change, and refusal to act
while the next stage is the human Deep Insight checkpoint. `git diff --check`
also passed.

## Cross-review correction

The original readiness predicates were too weak for the bound playbook. In
particular, a candidate high-frequency source was treated as though it had
already entered the update calendar, and independently selected latest model
and sensitivity versions could be combined despite a stale model binding.

The bridge now reads records through each authority's checked public reader
and requires the current mission, company, model ref, and model hash to agree.
It reports each unmet playbook criterion and remains `entered`/`waiting`.
There is intentionally no positive gate-pass fixture today: the current
IndustryFramework authority does not record per-input as-of dates or an update
calendar binding, while the model/sensitivity authorities do not record a
completed two-year filing reconciliation or peer-relative sensitivity bands.
Inventing proxy fields for those outputs would recreate the false pass. A
later producer/schema change can supply these facts without changing the
recoverable stage behavior.

Cross-review validation:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_model_stage_bridge tests.test_lane_registry tests.test_service
```

Result: 73 tests passed. Regressions cover the formerly accepted framework
without as-of/calendar proof and a sensitivity projection bound to an older
forecast model.
