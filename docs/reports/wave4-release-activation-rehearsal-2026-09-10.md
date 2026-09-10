# Wave 4 release activation rehearsal — 2026-09-10

## Scope

This was a **SIMULATION** from frozen detached commit
`79e0ef7ca9348484bc549cdc36b3846bd404f7b3` into the new temporary root
`/tmp/dalton-wave4-release-activation-20260910`. It read the current live
Dalton root and OpenClaw catalog, but did not modify the frozen checkout or
live files, sign or publish live governance, deploy services, or make a model
call. `--source-root` was deliberately omitted.

Inputs retained the accepted hashes:

- private mission v14 params:
  `dd09fcdea9a0329b594efade2bd6ea3479788df992222beb62563b7fab353ebb`
- simulation model manifest:
  `c4875b79c80cc05f7a238483200280c28cac2902aafcff5a9cd38e1acf8c8bec`
- simulation actor: `human:simulation-owner`

## Rehearsal result

The process exited 0 and all **13/13 steps** passed. It copied 23 items (836
MB), applied **67/67 schemas**, published mission v14 only in the copy with 22
write grants and 10 checkpoints, rendered three temporary LaunchAgent plists,
and completed one controller tick with **38 entries and 0 escaped paths**.

Three copied model configs were rewritten before any exercise and passed the
fatal confinement gate. After activation installed its eleven role configs,
the repeated check found **12/12 runtime model configs confined**: every
absolute router, budget, socket, key, and other path was under the temporary
root. No credential contents were copied by this check.

The canonical rehearsal report is
`/tmp/dalton-wave4-release-activation-20260910/rehearsal-report.txt`, SHA-256
`1b850e600ef5a229b9d3a895fbcab836d505c00430f7f0cad00974843015e6d4`.

## Copied model-page read

A separate read used the copied Core, configs, and router while placing its
scratch scheduler and journal under
`/tmp/dalton-wave4-release-activation-20260910/model-page-read`, outside the
canonical copied state directory. `CockpitPlane.models()` returned
`available=true` without a missing-policy or cross-router exception.

It reported **32 purposes: 26 configured and 6 unconfigured**. The configured
rows resolved their own policy and chain, including extraction, planner,
dossier producer/verifier, event/reflection, earnings producer/verifier,
zero-base producer/verifier, agenda, and thesis-impact assessment/verifier.
Conviction call, debate map, model spec, quality verifier, industry-framework
verifier, and street estimate were the dynamic or absent bindings reported as unconfigured rather than
being assigned a different role's policy.

The per-purpose evidence is
`/tmp/dalton-wave4-release-activation-20260910/model-page-read-summary.json`,
SHA-256
`25ec0fb36f7b578233241740c78940d54a2464c14bc04e7e2be234faf50269e3`.

## Remaining gates

This successful rehearsal is not live release acceptance. Lane switches remain
**15/16** because `model-catalog-sync.json` is absent. The current thesis-impact
verifier pin still names retired `profile:gemini-3-7-flash` and will refuse
with `profile_retired` until the owner repoints it. Company-wiki, crowd-tool,
and prior-research environment gates also remain closed, and four governance
records remain deliberately unseeded.

Rows marked `launched` demonstrate dispatch to refusing child stubs only; they
are not evidence of model calls or accepted research artefacts.
