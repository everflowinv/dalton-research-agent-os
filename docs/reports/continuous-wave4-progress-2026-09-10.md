# Continuous wave 4 progress

Owner clarification: the other session updates DeepSeek generally; Dalton compatibility is this session's responsibility. Runtime model selection must remain editable through Cockpit, including every producer/verifier role. The solution uses current broker public metadata plus append-only Dalton profile versions and explicit Dalton-owned lineage declarations; it does not hardcode a replacement model alias or overwrite the original main workspace.

In integration: typed artifact audit, DebateMap mission binding, per-unit dossier inputs with read-only reconstruction, and SEC 8-K owner-install helper. In review: truthful DebateMap schema migration, dynamic model catalog semantics/independence, complete Cockpit purpose/config coverage, persisted public metadata declarations, and real copied-state routing checks. Wave 3 remains the latest full-suite checkpoint: d21de7d, 6,184 tests passed, pushed with report456ed54 to continuous-integration-wave3. Wave 4 is not yet full-suite validated or deployed.

Next: merge remaining catalog/Cockpit repairs, run focused real routing and artifact tests, freeze a new full-suite checkpoint, rehearse against the current host catalog, rebuild the owner packet, and request only the concrete signing/deployment actions still required. The old request for the other session to provide a Dalton migration commit is superseded.

## Integrated checkpoint (not a release)

- Current broker route, price, context and credentials now append profile versions; no replacement DeepSeek alias is baked into runtime logic. Catalog observation expiry renews append-only and remains idempotent between expiries.
- Real live-router backup + current catalog + production setup routed all 11 requested roles without broker/model calls. DeepSeek's new alias has unknown lineage until a route-bound Dalton declaration is approved; see `live-copy-model-route-acceptance-2026-09-10.md`.
- Cockpit explicitly registers 13 known model config filenames, separates seven additional verifier purposes, preflights all present files, rolls back partial file replacement, and supports retry after immutable publication. Actual producer references determine independence; bootstrap brain defaults no longer prevent valid verifier choices.
- SEC helper validates exact signed inputs and target conflicts; its 7 tests plus 4 proposal tests were the reported 11. Independent review adds two helper cases and rollback of newly created outputs on late filesystem failure. Prepare only was run against live inputs.
- Focused integrated artifact/catalog checks: 323 passed; verifier selection/fallback: 109 passed; availability/catalog: 19 passed. These runs precede subsequent metadata/UI/legacy path changes and are not a full-suite claim.

Remaining acceptance: complete route-bound metadata validation + human Cockpit declaration, old single-pin overrides, extraction/thesis-impact/agenda runtime selection, then freeze code for full suite/wheel/current-live activation rehearsal. No live deployment, signing, gateway configuration mutation or paid calls performed.

## Frozen acceptance in progress

Code frozen at `61f56759c5e249be150623844df3ee4550a5ee65` in a detached acceptance worktree. Full `unittest discover` is running against that unchanged source. Final pre-freeze focused checks: 195 passed. Wheel verification passed: 430 Python/SQL files byte-identical; SHA-256 `097f7554c7b8ce8f489c844c6db73e55c2ce790269d2812c7d2d414f03b563ec`. Cockpit JavaScript syntax passed.

The latest integration now covers cross-provider credential references, exact-purpose routing for legacy workers, quality's independent verifier, owner metadata binding/deduplication/application status, and truthful legacy pin displays. Resident service choices explicitly report that a restart is needed. A preliminary current-live-copy activation rehearsal passed 13 steps, 67 schemas and 38 tick entries with zero escaped calls; the final frozen-code rehearsal is separate and still being recorded.

Private deployment review includes a guarded deploy script and the accepted wheel. Its release manifest deliberately remains pending until full-suite, final rehearsal and runtime acceptance results are complete. The script has only been syntax-checked; neither deployment nor any owner signature has run.

## Full-suite findings and final read-side work

