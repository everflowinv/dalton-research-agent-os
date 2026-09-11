# Writer stage checklist plan binding — 2026-09-11

The installed writer classified real source gaps as `not_planned`, while Cockpit correctly read the installed discovery plan files. `_mission_stage_driver()` mistakenly called `load_plan()` on `MissionSourceDiscoveryCoordinator`; that method belongs to a launcher. A broad exception handler swallowed the AttributeError for all three source coordinators and supplied an empty set of planned specs. Consequently the stage tick reported no discovery/acquisition needs and downstream checklist consumers saw blocked rather than actionable items.

The writer now uses each coordinator’s already validated `plan`, the same plan used by dispatch. Absent coordinators remain absent and uninstalled source specs remain unplanned. No source is connected or authorized by this correction.

Validation: actual writer socket RPC with a configured AlphaEngine coordinator and a mission verifies earnings-call `missing` and a generated discovery need; uninstalled annual-report sourcing does not become planned. Writer discovery and mission-stage suites: 32 tests passed. No live changes or provider calls.

Next: combine with stage inventory/read semantics, discovery shortfall cadence, acquisition stop directives, and terminal retryability fixes; run full same-freeze acceptance before deployment. Separately, actual SEC index attempts fail on existing immutable v1 rate-policy versus new governed quota (50 existing calls versus 200 current template); this is a source setup compatibility issue to investigate without overwriting prior authority or silently expanding its budget.
