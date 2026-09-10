# Cockpit model-selection role coverage — 2026-09-10

## Outcome

Cockpit model selection now repoints every model configuration the macOS
installer can create. The registry is explicit rather than dependent on which
CLI modules happened to be imported, and includes document extraction,
planner, initial screen, claim index, dossier producer/verifier, earnings
producer/verifier, event producer/verifier, zero-base producer/verifier, and
the legacy dossier verifier filename. Files that do not exist remain absent;
selecting a model does not activate a lane.

Independent verification is now a separately selectable stage for dossier,
deep insight, industry framework, earnings preview, earnings calibration,
event judgement, and thesis reflection. Each actual verifier call uses its
verifier purpose and verifier tier. Existing producer purpose names remain
unchanged. Zero-base already had separate producer and verifier purposes.

This slice covers calls that resolve a named purpose through the fallback
router, including the bounded planner's `plan` purpose. Document extraction
has no Cockpit purpose and resolves its configured cheap tier directly.
Thesis-impact assessment and verification read single-profile phase policies
from `service.json`. Repointing their configuration policy does not make those
legacy paths consume a purpose override; they require separate selector
integration and are not claimed as switched here.

## Persistence and failure behavior

Selection publishes append-only routing-policy versions and repoints every
present registered configuration. All router paths, policy pins, and the
requested selection validate before the first publication. Changed config
files are staged owner-only; a replacement failure restores every file already
replaced, preventing a mixed set of runtime policy pins. If that failure left
an unused policy version appended, an identical retry recognizes its exact
semantics, reuses it, and completes the repoint; unrelated policy movement
still fails as stale.

`research_planner_setup` no longer calls the static catalog seeder. Installation
must use profiles already registered by the live catalog sync and fails closed
when a requested profile is absent. This prevents a later setup or install run
from resurrecting a model the gateway catalog retired. Policy setup continues
to carry forward existing `purpose_overrides`, so an owner selection survives
an installer-created policy version.

## Cockpit visibility

The model page shows each live profile's declared family and capabilities. An
unclassified family is called out as unavailable for independent verification;
the UI does not infer a family from a model alias. The follow-up metadata
declaration path will use a Dalton-owned append-only declaration and will not
write fields into the OpenClaw plugin configuration.

## Validation

- 635 focused tests passed across model selection/setup and dossier, deep
  insight, industry, earnings, event, zero-base, and their mission lanes.
- Tests exercise two real role configuration files moving together, the next
  routed call reading the selected policy, rollback after a simulated second
  config replacement failure, complete installed-config registration, actual
  verifier-purpose calls, and refusal to seed a missing static profile.
- The model-selection unit suite (67 tests) additionally verifies a failed
  second file replacement restores both pins and an identical retry succeeds.
- `git diff --check` and Python compilation passed.

No live configuration, deployment, broker call, or model call was performed.
