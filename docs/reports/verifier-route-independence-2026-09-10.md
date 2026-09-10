# Verifier route independence — 2026-09-10

## Scope

This change makes model-family independence a routing constraint before a verifier call is admitted or sent to the model broker. It does not change model tiers, owner selections, immutable routing policy records, publication authority, or deployment state.

## Delivered

- `CockpitModel.call` accepts the immutable route decision refs of every producer whose output is being checked. Those refs are part of the WorkOrder identity, so the same verifier request cannot replay work performed against a different producer set.
- Producer families are resolved from the router's served decisions. Missing, rejected, or otherwise unresolvable producer decisions fail closed before budget admission and before adapter execution.
- Tier fallback filters every producer family. This covers a draft assembled from multiple model calls as well as the common Astra failure → Claude producer → GLM verifier path. The single-profile routing path also checks the complete producer-family set before admission.
- A single signature-aware compatibility helper forwards route refs to `CockpitModel` while preserving historical test doubles whose `call` method predates the optional argument. It does not catch or retry a `TypeError` raised inside a model call.
- Company dossier, zero-base review, event judgement, event reflection, earnings preview, and earnings calibration verifier calls now supply their producer route refs. Existing post-call family verification remains in place.

## Evidence

Focused command:

```text
PYTHONPATH=src python3 -m unittest tests.test_cockpit_model_fallback tests.test_model_fallback_chain tests.test_company_dossier_draft tests.test_zero_base_review tests.test_event_judgement tests.test_earnings_season_cli
Ran 224 tests in 6.995s — OK
```

The tests exercise the real `ModelRouter` with a mocked broker adapter, including provider fallback, pre-charge family exclusion, multiple producer families, no-independent-candidate zero-call behavior, unknown producer fail-closed behavior, WorkOrder identity separation, and legacy fake compatibility.

`git diff --check` also passed.

## Deployment and remaining validation

No live configuration, policy, signing, or deployment action was performed. The parent integration branch should run the repository-wide suite after cherry-picking because this isolated slice intentionally ran focused tests only.
