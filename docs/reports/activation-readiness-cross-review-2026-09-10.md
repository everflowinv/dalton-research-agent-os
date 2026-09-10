# Activation readiness cross-review — 2026-09-10

Reviewed the activation readiness audit after `6887e32`, with production ClaimIndex and company-research projection parity as the primary boundary.

The corrected `_claims` path matches `subject_claim_refs`: latest ClaimVersion per claim chain, current ClaimIndex entry versions, canonical deduplication, stable claim order, and the producer's 1,000-row limit. A test uses the real ClaimIndex authority fixture and production projection to assert parity.

Two remaining correctness defects were repaired:

1. Mission and product records were trusted as plain JSON. A row with a mismatched content hash, record id, company/subject, or version number could be reported as `present`; the mission report could likewise display the JSON's self-asserted hash despite pointer/version drift. The audit now recomputes content hashes, checks the mission pointer hash and exact ids, and classifies a damaged product head as `corrupt` with concrete blockers rather than ready. Mission pointer/version/hash drift aborts the audit.
2. A dossier's immutable historical evidence ref was tested against only the current latest/canonical Claim set. A later ClaimVersion or deduplication could therefore make a valid old citation look invalid. `bound_refs_valid` now means what its name says: every cited ClaimVersion still exists in the append-only ledger. It does not claim current-input freshness; that remains explicitly unprovable until the dossier contract persists an input fingerprint.

No authority is constructed and no schema is initialized. A read-only run against the current live snapshot validated mission v13's pointer and hash and still reported all five companies' three requested products as missing; it wrote only `/tmp/dalton-readiness-cross-review.json`.

Verification:

```text
PYTHONPATH=src python3 -m unittest tests.test_activation_readiness tests.test_claim_index_authority tests.test_company_research_view tests.test_dossier_lane tests.test_debate_map_lane tests.test_mission_event_judgement_lane
```

Result: 147 tests passed in 7.189 seconds. `git diff --check` passed.
