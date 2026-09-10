# Controlled lane reentry

Status: source candidate; no live state was read or changed and no model call was made.

Event judgement and company dossier coordinators may reenter a held child only when that exact persisted ticket summary contains a validated failed model trace and the budget authority contains an exact, current, unconsumed operator redrive. The launchers restrict traces to their own model purposes. Legacy summaries without the trace remain held, and ordinary content refusals receive no retry.

Before the fresh child starts, the launcher writes an owner-only, append-only attempt marker under its single-flight lock. The marker binds the authorization suffix, exact ticket, company signature or event batch, and SHA-256 of the retained prior summary. Its exclusive creation makes a crash before model enqueue finite across ticks and writer restarts. A successful Scheduler enqueue remains the model-level consumption record; request IDs, prompts, admission rules, and budget limits are unchanged.

Validation: 181 focused controlled-reentry, child-launcher, event-lane, and dossier-lane tests passed. Python byte compilation and `git diff --check` passed.
