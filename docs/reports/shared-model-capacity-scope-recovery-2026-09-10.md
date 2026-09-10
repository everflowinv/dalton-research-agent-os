# Shared model capacity scope recovery — 2026-09-10

The first fleet-capacity slice bound a workspace to one policy and aggregated usage by `policy_ref`. That made every other provider/credential fail admission, while replacing a policy could reset the same account's daily counters.

This repair keeps the signed 0.1 policy wire compatible and derives a stable scope from its explicit provider and credential-slot account. Reservations persist `scope_ref` and `account_ref`; daily cost/call and open-concurrency queries aggregate by scope across policy versions. An explicit active-head registry binds each scope to one exact policy ref/hash. Initial setup creates the first head, replacement uses a compare-and-swap `activate` operation, and runtime construction refuses stale policy bindings.

Workspace manifests may now carry `shared_model_capacity_bindings`, a closed list keyed uniquely by provider and credential slot. Each entry also binds the authority's scope/account and exact active policy. The legacy singular `shared_capacity` mapping remains accepted; the legacy and list forms cannot coexist. When a list is configured, a selected provider account without an exact binding is refused before broker transport. The workspace creation CLI accepts repeatable JSON binding files, so the runtime gate cannot exist only as an internal parser feature.

Transport/protocol uncertainty retains its conservative cost and concurrency reservation. The existing `OpenClawModelAdapter.replay` path reuses the exact workspace-scoped invocation, sends a broker `replayOnly` request, and reconciles the held reservation from the durable duplicate without initiating a model call. Unknown cost is never synthesized and undispatched reservations alone can expire automatically.

Zero daily calls, zero daily cost, and zero concurrency remain invalid rather than silently meaning unlimited/free. A future explicit free-account contract would need a separately versioned signed semantic; this patch does not infer it from zero.

Verification:

`PYTHONPATH=src python3 -m unittest tests.test_workspace tests.test_workspace_end_to_end tests.test_workspace_process tests.test_shared_capacity tests.test_openclaw_model_adapter`

Result: 77 tests passed, including two governed providers, missing-binding pre-transport refusal, cross-version same-day quota retention, stale-head refusal, exact replay reconciliation, workspace CLI/manifest validation, and immutable release workspace rehearsal.
