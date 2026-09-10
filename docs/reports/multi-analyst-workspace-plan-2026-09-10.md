# Multi-analyst workspace architecture plan — 2026-09-10

## Decision

Use one immutable, versioned Dalton code installation shared by all analysts and
one complete runtime namespace per workspace. A workspace owns its databases,
configuration, governance records, tokens, logs, spools, Unix sockets, HTTP
port, controller lock, and LaunchAgent labels. Sharing a Python environment that
is upgraded in place is unsafe: one analyst's install can replace modules while
another analyst's controller or child process is running. Install releases under
`runtime/releases/<wheel-sha256>/` and make each workspace manifest select an
exact release.

The workspace identifier is an explicit lowercase slug, validated once and
stored in a closed `workspace.json` record with a generated immutable UUID. It
must never be inferred from analyst display name. Suggested layout:

```
~/Library/Application Support/Dalton/
  runtime/releases/<wheel-sha256>/venv/
  workspaces/<slug>/config/service.json
  workspaces/<slug>/state/dalton-core/{core,scheduler,model-router,...}.sqlite
  workspaces/<slug>/state/dalton-core/run/{writer.sock,heartbeat.json,...}
  workspaces/<slug>/logs/*.log
  fleet/index.json                         # optional discovery metadata only
~/Library/LaunchAgents/space.lumos.dalton.<slug>.<role>.plist
```

`fleet/index.json` may list workspace UUID, slug, config path, release hash,
Cockpit endpoint, and lifecycle status. It is not a research, governance, or
budget authority. Each Cockpit continues to read only its configured Core.

## Current singleton inventory

| Boundary | Current implementation | Required workspace contract |
|---|---|---|
| Install root | `deploy/macos/install.sh` fixes state to `state/dalton-core`, config to `config/service.json`, logs to `~/Library/Logs/Dalton`, and runtime to one mutable `runtime/venv` | Add required `--workspace <slug>` (or `DALTON_WORKSPACE`) and derive every mutable path from the workspace root; select an immutable release venv |
| Bootstrap | `bootstrap.bootstrap` already accepts `state_dir` and `config_path`; all database, token, heartbeat, static-output, and writer-socket defaults derive from `state_dir` | Preserve this interface; add workspace identity/hash to generated config and refuse a config whose absolute paths escape its workspace root |
| Service config | `ServiceConfig` already requires absolute Core, scheduler, projection, router, catalog, heartbeat, and writer-socket paths; nested control/planner/review configs carry more absolute paths | Validate that all workspace-owned paths share the declared root. Explicit host-shared broker paths are the only allowlisted exception |
| Controller ownership | `controller_singleton.py` keys the lock and resident-process scan by exact config/heartbeat path | This is already suitable once config and heartbeat are unique. Include workspace UUID in lock contents and diagnostics; never scan/stop another config |
| LaunchAgents | `macos_launchagent.py` uses four fixed labels (`writer`, `controller`, `control`, `thesis-impact`) and fixed log filenames; `install.sh` stops and restarts those labels globally | Parameterize labels as `space.lumos.dalton.<slug>.<role>`, plist filenames, logs, working directory, and all arguments. Stop/drain/bootstrap only the selected workspace labels |
| Writer/children | Writer arguments and registered lane launchers consistently pass their selected `--state-dir`; lane tickets, summaries, connector catalogs, and `connector-spool` are relative to it | Keep that propagation and add a test that every registered launcher argv points only into its workspace. No shared ticket or spool directories |
| Cockpit | Control host/port and Tailscale `serve` are read from one service config | Require a unique loopback port per workspace; detect collision before any install mutation. Tailscale path/hostname publication needs an explicit per-workspace mapping rather than repeated replacement of one port mapping |
| Writer socket/tokens | `writer.sock` and `writer-tokens.json` derive from state; Cockpit and governance clients use configured paths | Unique automatically with workspace root. Tokens remain workspace-local and must not be copied between workspaces |
| Model router/scheduler | Both SQLite files are state-local. `CockpitModel` receives their absolute paths | Keep isolated. Identical policy refs in different databases are different workspace authorities and must be displayed with workspace UUID/database identity |
| Broker | Model and web-search broker sockets/auth keys are host paths supplied through model/service configuration; web-search paths are partly derived from the planner broker directory | Treat these as explicit host-shared services. A workspace owns no broker credential bytes; its config holds only the permitted slot refs and key/socket paths. Do not derive additional sockets by filename convention in the final contract |
| Connector governance and source limits | Governance files, connector reservation tables, mission budgets, pool spend, AlphaEngine counters, and raw spools are in each Core/state directory | Isolation is correct for approvals and research records. These ledgers cannot enforce an aggregate provider/account limit across workspaces |
| Installer seeds/setup | `install.sh` contains many seed-once paths and model setup calls rooted at the single state/config | Route every seed and setup through a `WorkspacePaths` object/CLI projection. Reinstall one workspace must preserve its overrides and never inspect or replace another workspace's files |
| Auxiliary scripts | Several operator scripts still document or default to `~/Library/Application Support/Dalton/state/dalton-core`; most runtime CLIs accept `--state-dir` | Production/operator entry points must require `--workspace` or explicit config. Legacy default may remain read-only compatibility but must be rejected for multi-workspace mutation |

