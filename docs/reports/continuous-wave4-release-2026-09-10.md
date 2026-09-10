# Wave 4 release acceptance — 2026-09-10

Status: code and predeployment acceptance passed. No live deployment, owner signature, model call, or host OpenClaw edit has been performed.

## Exact release binding

- Code: `79e0ef7ca9348484bc549cdc36b3846bd404f7b3`, unchanged detached release-acceptance checkout.
- Development branch: `continuous-integration-wave4`; subsequent integration commits are documentation only.
- Wheel: `dalton_core-0.1.0.dev0-py3-none-any.whl`.
- Wheel SHA-256: `99ba69af9960bc9e115842ff2f21c86802abf396dc802f35a85fecd4df4afec1`.
- Packaged 363 Python + 67 SQL + 3 HTML files match the frozen source; all inline JavaScript syntax checks passed.

## Resulting behavior

Current broker metadata determines routes, prices, credentials and capacity. Semantic changes and expired observations append new immutable profile versions. Historical profile IDs may retain old names; Cockpit shows the actual provider/model. Unknown route lineage remains unknown until the owner publishes a current, route-bound Dalton declaration. Metadata declarations preserve version history, reject stale UI identity and surface whether the current route actually applied the declaration.

Each purpose's configured policy determines model choice. Generation and independent verification use distinct purposes and actual producer evidence. Explicit selections work on legacy pins, update credential references across providers, preserve unrelated policy semantics, and stage/recover file changes. Resident service configuration changes report their restart requirement. Cockpit reads each purpose's actual router and policy, isolates unresolved pins, shows multi-profile allow-sets without inventing a fallback order, and keeps external injected configurations read-only.

Artifact work also includes per-unit dossier input fingerprints with read-only reconstruction, DebateMap mission binding and immutable schema migration, typed activation audit, and the exact-hash SEC 8-K discovery installation helper with rollback. Details are in the linked development reports; these changes are code delivery, not evidence of generated live products.

## Acceptance evidence

- Full discovery: **6,248 tests / 519.224s, OK (1 skip)** at `/tmp/dalton-wave4-release-full-tests.log`; SHA-256 `480538e29c4bd31456f23671c378e83c0f587250cca467673095fe9c872d67b5`.
- Previous `f1d61b8` checkpoint: 6,243 tests / 514.522s, 1 skip. This is historical, not a substitute for the release run.
- Subsequent focused checks: 142 deployment/model-selection/Cockpit tests and 106 rehearsal tests passed.
- Current live-copy rehearsal: 13/13 steps, 67/67 schemas, 38 tick entries, zero escaped calls.
- All 12 preserved/installed model configurations have their internal paths confined to the temporary root. This closes the earlier extraction-config path gap.
- Copied Cockpit model page: available, 32 purposes, 26 configured and 6 unconfigured; no missing-policy/cross-router exception.
- Rehearsal report SHA-256: `1b850e600ef5a229b9d3a895fbcab836d505c00430f7f0cad00974843015e6d4`.
- Per-purpose page summary SHA-256: `25ec0fb36f7b578233241740c78940d54a2464c14bc04e7e2be234faf50269e3`.
- Private deployment script: shell syntax and the actual pre-stop prefix passed against an isolated packet; exact archive materialization and read-only mission verification passed. The service-stop/install portion has not run.

The first 61f5675 full run had three fixture errors, fixed by explicitly syncing the catalog before role setup. Later 13-step rehearsals at 61f5675/f1d61b8 did not cover internal model-config paths; only this release rehearsal carries that stronger confinement result. Earlier reports remain preserved.

## Owner actions and next step

The private packet is `/Users/everflow/Projects/dalton-owner-activation-20260910`. Its release manifest now records the passed full suite and exact accepted artifacts. It contains reviewed deployment, mission-v14 signing and read-only postdeployment scripts. Deployment verifies the exact release, drains services, creates and verifies a database backup, installs an archived commit, and retains its source. Signing adds 11 write kinds and 3 checkpoints while preserving the five-company universe, budget and source bindings.

After deployment/signing, verify actual dossier, DebateMap and event-judgement products for ACN, CTSH, EPAM, IBM and DXC. The predeploy baseline has all 15 targets missing. Stub dispatch never counts as product completion. The postdeploy checker compares installed Python/SQL/HTML with the accepted artifact and reports mission, artifact and per-purpose route state using read-only database handles.

Remaining explicit owner/runtime gates: route-bound DeepSeek lineage confirmation; any SEC 8-K plan approval; the retired thesis-impact verifier pin if that workflow is activated; and deliberate alignment of the stale retired Agenda policy before reuse. Source-specific wiki/crowd/prior-research gates and deliberately unseeded records remain visible. The rehearsal lacks the model-catalog-sync switch file; real `install.sh` creates it, so confirm it during installed-runtime acceptance.

Original main retains the other session's four DeepSeek files. Its patch SHA-256 is `43832e8e6d32cec9bf48b59044d200df0d57a99d30598859c23d4c1360207932`. No responsibility for Dalton migration is delegated to that unrelated session.

## Supporting reports

- [Continuous progress](continuous-wave4-progress-2026-09-10.md)
- [Bound router repair](cockpit-bound-router-repair-2026-09-10.md)
- [Model configuration confinement](rehearsal-model-config-confinement-2026-09-10.md)
- [Release wheel validation](wave4-release-wheel-validation-2026-09-10.md)
- [Release activation rehearsal](wave4-release-activation-rehearsal-2026-09-10.md)
