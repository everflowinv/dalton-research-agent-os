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

## R15a freeze after independent fixes

Successor `34ef59cbbdc4210a58be8513c20c1b237022f822` is frozen as `foundation-r15a-financial`; full discovery and a new copied-state rehearsal are running. Its wheel matches all 606 runtime files and three embedded JavaScript blocks; wheel SHA `c568a0ecec35617169b05eaf2e84d55b554e85f03696b2e1d800cff587913184`. This is not deployment acceptance yet.

The pre-persistence repair is integrated as `86f5a596`, with the enabled-format-repair semantic-refusal regression `ae5521b0`; root independently passed 121 related tests and the strengthened refusal check. The historical input repair author `fc00274176689989fd8d0667a446a8971e6f095b` is integrated as `34ef59cb`. It positively limits legacy replay to no-structure schemas absent/0.1/0.2, restores their old result shape and selection/arithmetic/window contract, and retains strict handling for structured and unknown/future schemas. Author coverage passed 321 tests; independent review passed 151 in 14.847 seconds. The combined integration path passed 85 tests in 8.008 seconds.

Final five-company production CLI exports now succeed, reconstructing each stored input hash exactly. Their 484 computed model cells pass LibreOffice recalculation with maximum displayed-unit residual 5.8e-11 and no formula errors. All raw/recalculated workbooks and Formula Map hashes are bound; the 23 rehearsal SQLite/WAL/SHM content hashes are unchanged. Receipt `/private/tmp/dalton-r15-five-company-xlsx-qa-v1/legacy-input-replay-fix-receipt.json`, SHA `a41f1fd20393c26b95d4f0afdcec2edae92596e55735d4ad8c9f457ae92b68a2`, distinctly names the frozen fix and old R14c1 reference tree. These are legacy 0.2 exports with EPS/typed annual authority explicitly unavailable, not claims of new company-specific 0.4 production.

## R15a full acceptance result and final visual correction

R15a finished 7,655 tests with zero failures/errors and one skip in 747.983 seconds. Receipt SHA `2d4349a5e9a7221b140f25cf1ecf0836a4e848fa58339c5cbe18da1cd7f91b39`; log SHA `fe62b31fdcbafce8401572a94c6aa1f4365c1751981b993dc0b3619681acde87`; native result SHA `880d28ea253914c094848ff6fede69adc8519ef1077a254f055001c62763b2eb`. Its fresh pure-preserve rehearsal passed 14 steps/42 ticks/zero escape, binding SHA `afec3032e10d9b05df03c0a702df1fe2cef6024a05e05f39694f4ffed6099ef5`.

Actual ACN/IBM raw production workbook visual QA found clipped row-one unit headers in Financials/Driver. Period columns, Valuation, other hierarchy, colors and long labels passed. The correction will give the unit label an explicit readable span without changing values/formulas or moving the period grid. R15a is therefore retained as an undeployed passed runtime candidate; no accepted deployment manifest is published while the owner's strict final Excel-format criterion is still being corrected. Visual receipt SHA `bccc0016e96c8e6b097fec2dc78cb475f600ab55f21972bd5755b8ce1320f16e`.

Completed R15 scratch databases were verified and removed after the five-company QA closed: 23 SQLite/WAL/SHM files, 1,340,170,240 bytes, cleanup receipt SHA `d3512fdbd0e3a3f58296a32bcdb9406ff2e8308cbc45b72b45a27ecbaf62b3bb`. The completed R14c1 v2 rehearsal's 23 SQLite/WAL/SHM files were likewise removed after the successor rehearsal passed, freeing 1,291,857,920 bytes, receipt SHA `0a4f5621bb87398afbe5f30fbcbcf3306a360990b74dd1c3f97eeba31eac86b2`. All non-SQLite evidence/configuration files and every actual live/rollback backup remain, including the fresh R15a visual-QA input copy. No archive was created.

Two rehearsal warnings were independently checked and remain nonblocking. The retired verifier pin belongs to disabled legacy ThesisImpact; none of 17 active model roles uses it. The absent annual-only lane has zero admissions and is superseded by the enabled source-neutral lane, whose active mission contains exactly five acquired SEC annual documents. Neither warrants changing live configuration in this release.


## R15b final freeze and deployment start

Final source `5e1d159bf99da02c8e456d07b4d54141b4f32c7c` passed full discovery: 7,655 tests, zero failures/errors, one skip, 743.090 seconds. Log SHA `23ef84957c1cba47d6b5a6a16005424af74e232d0b7a22dde26e33dd6b494246`; native result SHA `880d28ea253914c094848ff6fede69adc8519ef1077a254f055001c62763b2eb`; runner SHA `244838b4122ee1507fe6a397a40fae5f5c186e36e530c6cd6b546ed09ae30268`. Wheel SHA `8d2e32c0b9b7b49166284771b0947ec9fa58dc5653ef4ce5f83cce9b9c076e93` matches 606 files and three embedded JS checks. Fresh rehearsal passed 14 steps/42 ticks/zero escape with 17 unchanged model configs, binding SHA `fded7fbc92cfbbae0be9623095be63fec4a87798314ef2f804ddcc27becb0f26`.

Header corrections `00e6af0c`/`5e1d159b` merge A1:C1 and redistribute 0.75 width from D to C, retaining total A:D width, period positions and exact unit text. Independent 30-test validation and actual ACN/IBM renders passed; root inspected final Financials/Driver. ACN 3,081 values/271 formulas and IBM 3,774 values/330 formulas are unchanged. Visual receipt SHA `038b2f148b9a67902230950cca36c1b36e6189c05dd85fd81d7bae685d2a4ed6`. This meets the corrected core-sheet presentation check, not full statement/Category/Street capability.

Accepted manifest SHA `9137c06d5564fa148b3f82b09293f7f81205f78f3c97e1812fa60947540a2a89` passed read-only live preflight. Disk gate required 4,745,776,373 bytes against 5,603,889,152 available. Deployment started under standing owner authorization; install, sustained health, publication and actual product outcomes remain pending at this checkpoint.

Completed R15a scratch SQLite/WAL/SHM files were checked for stable metadata/content and no open handles, then removed: 25 files, 1,341,689,856 bytes. Receipt `/private/tmp/dalton-completed-r15a-scratch-sqlite-cleanup-20260911T230857Z.json`, SHA `913d170826b9131e375321710d64f4257e2c9784a2d61809d6ee19bf63bf5228`. All non-SQLite evidence, active R15b scratch and actual rollback/live backups remain; no archive was created.
