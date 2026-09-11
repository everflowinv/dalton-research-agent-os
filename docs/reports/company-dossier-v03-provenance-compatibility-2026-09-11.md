# CompanyDossier 0.3 provenance and compatibility review

## Result

The 0.3 writer is compatible with the persisted 0.1/0.2 dossier chain inspected from a read-only copy of the R5 live Core database. All eight stored versions validate unchanged with the existing authority reader. They remain 0.2 records; the upgrade does not rewrite them or invent model proof.

The next successful incremental run may publish a mixed 0.3 version. A carried 0.2 drafted unit retains `unit_provenance: null` and a null input fingerprint, which means “formal per-unit proof unavailable.” Every unit drafted in that run must carry full 0.3 proof. Later carry-forward preserves the exact historical tuple and its original mission binding. Tests cover a real mission-v1 partial record followed by a mission-v2 update that drafts one additional unit.

## Read-only evidence

The audit copied `core.sqlite`, `core.sqlite-wal`, and `core.sqlite-shm` to an owner-only temporary directory before inspection. It did not construct `DaltonStore`, dispatch a model, or write an authority.

Observed persisted versions:

- 8 total versions across 3 companies.
- All 8 are schema 0.2 and validate byte-for-byte through `validate_dossier_version`.
- Every version is honestly partial under `dossier_completeness`; this does not change its readability status.
- Existing non-null input fingerprints range from 1 to 3 per version. They are deliberately cleared for legacy carry-forward because no formal call provenance exists.
- Two stored guidance profiles were found; both round-trip through the closed `validate_profile` contract.

A full reconstruction of all current prompt inputs on the copy was stopped after 90 seconds in the existing claim-to-evidence projection. The stack was in `query_company_research` evidence lookup, not in the new 0.3 normalizer. This audit therefore does not claim a full-company reconstruction timing acceptance.

## Closed proof contract

For each newly drafted unit, publication now requires:

- the exact company, unit, predecessor, mission, constitution, policy, prompt, and canonical parse-input snapshot;
- a formally successful producer WorkOrder, immutable ResultEnvelope receipt, selected route decision, and exact parser replay whose result equals the published unit;
- an independent verifier WorkOrder bound to the producer route, the exact changed-unit prompt and draft hash, and a formally successful strict `pass` result;
- canonical nested structure, material, classification, and optional guidance profile fields; unknown nested fields are refused.

The body hash excludes provenance so adding proof does not alter research meaning. The record content hash includes it so proof cannot be changed without changing version identity.

## Validation

`tests.test_dossier_unit_provenance`, `tests.test_dossier_lane`, `tests.test_company_dossier`, and `tests.test_company_dossier_draft`: 165 tests passed before the final cross-mission fixture expansion; the final focused provenance module has 8 tests passing. Negative cases cover cross-company/unit borrowing, wrong predecessor, governance drift, unknown nested input, altered published body, route/result receipt drift, and missing producer-route binding.

## Limits

The contract proves what newly produced units saw and what the verifier accepted. It does not retrofit evidence onto old versions, assert dossier quality, or make a partial dossier complete. Historical units with null proof remain explicitly unavailable for formal replay until they are legitimately redrafted.

## Coherent-snapshot correction

The first compatibility pass copied the SQLite main/WAL/SHM files sequentially while R6 was installed. That copy method does not prove a coherent SQLite snapshot, so it is not used as acceptance evidence. The corrected pass opened the source database with `mode=ro` and `query_only=ON`, then used SQLite's backup API to create one coherent 397,873,152-byte owner-only copy in 1.184 seconds. The temporary copies were removed after inspection.

Against that coherent copy, current R6 code reconstructed all 12 unit inputs for each of four current company heads without a shape failure: 48 canonical inputs covering 1,713 bounded material rows. Per-company times after the evidence batching fix were 28.896, 32.228, 29.370, and 27.911 seconds (118.405 seconds total). This closes input-shape compatibility, but it also shows a remaining projection performance issue: each unit independently rebuilds the ClaimIndex snapshot and company research view.

The performance fix in this branch removes the worst inner N+1: the prior `_claim_rows` executed an unindexed full scan of `evidence_relations` for every selected ClaimVersion. Live `EXPLAIN QUERY PLAN` reported `SCAN r`; the table held 2,442 relations at inspection time. The replacement fetches evidence in batches of at most 400 claim-version refs. A fixture regression compares every projected `source_types` and `latest_evidence_retrieved_at` value against direct per-claim authority queries and asserts that two selected claims issue one evidence query. It preserves ordering and content because these two outputs are respectively a sorted set and a maximum.

The remaining repeated-snapshot cost is outside this bounded patch and should be handled by passing one immutable snapshot/research projection through `plan_units`, rather than by adding an arbitrary cache or weakening freshness.
