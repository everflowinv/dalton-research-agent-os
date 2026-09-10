# Controlled host-failure redrive — 2026-09-10

This slice adds an explicit operator recovery for historical model work whose
immutable formal result contains exactly `HOST_COMPLETION_FAILED` or
`HOST_CONTROL_PROOF_MISSING`. It does not clear or rewrite Scheduler records.

`dalton-controlled-redrive` prepares a read-only candidate bound to the old
WorkOrder and result hashes, exact mission version/hash, admission and
settlement hashes, full original reservation, and the exact managed OpenClaw
2026.9.3 package and completion-bundle SHA-256 values. The installed bundle is
accepted only when the complete controlled-transport patch has each strict
post-patch anchor exactly once. Apply requires the reviewed candidate hash, reconstructs
the candidate from current authorities, first appends the conservative cost
correction, then appends one immutable recovery authorization. Repeating apply
is idempotent; another authorization for the old work is refused.

`CockpitModel.call` consults this authority only after finding the exact failed
formal result. A matching record derives a stable
`:operator-recovery:<record-hash>` request identity and recursively enters the
ordinary route, mission budget and accounting path. There is no automatic
redrive and no eligibility for successful work, routing failures, arbitrary
provider errors, changed missions, changed formal results, missing cost
authority, or an unrecognized installed repair.

Validation:

```text
PYTHONPATH=src python3 -m unittest tests.test_controlled_failure_redrive tests.test_cockpit_model_fallback tests.test_scheduler tests.test_thesis_impact_budget
```

Result: 61 tests passed. The candidate-only test compares Scheduler and budget
database bytes plus directory sidecars before and after prepare; apply/replay preserve the original WorkOrder
authority bytes and append one correction and one recovery record. No live
state, network, model call, signature or deployment was used.
