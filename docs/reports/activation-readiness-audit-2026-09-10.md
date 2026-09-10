# Five-company activation readiness audit — 2026-09-10

## Result

Added `scripts/audit_activation_readiness.py`, a read-only acceptance tool for the current mission's company universe. It opens Core SQLite with URI `mode=ro` and `PRAGMA query_only=ON`; it does not construct an authority, execute schema SQL, call a model, use a connector, or access the network. Optional JSON and Markdown outputs are ordinary operator files outside Core.

For each company it reports the latest CompanyDossier, company DebateMap, and EventJudgement with the exact stored ref, content hash, and creation time. A row count never qualifies as acceptance. The tool selects the head of the company's version chain or the newest exact judgement, checks current mission bindings where the artifact contract carries one, verifies the judgement's exact event hash, and recomputes DebateMap's current claim evidence fingerprint. Missing artifacts carry all applicable blockers: mission grant, producer/verifier/policy files, invalid JSON or absent model-route bindings, no eligible Claims, no unjudged events, or a remaining lane refusal that requires the failure ledger.

CompanyDossierVersion does not persist the producer's exact input fingerprint. Its mission binding is exact and its cited refs can be checked, but current-input freshness cannot be proven merely because no row count changed. The audit therefore emits `fresh: null` with that contract limitation instead of guessing. DebateMapVersion has the inverse gap: it persists the exact evidence fingerprint but has no mission-version binding field, so mission freshness is also `null`. These are acceptance-contract findings, not silently inferred passes.

## Live read-only result

The tool was run against the current private live Core and wrote `/tmp/dalton-activation-readiness.json` and `/tmp/dalton-activation-readiness.md`. Current mission is `coverage-mission-version:us-it-services:13`, hash `d59edcb9ff3757976affe199be8f316d4dec2f8c203468f56fb6df0404ed19f3`, with ACN, CTSH, EPAM, IBM, and DXC.

None of the fifteen requested first artifacts exists. Every company dossier is blocked by the missing `dossier` mission grant (and the JSON includes the additional missing config/policy blockers). Every DebateMap is blocked by the missing `debate_map` grant (plus config where absent). Every EventJudgement lacks the paired `event-judgement-model-config.json` and `event-verifier-model-config.json`; the JSON separately shows whether each company currently has an unjudged event. No rehearsal launch or historical count is reported as completion.

The live database and configuration were not changed. The generated live outputs remain in `/tmp` and contain no repository fixture or committed live data.

## Verification

```text
PYTHONPATH=src python3 -m unittest tests.test_activation_readiness tests.test_dossier_lane tests.test_debate_map_lane tests.test_mission_event_judgement_lane
```

Result: 130 tests passed in 6.359 seconds. Tests cover exact per-company head selection and hashes, mission and input freshness, explicit contract-unverifiable states, actionable missing reasons, Markdown output, and a failed write through the audit's SQLite handle. `git diff --check` passed.
