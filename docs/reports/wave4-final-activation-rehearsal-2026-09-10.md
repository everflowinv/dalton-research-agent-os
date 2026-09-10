# Wave 4 final activation rehearsal — 2026-09-10

## Scope and inputs

This was a **SIMULATION** against a new temporary, read-only copy of the current
live Dalton state. The source code was the frozen detached commit
`61f56759c5e249be150623844df3ee4550a5ee65`. The run did not write to the live
root, publish a live mission, approve live governance, contact the model broker,
or make a model call.

The exact inputs were:

- live source: `/Users/everflow/Library/Application Support/Dalton`
- temporary output: `/tmp/dalton-wave4-final-activation-20260910`
- OpenClaw catalog: `/Users/everflow/.openclaw/openclaw.json`
- mission candidate: private mission v14 params, SHA-256
  `dd09fcdea9a0329b594efade2bd6ea3479788df992222beb62563b7fab353ebb`
- simulation model manifest: `deploy/phase9/activation-models-v1.simulation.json`,
  SHA-256 `c4875b79c80cc05f7a238483200280c28cac2902aafcff5a9cd38e1acf8c8bec`
- simulation actor: `human:simulation-owner`
- simulated governance approvals: none

The command deliberately omitted `--source-root`; that option denotes a source
data root, not the frozen code checkout.

## Result

The command exited 0. All **13/13** rehearsal steps passed:

- 23 live-state items (834 MB) were copied through the read-only rehearsal path.
- All **67/67** schemas applied to the copy.
- Mission v14 was published only in the copy with all 22 write grants and all
  10 checkpoints present.
- Eleven model configuration files were installed in the copy, with zero paid
  calls and zero simulated governance approvals.
- Three LaunchAgent plists rendered under the temporary root.
- One real controller tick completed with **38 entries and 0 escaped paths**.
  The tick ledger itself reported 35 lanes.

The canonical text report is
`/tmp/dalton-wave4-final-activation-20260910/rehearsal-report.txt` (SHA-256
`e2c63a44b1699b7aeae021d16f8e6ae3956276216c00b2fc169c18e5c107ab45`).

Rows shown as `launched` prove that the controller selected and dispatched the
configured lane against its refusing child stub. They do **not** prove a model
call, accepted evidence, or a product research artefact.

## Missing gates and deployment decisions

The run remains a rehearsal rather than deployment acceptance because these
gates are open:

- Lane switches are **15/16**. `catalog_sync (P14-M2)` is unconfigured because
  `model-catalog-sync.json` was not installed.
- The existing verifier phase pin
  `model-routing-policy-version:dalton-openclaw-verifier:1` names retired
  `profile:gemini-3-7-flash`. A live verifier call will refuse with
  `profile_retired` until the owner repoints it to the verifier tier chain.
- Company wiki governance seeds remain gated because the configured wiki index
  is absent.
- Eight crowd-tool seeds remain gated because the three required executable
  settings are unset.
- The rehearsal reports the two prior-research seeds gated because
  `DALTON_PRIOR_RESEARCH_DIR` is unset in the current deployment environment.
- Guidepoint transcript narrowing, HKEX daily buyback tape, and two ROIC records
  remain deliberately unseeded under the current install policy.

The simulation made no claim that launched child stubs produced artefacts and
did not activate or mutate the live installation.
