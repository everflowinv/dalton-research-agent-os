# Foundation scheduling fairness audit — 2026-09-10

The current live d297c0f runtime is healthy after disk recovery, but the product audit remains 1/15. A completed or rejected child is not proof that the rest of the mission can advance.

## Confirmed current blockers

- Dossier uses one global claim/index/head signature and checks its terminal failure before selecting a company. The child always selects the first screened company needing work. A content refusal for ACN therefore stalls other companies; unrelated evidence can reopen the same global run. A per-company eligibility/input signature and explicitly selected child are in development. Real content refusals must remain held for their exact company/input.
- Event judgement selects the globally newest unjudged event without applying the supplied mission/universe. `_last_batch` suppresses it after a failed batch, even when the child produced no judgement, so other eligible events remain unvisited. Mission-scoped group selection, durable per-group failure handling and bounded capacity recovery are in development.
- Exact host redrive authorities alone cannot force a held coordinator to re-enter the adapter. Existing 19 rows retain exact old model task/result/mission/cost bindings, but legacy child summaries omit the task-to-child edge. They must not be associated by company, purpose or timestamp guesses. New child failure tracing will preserve that edge. Any one-time child replay must keep original model request identities so only an already-reviewed exact redrive can admit a fresh call.

## Follow-on audit before foundation completion

Deep Insight and Investment Memo coordinators also use one global signature before a child selects a company. Their deterministic no-input selection can skip missing prerequisites, but a paid content refusal still risks blocking other eligible companies. Earnings-season uses `_last_batch`; verify partial/failure continuation against its actual per-company producer semantics. These paths must be checked and repaired where reproduced after the current dossier/event changes. Industry Framework has a single mission industry, so the same code shape is not by itself evidence of cross-company starvation.

No human research/source approval was changed. Higher-level investment methodology remains deferred.
