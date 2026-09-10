# Model-stage bridge signed-scope correction

The original bridge mixed the full generic Playbook aspiration with the narrower September 7 execution decision. It therefore required an installed high-frequency calendar and peer-relative sensitivity proof that the currently signed first-coverage program explicitly permits as disclosed, unconnected gaps.

The industry-model gate now requires the active mission-bound framework, sourced drafted material, a computed comparison covering every company in the signed universe with computed revenue, year-on-year revenue growth and gross margin, comparability-limit notes, and explicit candidate-source gaps for deferred high-frequency material. A cadence key, calendar link, TAM estimate, supply estimate, or arbitrary peer USD cell is not accepted as substitute evidence.

The company-model gate now follows the stated P13-M3 criteria: a mission/model-bound sensitivity projection selects three to five drivers, every selected driver has its historical peak/trough/mean band, all what-if cells compute, the consensus bridge is available and quantified, assumptions carry reasons and refs, at least eight historical quarters are present, and the separately replayed filing proof binds the exact model version/hash and reports available. An unrelated peer comparison is ignored.

The lane reads the filing proof through the checked authority reader and includes it in the company verdict. Missing or invalid authority material remains `waiting`; it does not create a synthetic gate pass or terminal failure. Existing fair iteration across companies remains unchanged.

Validation: `tests.test_model_stage_bridge`, `tests.test_company_model_forecast`, and the forecast duplicate regression pass together (37 tests). The tests include explicit unconnected industry gaps, incomplete comparisons, stale model/sensitivity bindings, missing filing proof, incomplete driver bands and consensus bridge, unrelated peer data, recoverable waiting, and later-company progress.

## Authority-level acceptance

A full positive fixture now publishes eight quarters of four statement concepts through `CoverageMissionAuthority`, publishes a four-driver `ForecastModelVersion` plus its replayable filing proof, publishes a vendor-observed `ConsensusEstimateVersion`, derives and publishes a bound `SensitivityProjectionVersion`, records the prerequisite real stage chain, and calls `advance_once` against the same SQLite Store. The company-model gate advances once; the next tick is idle and writes no duplicate stage record. This exposed and fixed a real reader bug: company-stage evaluation tried to read the unrelated industry-framework table first and became `authority_read_failed` when that authority had not initialized the database.

The industry comparison check is Cartesian rather than aggregate: every company in the signed universe must have a computed cell for each required metric. One company's missing margin cannot be hidden by another company's computed margin.
