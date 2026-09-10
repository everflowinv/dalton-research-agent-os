# Owner activation scenario rehearsal — 2026-09-10

## Scope

This is a **SIMULATION** against a temporary backup of the current live Core.
It does not publish the mission, approve live governance, contact a broker,
call a model, or accept generated research as a product artefact.

The new `rehearse_activation_scenario.py` subclasses the existing deployment
rehearsal. A small empty hook in `Rehearsal` inserts the opt-in activation step
after bootstrap, all migrations, seeds, and catalog sync, and before plist
rendering. The ordinary rehearsal executes no extra step.

Inputs are explicit and hash bound. This run used owner mission candidate v14
at SHA-256 `dd09fcdea9a0329b594efade2bd6ea3479788df992222beb62563b7fab353ebb`
and the committed simulation model manifest at SHA-256
`350f73aead1c1f716ce5bbfa92227d1e82b3cf19a8d2d3db02ac137179f097db`.
The manifest installs four independent brain/verifier pairs, claim-index cheap,
planner brain, and deliverable brain through the production setup functions.
An optional repeatable governance filename list can simulate approval of exact
validated proposal records inside the temporary governance directory only;
no records were selected for this run.

## Result on a current live read-only copy

- 830 MB copied with SQLite backup readers opened in read-only mode.
- 67/67 schemas applied to the copy.
- Mission v14 published through the real `CoverageMissionAuthority` in the
  copy; 22 write grants and 10 checkpoints left no missing required grant.
- 11 model configuration files installed; their router and broker paths were
  checked to remain under the confined temporary root.
- Rendered writer argv includes the event, zero-base, dossier and earnings
  producer/verifier pairs plus claim-index; planner and deliverable configs are
  present for their existing consumers.
- One controller tick reported 38 entries and 0 escaped in 4.9 seconds. The
  refusing broker and existing child-launch stubs remained in force. A
  `launched` row therefore demonstrates dispatch wiring only, not a successful
  model call or an accepted research artefact.

## Remaining gates and observations

The model catalog sync refused because the concurrently edited broker catalog
reports that `profile:deepseek-v4-flash` changed route and requires a new
profile id. Consequently the catalog-sync switch was the sole missing modeled
lane switch (15/16 present). This is a real pre-deploy blocker to resolve in
the model catalog; the activation simulation continued collecting findings
because catalog sync is an existing non-fatal rehearsal step.

Environment-gated sources remain absent: company wiki (missing index), crowd
tools (three executable variables unset), and prior research (directory unset).
Guidepoint and several HK/ROIC records remain deliberately unseeded under the
existing install policy. The plist diff also shows the expected large argv
delta from the current installed service to this integrated code. These are
review inputs, not evidence that the mocked tick produced usable research.

No claim is made that a before/after hash of the actively used live files stayed
constant because another user session may update model configuration while the
simulation runs. Safety comes from read-only SQLite source connections, path
confinement, temporary destinations, and tests that use a static fixture to
assert source inputs are unchanged.
