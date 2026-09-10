# CTSH product-input audit — 2026-09-10

## Scope and method

This was a read-only diagnosis of the CTSH Initial Screen `earnings_calls` failure and the downstream `no_unjudged_events` result. I opened the production Core SQLite database with SQLite URI `mode=ro`; I did not run a model, dispatch a lane, mutate production state, or expose document text, credentials, or private configuration. Counts below are deliberately aggregate.

## Finding

The two visible results describe one consistent input chain:

1. The Initial Screen requires four acquired/read earnings-call transcripts.
2. Ten acquired discovery rows are associated with CTSH under the earnings-call spec, but human review dismissed eight as wrong-company documents. Those eight document identifiers are also associated with EPAM. Only two CTSH documents are `extraction_staged`.
3. The folded stage input therefore reports two qualifying earnings calls, leaving a real shortfall of two. The latest two research-plan versions independently describe the same shortfall and say the AlphaEngine call budget was already exhausted, so acquisition should resume after its cap resets.
4. CTSH has no row in `research_events`. Consequently it has no unjudged event. The tracking producer intentionally scans only companies that have passed Initial Screen; CTSH has `entered`/`gate_failed` history but no `passed` record, so it is excluded before event generation. The judgement result is therefore an empty upstream event set, rather than an anti-join or judgement-selection failure.

The live aggregate evidence at inspection time was:

| Input | CTSH count/state |
|---|---:|
| Earnings-call discoveries marked acquired | 10 |
| Latest reviews dismissed as wrong company | 8 |
| Latest reviews extraction staged | 2 |
| Initial Screen requirement | 4 |
| Research events, all kinds | 0 |
| Unjudged research events | 0 |

This means the product should keep the Initial Screen gate closed and describe the actionable deficit as “two additional valid CTSH earnings calls.” It should not suggest that the event judgement lane can resolve the deficit; that lane has no CTSH event to consume until the screen passes and tracking begins.

## Existing evidence and why it does not close the gate

CTSH has admitted claims, including transcript-origin claims, elsewhere in Core. Those claims do not prove that four distinct, reviewed CTSH earnings-call documents satisfy the source-base requirement. The stage gate is document-based, and only two relevant discovered documents have a successful extraction review. Treating the broader claim population as four calls would silently replace the approved gate definition.

The two staged transcript reviews point to candidate-claim authority identifiers rather than admitted `claim_versions` identifiers. That is expected for the review contract and is not evidence that the review is missing. No source text was inspected or copied for this audit.

## Latent code risks found outside the immediate cause

Two deterministic attribution defects deserve separate fixes, but neither changes the current CTSH conclusion:

- `mission_stage._document_counts` deduplicates across the whole mission by `document_ref` alone. When the same document appears for two companies with equal acquisition rank, its company/spec attribution depends on row order. In this dataset that happens to keep the eight dismissed cross-company rows out of CTSH's folded count, but the mechanism does not consult the dismissal and can credit a future shared row to the wrong company.
- `research_event.document_event_candidates` also deduplicates by document reference after fetching multiple discovery/spec rows. A document present under both an earnings-call and another spec can inherit whichever spec row sorts first and be emitted with the wrong event kind (for example `news` instead of `transcript`).

These should be corrected with explicit company/spec/review semantics and targeted cross-company fixtures. They should not be folded into a CTSH data repair because that would obscure the actual missing-source condition.

## Next step

After the AlphaEngine budget resets, acquire and review two additional CTSH-specific earnings-call transcripts. Re-evaluate the Initial Screen against four distinct valid documents. Once CTSH receives a `passed` stage transition, the existing tracking lane will scan it and create idempotent document/claim events; only then should the event-judgement lane be expected to have CTSH work.
