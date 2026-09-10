# Dossier prompt-contract recovery — 2026-09-10

The live producer repeatedly returned more than the closed six-item `gaps` limit because the parser enforced the limit but the prompt did not state it. The prompt now states every hidden bounded string/list constraint relevant to the returned draft: gap count and length, sentence/unknown length, distinct source count, and the existing sentence caps. Validation remains strict; output is never truncated or repaired.

The producer contract has a stable fingerprint in the lane signature. This gives a previously terminal refusal one new task identity after this reviewed contract change while unchanged successful work retains its existing prompt identity. If every attempted unit still violates the contract, the child reports `rubric_refused`; the coordinator records that result as terminal before its generic failed-child branch, so the same company/evidence/configuration does not launch repeatedly. Transport/model unavailability remains a retryable failure rather than being mislabeled as content refusal, and partial valid drafts retain their existing behavior.

Validation:

```text
PYTHONPATH=src python3 -m unittest tests.test_company_dossier_draft tests.test_company_dossier tests.test_dossier_lane tests.test_cockpit_model_fallback
```

Result: 166 tests passed. No live state, broker, model, connector, or deployment was used.
