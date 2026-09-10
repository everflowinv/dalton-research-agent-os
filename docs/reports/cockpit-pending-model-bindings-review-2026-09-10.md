# Cockpit pending model bindings review — 2026-09-10

This review traced the six `pending_unconfigured` purposes from the Cockpit
registry to their installed lane launch arguments and final call sites. It was
read-only; no runtime configuration or source was changed during the review.

## UI binding omissions

Three purposes already have a persistent runtime selection. Their mission
lanes always derive the launch argument from
`initial-screen-model-config.json`, but `purpose_policy_bindings` labels them
as an unknown dynamic launch argument:

- `model_spec`: `mission_model_spec_lane.argv_fragment` passes
  `initial-screen-model-config.json` to `CompanyModelSpecLauncher`, and
  `company_model_cli.run_model_spec` constructs the model from that file.
- `debate_map`: `mission_debate_map_lane.argv_fragment` passes the same file to
  `DebateMapLauncher`, and `debate_map_cli.run` reads it for the draft call.
- `conviction_call`: `mission_conviction_lane.argv_fragment` passes the same
  file to `ConvictionCallLauncher`, and `conviction_call_cli.run` reads it for
  the proposal draft.

The minimum repair is to bind these three purposes to
`initial-screen-model-config.json` in `PURPOSE_MODEL_CONFIGS` and remove them
from the dynamic-unconfigured fallback. This makes the page display the policy
the installed controller actually launches and lets the existing atomic
selection publisher repoint the shared config. The page should mark that one
shared file controls all affected purposes.

## Genuine missing verifier binding

`industry_framework_verifier` is a real model call. The launcher only passes
`dossier-verifier-model-config.json`, while the current paired dossier install
uses `company-dossier-verifier-model-config.json`. The Cockpit registry mirrors
the old-only launcher and therefore reports the currently absent file. This is
a runtime wiring gap, not merely a display gap: with a producer configured and
no legacy verifier file, the framework cannot obtain its required independent
verification.

The minimum repair is to make the framework launcher argv prefer
`company-dossier-verifier-model-config.json` and fall back to the legacy
`dossier-verifier-model-config.json`, matching the dossier/deep-insight
consumer rule already represented in the registry. Update the registry to the
same ordered pair and test installed argv plus the verifier's actual policy
read.

## Truthful unconfigured or non-model stages

- `quality_verifier` is genuinely unconfigured in the deployed CLI path.
  `research_quality_cli` constructs only one judge model and calls
  `score_artefact(..., model=model)` without a `verifier_model`. The verifier
  API is reachable to an explicit Python caller, but no persistent installed
  config supplies it. Keep the row unconfigured until the CLI/launcher gains a
  distinct verifier argument and independent config; do not infer the judge's
  initial-screen pin.
- `street_estimate` does not make a model call in the current mission lane.
  `mission_consensus_lane` explicitly documents deterministic extraction and
  calls `street_estimate_extraction.extract(context)`, which is deterministic.
  The registered purpose is therefore not a missing runtime selector. The
  Cockpit should display this stage as deterministic/no model rather than as a
  pending model configuration.

Of the six pending rows, three are UI binding omissions, one is a real verifier
wiring gap, one is a genuinely absent optional verifier, and one is not a
model-consuming runtime stage.
