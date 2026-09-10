# Foundation scheduling fairness audit — 2026-09-10

The current live d297c0f runtime is healthy after disk recovery, but the product audit remains 1/15. A completed or rejected child is not proof that the rest of the mission can advance.

## Confirmed current blockers

- Dossier uses one global claim/index/head signature and checks its terminal failure before selecting a company. The child always selects the first screened company needing work. A content refusal for ACN therefore stalls other companies; unrelated evidence can reopen the same global run. A per-company eligibility/input signature and explicitly selected child are in development. Real content refusals must remain held for their exact company/input.
- Event judgement selects the globally newest unjudged event without applying the supplied mission/universe. `_last_batch` suppresses it after a failed batch, even when the child produced no judgement, so other eligible events remain unvisited. Mission-scoped group selection, durable per-group failure handling and bounded capacity recovery are in development.
- Exact host redrive authorities alone cannot force a held coordinator to re-enter the adapter. Existing 19 rows retain exact old model task/result/mission/cost bindings, but legacy child summaries omit the task-to-child edge. They must not be associated by company, purpose or timestamp guesses. New child failure tracing will preserve that edge. Any one-time child replay must keep original model request identities so only an already-reviewed exact redrive can admit a fresh call.

## Follow-on audit before foundation completion

Deep Insight and Investment Memo coordinators also use one global signature before a child selects a company. Their deterministic no-input selection can skip missing prerequisites, but a paid content refusal still risks blocking other eligible companies. Earnings-season uses `_last_batch`; verify partial/failure continuation against its actual per-company producer semantics. These paths must be checked and repaired where reproduced after the current dossier/event changes. Industry Framework has a single mission industry, so the same code shape is not by itself evidence of cross-company starvation.

No human research/source approval was changed. Higher-level investment methodology remains deferred.

## Integration checkpoint, 23:12 UTC

Event group fairness (including older groups of the same company), exact child group validation, and restart adoption are integrated. Deep Insight now has company-scoped identities, exact source checks in the child, and completed-ticket adoption; Memo has company-scoped frozen input/configuration/provider identities. Earnings uses occurrence/window-scoped durable outcomes and streams all calendar authority refs before the caller's output bound; forty recent calendar revisions cannot hide an older open occurrence. Root integration suites passed 188 event/failure tests, 140 gate/Memo/shared-ledger tests, and 115 earnings tests. An earlier gate test command named a nonexistent module and failed import; the corrected module list passed. None of these results replaces the pending frozen full suite.

A separate exact persisted failure-trace reentry helper is under integration review, to let a subsequently approved host recovery reach the adapter without guessing historical child/task associations or clearing content holds. Zero-base review also has a live route-unavailable failure labelled as content refusal; its classification/configuration recovery is being audited.

The live product snapshot at 23:04:55 UTC has four current mission/evidence-bound DebateMaps (ACN, EPAM, IBM, DXC), up from one. Forecast model authority remains empty. A strict read-only call to the actual input builder found all five latest specifications fail with `cost-bound specification carries no cost template metadata`: validation creates that metadata, but specification persistence drops it. The chooser suppresses these errors and incorrectly reports all models current. A round-trip persistence/migration and blocked-input reporting fix is in progress. Old immutable specifications will not be rewritten or have metadata invented.

Next: close these actual foundation blockers, freeze exact source for full suite/wheel/current-state rehearsal, deploy under the existing authorization, and audit actual products again. Higher-level methodology remains deferred.
