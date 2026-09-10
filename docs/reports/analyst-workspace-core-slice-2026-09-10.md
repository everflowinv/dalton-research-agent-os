# Analyst workspace core slice — 2026-09-10

This slice adds the deterministic identity and path boundary required before
multiple analyst runtimes can be installed. It does not install, migrate, start,
or stop a live workspace.

`dalton-workspace create` writes a closed, hash-bound `workspace.json` beneath
`<host-root>/workspaces/<slug>`. A canonical UUID separates workspace identity
from its display slug. State, service config, logs, connector spool, writer
socket, and Cockpit port are explicit. The selected code release is a SHA-256
reference and an installed directory declared read-only shared. Other host paths
must be enumerated in `shared_readonly_paths` before a workspace-bound service
config may use them.

An optional `shared_capacity` binding names only a database directly beneath
the host's `fleet-capacity` directory plus an exact policy ref/hash. It grants no
policy and creates no database. Its absence means aggregate vendor capacity is
unknown; workspace-local mission budgets do not claim global safety.

`bootstrap(..., workspace_manifest=...)` validates the manifest and exact state
and config paths before creating directories or databases. Generated
`service.json` stores only workspace UUID, manifest path, and manifest hash.
`ServiceConfig.from_file` reopens the manifest, checks all primary database and
socket paths, and rejects known absolute path fields outside the workspace or
declared read-only roots. Legacy service configs without the optional workspace
binding retain their byte shape and behavior.

The CLI also provides `show`, `validate`, and `dry-run-migrate`. The migration
command emits a stable proposed identity and action list with
`writes_performed:false`; it does not copy or move legacy data.

Validation covered two real temporary bootstraps with distinct databases,
tokens, sockets, configs, ports, and UUIDs; repeat bootstrap preserved token
bytes. Duplicate IDs/slugs/roots/ports, symlink escape, manifest tampering,
service path substitution, undeclared external paths, and invalid shared
capacity roots fail closed. The existing exact-config resident controller scan
continues to distinguish the two workspaces.

Focused command:

```
PYTHONPATH=src python3 -m unittest tests.test_workspace tests.test_service \
  tests.test_extraction_window_settings tests.test_mission_stage
```

Result: 88 tests passed. Slice 2 should consume
`load_workspace_manifest(path) -> WorkspacePaths`, pass the manifest path to all
four LaunchAgents through `DALTON_WORKSPACE_MANIFEST`, and namespace labels and
logs without changing the legacy no-manifest path.
