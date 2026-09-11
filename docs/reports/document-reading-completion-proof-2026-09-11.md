# Document reading completion proof — 2026-09-11

## Finding

A resolved document review is not evidence that its bytes were read. Production forensics found reviews closed with the stable “no admissible” rationale even though every model window had failed. The prior projection therefore overstated `read` while acquired SEC filings remained valid inventory.

## Contract

`DocumentReadCompletionAuthority` records one immutable proof per review while the review is still open. It asks an injected authoritative receipt reader to re-read each formal successful window and requires exact context, source bytes, WorkOrder, ResultEnvelope, offset, and next-offset bindings. Windows must form a contiguous chain from offset zero through an explicit terminal `next_offset=null`, with one stable source-content and source-review hash and the mission automation principal.

The producer sequence is proof first, review resolution second. A crash before proof leaves the review open. A crash after proof but before resolution also leaves it open and retryable; the identical proof replays as a duplicate. Mission readiness counts the document as read only after the current review is resolved and its immutable identity matches the source-review snapshot inside the proof. Legacy resolved reviews have no proof and remain read-unknown. Exact SEC 10-K inventory can still count as acquired while read remains zero.

The extraction service integration must expose `read_completion_receipt(review_id, source_review_hash, offset, actor_ref)` from the same path that validates Scheduler formal result, WorkOrder, invocation, route/profile, and mission-budget bindings. The CLI must not query Scheduler rows or construct receipts itself.

## Verification

`PYTHONPATH=/Users/everflow/Projects/dalton-document-read-proof-worktree/src /Users/everflow/Projects/dalton-research-agent-os/.venv/bin/python -m unittest tests.test_document_read_completion tests.test_mission_stage`

The focused suite covers successful multi-window proof, proof-before-resolution crash behavior, idempotent replay after resolution, failed and incomplete chains, authority drift, wrong actor/review hash, raw insert refusal, legacy unresolved read counts, and acquired-vs-read projection. Result: 33 tests passed.

## Live read-only projection before proof deployment

Against active mission `coverage-mission-version:us-it-services:14`, the proof table is absent, so no legacy qualitative dismissal is promoted to read. The projected acquired/read pairs were: ACN annual 1/0 and broker 10/0; CTSH annual 1/0, earnings calls 1/0; EPAM annual 1/0, calls 2/0, broker 8/0; IBM annual 1/0 and broker 3/0; DXC annual 1/0, calls 4/0, broker 1/0. Quarterly financials remain 4/4 for each company through their separate statement-reading authority. This was a URI `mode=ro` query; it made no live writes.