The first frozen run (`61f5675`) completed **6,242 tests / 529.886s with 3 errors and 1 skip**. All three errors were deployment-pair fixtures relying on removed implicit static catalog seeding. Production installs synchronize the catalog first; the fixtures now do the same (`5e326f6`, 7 deployment-pair tests passed). The stronger same-family verifier budget test also passed separately. This is not recorded as a green full run.

Final frozen activation rehearsal passed all 13 steps, 67 schemas and 38 tick entries / zero escaped. Read-only copied-state selection passed planner and both thesis-impact roles. The old Agenda pin remains v2 against v3; selection explicitly refuses it with no file changes. It requires a deliberate pin migration if that retired workflow is reactivated.

A remaining Cockpit read-side issue is being closed before final acceptance: a single representative model config cannot describe every role's actual policy. Per-purpose consumer bindings and visible provider/model routes are being wired so the page shows the same model the worker will use. After that change, freeze again and repeat the complete suite and artifacts. Private deployment remains locked pending that acceptance.

## Final acceptance checkpoint

Per-purpose actual consumer bindings are integrated at `f1d61b8ad7c0d9a94830cead535570d6b0a5e7a5`. Cockpit displays each installed role's policy and current provider/model route; dynamically supplied paths remain explicitly unconfigured instead of borrowing extraction's policy. Integrated deployment/model-selection/Cockpit checks passed **139 tests / 24.792s, 1 skip**.

A new, unchanged detached checkout is running full discovery. Parallel Sol acceptance covers the new wheel, JavaScript syntax, current-live-copy activation, and independent policy-binding review. The first frozen test failure remains historical evidence; it has not been relabeled as a pass. Owner deployment manifest remains pending.

Independent review of `f1d61b8` found remaining read-side defects: bindings retained their own router paths but the page read a single router; planner uses `planner_model_router_db`; legacy multi-profile filters are an unordered candidate set, not a fallback chain. A real copied-state model-page read reproduced an uncaught missing policy. These are being fixed before release. The successful wheel and 13-step rehearsal at that checkpoint do not supersede this runtime finding. Affected owner manifest stays pending.

The second frozen full run at `f1d61b8` passed **6,243 tests / 514.522s, 1 skip** (`/tmp/dalton-wave4-final-full-tests.log`). Subsequent bound-router repair `5ad6180` passed 142 integrated focused tests / 25.840s, 1 skip; the previously failing copied-state model page now returns 32 purpose rows, 26 configured, zero binding errors. The full result remains bound to `f1d61b8`, not to later repairs.

Copied-state diagnosis also found a rehearsal gap: extraction model config retained the live absolute router path while newly installed role configs used the temporary router. All direct root reads used read-only router handles and child dispatch was stubbed; no live write/model call occurred. Normal install runs extraction setup first and does not create that mismatch. The rehearsal is being strengthened to rebind and validate internal model-config paths before the final freeze.

## Release checkpoint accepted

`79e0ef7ca9348484bc549cdc36b3846bd404f7b3` passed **6,248 tests / 519.224s, 1 skip**. The same unchanged checkout passed 430 Python/SQL + 3 HTML byte comparisons, JavaScript syntax, the 13-step current-copy rehearsal (all 12 model configs internally confined), and the copied Cockpit model page. Original main's external DeepSeek patch hash remains unchanged. [Final report](continuous-wave4-release-2026-09-10.md) is now the authoritative acceptance record.

Next step is owner deployment and mission v14 signing using the prepared private packet, followed by installed-runtime and actual product acceptance. The packet preserves exact source/wheel/test/rehearsal bindings and includes a read-only postdeploy checker. No live deployment, signature, metadata declaration or paid model call has run. This checkpoint completes predeployment development and acceptance, not live product acceptance.

## Live deployment recovery

Owner deployment exposed an actual installer-order gap, fixed in `67373c0` with 11 focused tests on both development and installed Python. Recovery deployment completed catalog/roles/startup; an unmanaged preexisting controller was then identified and stopped, and fresh health passed. Installed 433 source files match the accepted runtime. Mission remains v13: only the owner's v14 signature is pending before product acceptance. [Recovery details and next step](wave4-live-deployment-recovery-2026-09-10.md).
