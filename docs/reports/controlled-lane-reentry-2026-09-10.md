# Controlled lane reentry

Status: source candidate; no live state was read or changed and no model call was made.

Event judgement and company dossier coordinators may reenter a held child only when that exact persisted ticket summary contains a validated failed model trace and the budget authority contains an exact, current, unconsumed operator redrive. The launchers restrict traces to their own model purposes. Legacy summaries without the trace remain held, and ordinary content refusals receive no retry.

Before the fresh child starts, the launcher writes an owner-only, append-only attempt marker under the same single-flight lock that starts the process. The marker binds the authorization suffix, exact ticket, company signature or event batch, and both the SHA-256 and exact base64-encoded bytes of the prior summary. The original summary therefore remains reviewable after the child replaces `summary.json`. Exclusive marker creation makes a crash before model enqueue finite across ticks and writer restarts, while the uninterrupted lock prevents another start from consuming the child slot between claim and spawn. A successful Scheduler enqueue remains the model-level consumption record; request IDs, prompts, admission rules, and budget limits are unchanged.

Validation: 181 focused controlled-reentry, child-launcher, event-lane, and dossier-lane tests passed. Python byte compilation and `git diff --check` passed.