## Shared vendor capacity

Mission budgets and `budget_pools.py` are intentionally scoped by mission and
stored in one workspace's Core/budget database. Two workspaces can therefore
each admit a call while their sum exceeds one provider account's daily or
concurrent limit. The model router's credential-slot checks establish that a
workspace is allowed to use a slot; they are not evidence of a host-global
reservation. The reviewed source contains no authority that atomically reserves
capacity across separate workspace databases. The external broker may apply its
own transport throttles, but Dalton currently cannot rely on or report that as a
global budget guarantee.

If aggregate enforcement is required, add one small host-owned
`VendorCapacityAuthority` beside the shared broker. Its key is
`(provider_account_ref, credential_slot_ref, UTC day)`, and its API is
`reserve(workspace_uuid, work_order_ref, maximum_cost, calls, expires_at)`,
`settle(reservation_ref, actual_cost, outcome)`, and `release_expired(now)`.
Every broker-bound path must acquire this reservation before transport, after
the workspace-local mission reservation. Failure must release/settle both in a
defined order. The authority stores opaque workspace/work refs and totals, not
research content or secrets. Until this exists, the fleet UI must show
workspace-local remaining budget and state explicitly that aggregate vendor
capacity is unknown.

Connector APIs have the same issue when workspaces share an account or IP quota.
Apply a global authority only to capabilities whose approved governance record
names a shared quota scope; public-source limits must not be guessed into one
global pool.

## Three implementation slices

### Slice 1 — workspace identity and path closure

Add `src/dalton_core/workspace.py` with `WorkspaceIdentity`, `WorkspacePaths`,
slug validation, closed-manifest hashing, and path containment checks. Extend
`dalton-bootstrap`, `ServiceConfig`, health, drain, and installer argument parsing
to consume it. Move installation to content-addressed release venvs while
retaining the existing single workspace as an explicit migration named
`default`. Do not move data implicitly; produce a dry-run migration manifest and
require an owner operation for the rename.

Acceptance: two bootstrap calls produce disjoint byte paths and UUIDs; malformed
or cross-root absolute paths fail before writes; the same release hash is reused
read-only; upgrading workspace A does not modify B's config, databases, or
selected release.

### Slice 2 — process, socket, port, and installer isolation

Parameterize `macos_launchagent.py` labels and log names with the workspace slug.
Refactor `install.sh` around the `WorkspacePaths` projection so stop, singleton
preflight, drain, pip/release selection, setup, plist rendering, Tailscale setup,
and health checks all address one workspace. Add a host-local port allocation
preflight with an explicit configured port; do not silently choose a new port.
Update controller diagnostics and Cockpit responses to include workspace UUID.

Acceptance: both writer/controller/control sets run concurrently, sockets and
ports are distinct, `controller_singleton` accepts both configs, and stopping,
draining, reinstalling, or failing installation of A leaves every B PID, plist,
socket, log, and file hash unchanged.

### Slice 3 — shared capacity and optional fleet discovery

Implement the host-owned capacity authority only after its provider/account
scope is approved. Integrate it at the final pre-transport boundary used by
model and connector adapters, with crash expiry and double-settlement
idempotency. Add a read-only fleet index writer to the installer and a fleet
reader that opens each workspace through its declared config. Fleet operations
must link to the selected Cockpit; they must not proxy writes across workspaces.

Acceptance: concurrent reservations from A and B cannot exceed the shared cap;
an expired A reservation becomes available to B; settlements are charged once;
a missing/unavailable shared authority fails closed only for governed shared
slots. With no global authority configured, both workspaces remain operational
and the UI reports aggregate capacity as unknown rather than summing local
balances.

## Two-workspace end-to-end acceptance

Create `analyst-a` and `analyst-b` from the same wheel hash with separate
missions, model selections, budgets, governance decisions, and Cockpit ports.
Run simultaneous deterministic stub lanes using the same company ref and the
same local record IDs. Verify each Cockpit displays only its own mission,
products, costs, jobs, and approvals; identical IDs in the other Core never
resolve. Confirm all SQLite/WAL files, raw artifacts, summaries, tokens, and logs
remain under their respective workspace roots.

Then run simultaneous broker-bound stub transports through one shared
credential slot. Verify local mission/pool admission in each workspace and the
host-global reservation as two separate decisions, with no paid network call.
Force a collision at the shared cap, a child crash, PID reuse, controller
restart, workspace-A reinstall, and port collision. Exactly one global
reservation may admit, stale reservations recover once, both local ledgers
retain accurate attribution, and no lifecycle action against A changes B.

This plan does not authorize a live migration, additional analyst, shared
credential, provider quota, Tailscale route, or fleet-wide write operation.
