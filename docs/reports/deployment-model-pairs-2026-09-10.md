# Dossier and earnings deployment model pairs

The macOS installer now accepts two explicit, opt-in model pairs:

- `DALTON_DOSSIER_MODEL_PROFILE` or `_TIER` with
  `DALTON_DOSSIER_VERIFIER_MODEL_PROFILE` or `_TIER`.
- `DALTON_EARNINGS_MODEL_PROFILE` or `_TIER` with
  `DALTON_EARNINGS_VERIFIER_MODEL_PROFILE` or `_TIER`.

Each pair must be complete and the two pins must differ. With neither variable
set, the installer writes no model configuration and preserves the prior dark
state. A half pair or identical pins stops installation with an explicit
error. Runtime model routing continues to enforce producer/verifier family
independence; verifier-tier policies carry the `verify` and `adjudicate`
independence capabilities.

The dossier now prefers independent `dossier-model-config.json` and
`company-dossier-verifier-model-config.json` routes, so configuring it cannot
change Initial Screen, debate, framework, or conviction routing. Existing
installations remain compatible: when the new files are absent the lane falls
back to the prior `initial-screen-model-config.json` and
`dossier-verifier-model-config.json` paths. The Deep Insight Gate uses the same
new dossier pair first and the same legacy fallback, so one installed pair
activates the dossier's downstream gate consistently. Earnings preview/calibration and
its verifier each have their own policy and the two existing earnings paths.

All four producer/verifier groups (event judgement, zero-base, dossier, and
earnings) are preflighted before the installer creates directories, updates the
venv, or stops services. The preflight rejects half pairs, identical pins, and
unknown tier names, so an invalid pair cannot write its first configuration.
Setting both profile and tier for either role is also rejected; the installer
never compares a concatenated pin that differs from the profile it would use.

The installer now seeds `p12a-dossier-policy-v1.json` into the runtime state
only when absent. It never overwrites the owner's installed dossier policy.
The existing tracking-policy seed retains the same non-overwrite behavior and
continues to supply the earnings policy path.

Tests perform the underlying setup against a temporary service/router, then
assert the exact `argv_fragment` paths consumed by the dossier and earnings
lanes. They also inspect the installed verifier policy, partial-pair lane
behavior, environment gating, the non-overwrite policy guard, and rehearsal
seed/switch inventory.

No model pair is enabled by this commit, and no live install, policy decision,
signing, or deployment was performed.
