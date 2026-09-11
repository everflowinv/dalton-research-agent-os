# OpenClaw catalog runtime sync — 2026-09-11

The production path is installed and active. `install.sh` performs an
append-only catalog synchronization before it installs purpose routing
policies, writes `state/dalton-core/model-catalog-sync.json`, and the generated
Writer LaunchAgent passes that exact file with `--model-catalog-config`. The
controller calls `dispatch_catalog_sync` before model-calling lanes. A
read-only check of the installed LaunchAgent and switch file found that exact
wiring, and the latest controller result reported the catalog lane as idle
because it had already run in the current hour. No live file or process was
changed.

OpenClaw 2026.9.3 treats changes below `models` as hot configuration changes
and changes below `plugins` as `reloadPlugins`; its default reload mode is
`hybrid` with a 300 ms debounce. The Dalton broker itself takes an immutable
`api.pluginConfig` snapshot when that plugin service starts. Thus the host's
plugin reload is the operation that replaces the broker snapshot; Dalton does
not mutate or partially refresh a running broker object.

Dalton already reread the OpenClaw JSON on every hourly reconciliation and
Cockpit reread both the Router and OpenClaw JSON on every model-page request.
One runtime gap remained: the resident Writer remembered only the wall-clock
hour. If OpenClaw hot-reloaded a changed route, price, context window, or output
limit later in that same hour, the next Dalton ticks returned `idle` until the
hour changed. During that interval Cockpit could show the file-side drift, but
the Router still selected the prior immutable profile version.

The lane now derives a secret-free digest from the normalized provider and
broker public catalog. Within an already-processed hour it compares that digest
on each controller tick and reconciles immediately when it changes. Credential
changes do not alter the digest. The hourly pass remains, so time-based
availability and provider-control expiry are still reevaluated without a file
change. A route rename keeps the stable `profile:` ID, appends a new profile
version pointing at the new provider/model route, and preserves the exact old
profile row for historical route replay.

Focused tests exercise install ordering and switch wiring, startup and hourly
dispatch, same-hour route hot reload through the real lane and ModelRouter,
old-row byte preservation, add/update/retire/revive behavior, read-only drift
reporting, duplicate-key and secret exclusion, and the broker's full protocol
suite. They make no host, network, or model call.
