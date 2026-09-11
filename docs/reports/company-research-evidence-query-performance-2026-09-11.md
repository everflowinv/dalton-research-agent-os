# Company research evidence-query performance

`_claim_rows` previously performed one `evidence_relations` join for every selected ClaimVersion. The production schema has no index on `evidence_relations.claim_version_id`; SQLite therefore performed a full relation-table scan for each claim. The bounded fix selects the company's immutable claim-version refs first and fetches their evidence metadata in batches of 400.

The output contract is unchanged. Evidence order never enters the projection: `source_types` is sorted after set de-duplication and `latest_evidence_retrieved_at` is a maximum. The regression compares both fields against direct per-claim authority queries and verifies one evidence query for a two-claim company.

On a coherent SQLite backup of the installed R6 state, all 48 current Dossier unit inputs reconstructed without shape errors. Total time remained 118.405 seconds because `plan_units` independently rebuilds the ClaimIndex/company view for every unit. A later bounded optimization should share one immutable snapshot within a reconstruction call; this patch does not introduce caching or change authority semantics.
