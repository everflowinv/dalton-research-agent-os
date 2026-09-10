# Zero-base route recovery

## Problem

A zero-base producer or verifier for which Cockpit selected no route raised a
generic `CockpitModelError`. The review converted every such error to
`lane_status=refused`; the lane failure ledger therefore recorded
`lane_content_refusal`, a terminal decision about the company's content even
though no model had examined it. The review item identity also omitted the
producer/verifier model configuration, so repairing a Cockpit selection did
not supersede that terminal hold.

## Change

- Cockpit now exposes `CockpitModelRouteUnavailable`. Fresh route selection
  failures and replayed formal `MODEL_ROUTE_REJECTED` results produce this
  typed error. `lane_status_for` maps it to `model_unavailable`, which the
  existing lane classifier treats as a recoverable dependency.
- `ZeroBaseReviewLauncher.configuration_signature()` hashes the exact producer
  configuration, verifier configuration, tracking policy, and packaged
  verifier provider contract used by the child.
- Review item, batch, and ticket identities bind that signature. The launcher
  rejects a configuration that changes between selection and spawn. Settlement
  uses the signature persisted on the ticket, so an old child cannot poison a
  repaired configuration's item key.
- Existing company input hashes remain part of the identity. Genuine output
  validation refusals remain terminal. The persistent dependency ledger still
  permits only its existing bounded probe cadence, while a changed
  configuration retires only obsolete holds for that company.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_mission_zero_base_lane tests.test_zero_base_failure_recovery tests.test_budget_pools tests.test_zero_base_review`

119 tests passed. Coverage includes route-unavailable classification, bounded
probe and restart behavior, configuration repair, old-child settlement under
its launch signature, content-terminal behavior, config-signature changes,
pool accounting, and the real zero-base review parser path.

No live state, historical failure ledger, route selection, mission, or model
was changed. Historical holds become obsolete only when a deployed model,
policy, or provider-contract configuration actually changes.
