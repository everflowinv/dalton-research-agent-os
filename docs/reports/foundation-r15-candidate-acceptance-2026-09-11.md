# R15 financial candidate acceptance — 2026-09-11

R15 `d1079bafc565e1bfa7d19d3360504fa845e9b327` is frozen and pushed, but **not accepted or deployed**. Live remains verified R14c1. No accepted deployment manifest was created. Owner authorization to deploy remains in place; the block is concrete technical acceptance, not a new permission requirement.

## Frozen checks

- Full discovery: 7,648 tests, 2 failures, 15 errors, one skip, 736.935 seconds. All 17 failures originated in three fake-statement fixtures missing the newly required `unit`/`period_start` fields, which made the lane return unavailable. Root supplied explicit filed units/periods in those fixtures without relaxing production validation; all 49 tests in the affected modules passed. That correction does not retroactively make the frozen full suite pass.
- Wheel: 606 exact runtime files (438 Python, 73 SQL, 92 JSON, 3 HTML); all three embedded JavaScript checks passed.
- Fresh pure-preserve copied-state rehearsal: 14 steps, 42 ticks, zero escape. All 17 model configurations and mission/service/OpenClaw/source permissions were preserved; zero live/configuration/external/model mutations.
- Strict AMZN-format combined fixture: 110 recalculated income, cash-flow, quarterly/annual EPS/share and historical-ratio cells passed, no missing/formula-error/FormulaMap gaps, maximum residual 4.31167e-9. Root inspected final Financials and Driver renders. These are fixture authority and format checks, not proof that live companies have new 0.4 models.

| Evidence | SHA-256 |
| --- | --- |
| Failed full-suite receipt | `4f82001fef9b7d076c9529f8d75f998f8646b0be39f8413a245cf72100f05cab` |
| Full-suite log | `97b985f468d1df42202df03675bf7099cd4617dbf286c3a35b4c22fc105cf4a5` |
| Native counts | `85c15701f2505fd6b0f917c6ca00bbec78074882e3efeb29222152483e1c66fc` |
| Wheel | `e495441f54115e8bf18bee6db8717b064926ed6478fea43a42df11421d2a7fda` |
| Wheel verification | `db171c25ac7c434f3e275c3a486b9efab54e793efeef761fc20392c320d0d7b1` |
| Copied-state binding | `5a5af6ab3a0290a9a89c845ccc4a618367fc97004aa79ae24f3210e4d49c3dbb` |
| Copied-state report | `e758c9fe07d4e81374976400dd0dcba076c1e85f45618d4d0b5f88c88bc318fc` |
| Combined 110-cell Excel QA | `ff8ba7916326285adbcfc0138bdc77c7c196a1cd333baa07f043170a4e68aece` |

## Product checks that hold deployment

The previously passed five-model/500-cell legacy check compared stored canonical model bytes, readiness and recomputed results. It did **not** reconstruct each immutable historical input through the current production exporter. That additional check now found all five real company CLI exports refused with `forecast model does not bind these model inputs`. The new series metadata includes `ambiguous_periods: []` in old 0.2 cash-flow inputs, changing their identity. The series implementation also changed duplicate-selection and mixed-unit behavior, so merely removing an empty field is insufficient for full legacy replay. The repair must preserve the legacy projection contract while leaving new typed models strict. Historical source R14c1 reconstructs all five expected input hashes exactly.

A separate production audit found that `run_model_spec` persists a structurally legal 0.4 spec before replaying its formulas against real filed numbers. A false bridge can therefore become current and leave the forecast lane blocked. The fix must validate source-bound candidate inputs and real historical replay before persistence; absent evidence remains explicitly unavailable, while numerical contradiction must not produce a current spec. Semantic failures must not trigger blind format-repair retries.

The five current models remaining 0.2 is expected while R14c1 is installed. R15 changes the model task and state identity, and all five become eligible after installation. The signed daily budget is nearly exhausted, so new paid work may legitimately wait until UTC midnight. Notes and numeric-period evidence in the specification prompt remain a separate foundation follow-up; full balance-sheet, detailed cash-flow, business segments/category and Street breadth are not complete.

## Next

Finish and independently review both production fixes, retain all failed-candidate evidence, freeze a successor, then repeat full-suite/wheel/copied-state acceptance on that exact source. Deploy only after it passes, perform sustained health observation, and verify actual normal-pipeline research and new-model production separately from runtime health.
