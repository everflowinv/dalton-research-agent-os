# Foundation model integrity review — 2026-09-10

## Scope and result

This review used the live Core database read-only and retained only structural refusal metadata. It did not copy research prose or filed values into the repository.

The four published forecast attempts blocked by `segment_sum` exposed a code defect in the invariant projection. `segment_groups` treated every XBRL dimension as an additive operating segment. In the representative ACN run, every refusal came from `StatementEquityComponentsAxis`, `StatementClassOfStockAxis`, or `ConsolidationItemsAxis`. Those axes describe a roll-forward, an attribute, or alternate consolidation views; their members do not form an additive partition of the consolidated fact. Several sums were exact multiples of the consolidated amount, consistent with a multi-dimensional context having been projected into the ledger's single axis/member shape.

The repair checks only duration facts on known business-segment, geography, product/service, or generic segment axes. It also requires one unique row per member. A repeated member means the one-axis projection lost another dimension, so the group is unavailable rather than passed or failed. A genuine unique-member business segment mismatch is still refused with the existing tolerance and reason. No fiscal-period, unit, source, or numeric rule changed.

A read-only replay over the current statement structures produced:

- ACN: 71 additive groups checked, all pass. The prior false refusals were exclusively non-additive axes.
- CTSH: 32 additive groups checked, all pass after excluding attribute/consolidation axes.
- DXC: 11 additive groups checked, all pass after excluding consolidation/equity axes.
- EPAM: 28 additive groups check successfully. Eight prior residual findings were geographic cash balance instants representing only selected jurisdictions, not a complete duration-flow partition; instant facts are now outside this invariant.

## IBM

IBM is a different case and this patch does not make it pass. The current filed ledger contains nondimensional quarterly `us-gaap:Revenues` history, but the current model specification binds no revenue driver to that concept. Its drivers describe software adoption, pricing/mix, consulting conversion, infrastructure, financing, acquisitions, and currency; most correctly have no filed GAAP counterpart. The forecast builder therefore truthfully reports `no revenue driver rests on a filed concept with quarterly history`.

This is a specification/pipeline mismatch, not missing source data and not a reason to weaken `revenue_anchor`. The model-spec prompt says a revenue driver should be what moves revenue and explicitly says not to answer “revenue”, while the downstream deterministic forecast requires one filed revenue concept as its calculation anchor. Editing live facts or guessing an anchor in the forecast layer would erase that distinction.

The existing governed model-spec lane can produce a new specification when its bound company-state hash changes, such as after a newly filed statement or a governed industry-classification update. On that next run, the producer should bind the available filed revenue concept as the calculation anchor while preserving the economic drivers. For the unchanged current state, the append-only table's `(company_ref, state_hash)` uniqueness and `choose_company` deliberately suppress another paid judgement. A durable correction therefore needs an explicit versioned model-spec supersession/review path, or a versioned spec contract with a separate filed `revenue_anchor_concept`; it should not be simulated by editing the current row or the filing ledger. That contract change is outside this narrow invariant fix.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_economic_invariants tests.test_company_model_forecast tests.test_model_forecast_driver`

Result: 156 tests passed. Added regressions cover non-additive XBRL axes, collapsed multi-dimensional duplicate members, geographic balance instants, and retain the existing real business-segment mismatch refusal.
