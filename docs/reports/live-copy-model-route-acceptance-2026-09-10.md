# Live-copy model route acceptance — 2026-09-10

## Boundary

This was a read-only acceptance exercise. The live ModelRouter was opened with
SQLite `mode=ro` and backed up into `/tmp`; all catalog sync, production setup,
policy creation, and route decisions occurred only in that temporary copy. The
current OpenClaw configuration was read through the catalog projection that
copies public provider/model, limits, rate-card, family, capability, and
credential-slot identifiers. No credential value is present below. No adapter,
broker, model, or network call ran.

The live state currently contains three model config files. The reviewed
activation manifest expands this to the intended eleven roles. I used the same
`research_planner_setup.install` and `deliverable_model_setup.install` functions
as production, with every service path redirected into the temporary root.

## Catalog result

The public OpenClaw catalog contained 24 provider models and 23 broker profiles.
After sync, the copied router had 23 live broker profiles, seven retired
historical profiles, no missing, extra, or drifted live profile, and
`catalog_in_sync=true`. Running all eleven setup installs after catalog sync did
not reset the dynamic entries: a subsequent read-only status remained in sync.
This covers the first-setup/static-reset risk.

`profile:deepseek-v4-flash` correctly resolves to the current public route:

```text
provider=deepseek
model=deepseek-flash
credential_slot_ref=credential-slot:openclaw:deepseek
input_per_million_usd=0.15
output_per_million_usd=0.60
capabilities=research,verify,code,summarize,extract
family=unclassified:deepseek
```

The cheap claim-index policy selected that exact latest profile version with no
rejection reason. This confirms provider switching, the new alias, credential
slot, and price are usable by a non-independent producer route without making a
paid call.

## Eleven-role routing result

All eleven generated configs resolved through their newly installed policy:

- Event, zero-base, dossier, and earnings producers selected
  `openai/gpt-6-astra` (`openai-gpt-6`).
- Their four verifier configs selected
  `claude-cli-gateway/claude-fable-5-1` (`anthropic-claude-5`) when passed the
  actual producer family. Each had multiple eligible independent candidates.
- Planner and deliverable selected `openai/gpt-6-astra`.
- Claim index selected `deepseek/deepseek-flash` on the cheap tier.

These are router selections only. They prove config, policy, profile,
credential-slot, capability, context, output, budget, and family filters admit a
route. They do not claim that an adapter launch or model output succeeded.

## Remaining blocker

DeepSeek Flash is the only live broker profile with an unclassified family.
Passing its actual `unclassified:deepseek` family into a verifier route yields
zero eligible candidates and `model_family_not_independent`. This is the
required fail-closed behavior, but it means any flow that uses the cheap model
as producer and then requires independent model verification cannot complete.
The catalog marks this profile as requiring smoke calibration as well.

The recovery is concrete: publish explicit durable family metadata for
`profile:deepseek-v4-flash`, sync it as a new immutable profile version, and
then rerun the same producer-family verifier route. Capability metadata is
already present. Until the family is explicit, the cheap route is suitable only
for stages whose own contract does not require independent model-family
verification.

## Evidence

The disposable run produced 11 model configs and 12 immutable route decisions
(11 role checks plus the DeepSeek-to-verifier refusal). The post-install catalog
check was clean. No repository production code or tests were changed for this
read-only validation, and no live file was written.
