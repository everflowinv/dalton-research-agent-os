# Deep Insight fairness review — 2026-09-10

## Review result

The company-scoped scheduling in `51759eaf` correctly prevents one content
refusal from starving the next company and keeps the existing human checkpoint
inside the child. Permission holds are scoped by company and move when the
mission, governance pointer, policy, or model configuration changes.

Two execution-integrity gaps remained:

1. The coordinator named a ticket from a source signature, but the launcher did
   not pass that signature to the child. A dossier, debate map, mission, prior
   gate decision, numeric input, valuation, model configuration, policy, or
   packaged verifier contract could move between selection and execution while
   the child drafted the new state under the old ticket.
2. After a writer restart, a completed child whose result had not yet been
   settled could be relaunched with the same ticket, truncating its log and
   potentially repeating four model calls.

## Correction

The shared company input fingerprint now includes the complete company source
projection, current mission record, the three consumed configuration/policy
documents, and the packaged Deep Insight verifier-provider contract. The
launcher records and passes the exact 64-character fingerprint. The child
recomputes it after opening the authority and refuses `input_changed` before
scope evaluation, drafting, or any model call when it differs.

The launcher adopts an exact persisted ticket once after restart. This lets the
coordinator settle its durable content, permission, dependency, or transient
outcome without launching another child. A later permitted retry is not trapped
replaying that ticket indefinitely.

Published and pending human gates retain their existing authority rules. The
lane cannot approve, reject, reopen, or skip the `deep_insight_gate` checkpoint.
No human decision path was changed.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_deep_insight_gate_lane`

Result: 62 tests passed. New tests cover exact input acceptance, moved-input
refusal with zero producer/verifier calls, and restart ticket adoption; existing tests cover content-terminal
holds, permission recovery, company fairness, stage gating, independent
verification, and human decisions.

No live state, model call, network request, or deployment was performed.
