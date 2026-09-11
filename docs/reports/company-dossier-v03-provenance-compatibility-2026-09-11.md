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
