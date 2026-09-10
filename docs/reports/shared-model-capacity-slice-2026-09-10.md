# Shared model capacity slice — 2026-09-10

This slice implements the optional host-level model capacity binding declared by
an analyst workspace. It does not create or approve a live policy and does not
activate any workspace.

`SharedCapacityAuthority` opens an existing owner-only SQLite authority and
requires the manifest's exact approved policy ref and content hash. A policy has
one explicit provider and credential slot plus positive daily-call, daily-cost,
and concurrency caps. There are no default policies, inferred account scopes, or
automatic approvals. The managed database path is confined by the workspace
manifest to `<host-root>/fleet-capacity/*.sqlite`.

Reservations use `BEGIN IMMEDIATE` and are idempotent on workspace UUID plus
model invocation. Calls and maximum finite cost are admitted atomically across
processes. An undispatched reservation may expire; a dispatched reservation
never expires automatically and retains its maximum cost until an exact replay
or reconciliation supplies provider cost. Settlement is idempotent, and an
exact replay may replace a conservative unknown settlement with measured cost.

When `DALTON_WORKSPACE_MANIFEST` names a workspace with `shared_capacity`, the
OpenClaw adapter adds workspace UUID to invocation identity, reserves after all
local route/request checks, marks dispatch immediately before `_exchange`, and
settles reported cost afterward. Missing database/policy, wrong provider/slot,
or exhausted capacity fails before broker transport. Transport, timeout, and
protocol ambiguity retains the full reservation and its concurrency slot until exact completion is known. A timeout cannot free a slot while the provider may still be running; an undispatched reservation cannot dispatch after expiry. A legacy process
without a workspace manifest, or a workspace with no shared-capacity binding,
keeps its existing behavior.

Tests exercise real two-process contention for one slot, cross-workspace
invocation identity, daily/concurrency isolation, idempotent reservation and
settlement, expiry of only undispatched work, conservative transport failure,
and a missing-policy refusal whose mocked transport must not be reached.

Focused validation:

```
PYTHONPATH=src python3 -m unittest tests.test_shared_capacity \
  tests.test_workspace tests.test_openclaw_model_adapter tests.test_service
```

Result: 103 tests passed.

This slice governs only model calls passing through `OpenClawModelAdapter`.
Connector transports still use workspace-local quota reservations and are not
globally enforced. The capacity database also needs an explicit owner workflow
for installing signed/approved policies and reconciling a dispatched call that
can never be replayed; neither action is inferred here.
