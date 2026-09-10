# Shared connector capacity — 2026-09-10

## Implemented boundary

Workspace manifests may declare zero or more `shared_connector_capacity`
bindings. Each binding pins an owner-created SQLite authority, policy ref, and
policy hash. Policies use a closed schema with exact connector, capability,
credential-slot set, and provider-account scope. Limits cover calls, cost, and
concurrency over an explicit rolling window; an AlphaEngine owner policy can
therefore use 86,400 seconds without resetting at UTC midnight.

`ConnectorTransportExecutor` is the final governed adapter boundary shared by
Gemini web search, AlphaEngine search/acquisition, SEC connector flows, and
Guidepoint. It now validates every declared policy, selects at most one exact
scope, takes the local connector quota first, then atomically reserves host
capacity before opening transport. The shared identity includes workspace UUID,
connector invocation, and physical attempt. It marks dispatch immediately
before adapter invocation and settles measured `provider_usage.cost_micros`
when supplied. Unknown/timeout and crash recovery retain maximum cost and the
concurrency slot; only undispatched reservations expire or release. Replay
reuses the same reservation.

Policy revisions aggregate on the stable connector/capability/credential/account
scope hash, so changing a policy ref does not reset spend or call counts.
Unrelated connectors with no matching binding continue to run, while overall
fleet capacity for them remains unknown. Any declared but unreadable or
hash-mismatched policy fails before external transport.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_shared_connector_capacity tests.test_connector_transport_executor tests.test_workspace tests.test_workspace_runtime tests.test_live_mcp_connector tests.test_mcp_managed_shadow`

Result: 66 tests passed. Coverage includes two-workspace contention, spawned
two-process atomic admission, duplicate replay, pre-transport missing-policy
refusal, dispatch-crash conservative recovery, rolling 24-hour behavior across
a date boundary, and stable-scope accounting across policy versions.

## Explicit remaining boundaries

Direct `PublicHttpTransport` users and `bounded_probe_executor` do not traverse
`ConnectorTransportExecutor`; they do not claim shared fleet enforcement.
Connector policy `provider_account_ref` is owner-bound together with the exact
credential-slot set because the current ConnectorProfile has no separate
provider-account authority field. Public credential-free profiles bind an
explicit empty slot set. Connector-wide activation still requires owner-created
policies and manifest bindings; this change creates or approves none.

## Account and recovery review correction

The unactivated connector policy is now schema 0.2: one explicit `quota_scope_ref` and `provider_account_ref` owns a list of exact connector/capability/credential scopes. Search and document retrieval spend the same account window; operation names cannot create separate copies of its cap. Policy versions extend an explicit active head while all reservations retain their original policy hash. Stale policies cannot reserve or dispatch new work; their existing dispatched attempts can still settle.

Reservations validate future expiry and positive physical attempt/cost, enforce the per-call cap, and refuse expired/released attempts with an explicit fresh-attempt requirement. Missing observations retain uncertain completion/concurrency. Recovery scans all bindings before mutation, resolves one workspace-bound reservation and exact historical policy, and does not count the same shared database twice when multiple account policies live there. Wrong-policy mutation is refused.

Root verification: 28 connector recovery/Cockpit/fleet tests passed. Direct-transport bypass and the expired pre-journal retry convergence remain under independent review; no live capacity authority was created.
