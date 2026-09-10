# Wave 4 final2 activation rehearsal — 2026-09-10

## Scope

This was a **SIMULATION** using the frozen detached source commit
`f1d61b8ad7c0d9a94830cead535570d6b0a5e7a5` and a new temporary copy of the
current live Dalton state. It did not modify the frozen checkout or live root,
publish or sign live governance, deploy services, contact the model broker, or
make a paid model call.

Inputs were hash-bound:

- live source: `/Users/everflow/Library/Application Support/Dalton`
- output: `/tmp/dalton-wave4-final2-activation-20260910`
- OpenClaw catalog: `/Users/everflow/.openclaw/openclaw.json`, read only
- private mission v14 params SHA-256:
  `dd09fcdea9a0329b594efade2bd6ea3479788df992222beb62563b7fab353ebb`
- `deploy/phase9/activation-models-v1.simulation.json` SHA-256:
  `c4875b79c80cc05f7a238483200280c28cac2902aafcff5a9cd38e1acf8c8bec`
- simulation actor: `human:simulation-owner`
- simulated governance approvals: zero

The command omitted `--source-root`, as required; that option denotes source
data rather than the code checkout.

## Acceptance evidence

The rehearsal exited 0 and all **13/13 steps** passed. It copied 23 live-state
items (835 MB) through the read-only path, applied **67/67 schemas**, published
mission v14 only in the copy with 22 write grants and 10 checkpoints, installed
11 model configs in the copy, and rendered three temporary LaunchAgent plists.

One controller tick completed with **38 entries and 0 escaped paths**. The tick
ledger reported 35 lanes. Rows marked `launched` establish only that the
controller dispatched the lane to its refusing child stub. They are not model
calls, accepted evidence, or research artefacts.

The canonical report is
`/tmp/dalton-wave4-final2-activation-20260910/rehearsal-report.txt`, SHA-256
`ccaef6b05380ac02f791fba411fd6a760e8607d20984d1f20869f8a8cd54780e`.

## Remaining gates

- Lane switches remain **15/16**: `catalog_sync (P14-M2)` is unconfigured
  because `model-catalog-sync.json` was not installed.
- The current verifier phase pin names retired
  `profile:gemini-3-7-flash`; verifier routing will refuse with
  `profile_retired` until the owner repoints it to the verifier tier chain.
- Company-wiki seeds remain gated by the absent wiki index.
- Eight crowd-tool seeds remain gated by three unset executable settings.
- Two prior-research seeds remain gated because the rehearsal environment has
  no `DALTON_PRIOR_RESEARCH_DIR`.
- Guidepoint transcript narrowing, HKEX daily buyback tape, and two ROIC
  governance records remain deliberately unseeded under the current policy.

These are deployment inputs or owner decisions. The successful copy rehearsal
does not represent live activation or product acceptance.
