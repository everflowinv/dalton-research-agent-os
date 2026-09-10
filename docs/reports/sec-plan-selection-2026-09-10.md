# SEC discovery-plan selection — 2026-09-10

## Result

The writer LaunchAgent now has a persistent, owner-controlled way to select a reviewed SEC discovery plan without treating the newest file as approved.

When `discovery-plans/sec-filings-plan-selection-v1.json` is absent, rendering keeps the established `us-it-services-sec-filings-v1.json` path. When the selector exists, rendering fails closed unless it is an `approved` SEC selector with a valid content hash. Its `plan_path` must name one direct child of the state directory's `discovery-plans` directory. The selected plan is loaded through the canonical discovery-plan validator and must match the selector's exact plan id, content hash, and `source:sec-edgar` source.

The 8-K proposal builder now emits a third review artifact. By default it is written beside the review bundle with `.selector.json`; `--selector-output` can name a distinct path. The generated selector is `proposed`, binds the candidate's exact ref/hash and installed filename, and cannot activate a LaunchAgent. Proposal outputs remain forbidden inside the source state directory.

## Owner review and later installation

A review run may use:

```text
python3 scripts/build_sec_8k_discovery_proposal.py \
  --active-plan <state>/discovery-plans/us-it-services-sec-filings-v1.json \
  --source-core <state>/core.sqlite \
  --governance <state>/connector-governance/sec-filings-index-v1.json \
  --plan-output <review>/us-it-services-sec-filings-v2.json \
  --bundle-output <review>/sec-8k.review.json \
  --selector-output <review>/sec-filings-plan-selection-v1.json
```

After reviewing the candidate and bundle, the owner may approve the selector in place by changing `status` to `approved` and recomputing its canonical `content_hash`, then install the exact candidate and selector bytes under `discovery-plans/`. This change does not perform that approval or installation. A proposed, malformed, tampered, mismatched, or path-escaping selector stops render rather than reverting silently or choosing another plan.

## Validation

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_sec_plan_selection \
  tests.test_sec_8k_discovery_proposal \
  tests.test_service
```

Result: 61 tests passed. Coverage includes real plist rendering for default v1 and selected v2, proposed-selector refusal, selector and plan tampering, plan-ref mismatch, path escape refusal, and proposal binding. `git diff --check` passed.

No live state, original main checkout, governance approval, LaunchAgent, rehearsal, or PROJECT_STATUS was changed.
