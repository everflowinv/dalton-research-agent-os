# Workspace runtime admission — 2026-09-10

## Result

Workspace processes now fail closed when `DALTON_WORKSPACE_MANIFEST` and their
runtime paths do not identify the same workspace. The check runs before the
controller ownership lock, writer store/socket construction, bootstrap
directories, and lane per-run ticket/log creation. A service config carrying a
workspace binding also refuses to run when the environment binding is absent.
Legacy service configs remain unchanged when the environment variable is not
set.

The shared reader validates the manifest content hash and compares the exact
service binding (`workspace_id`, manifest path, manifest hash), config path,
state directory, core database, writer socket, and the other workspace-local
service database/heartbeat paths. Bootstrap additionally requires its explicit
manifest argument to equal the environment manifest.

`LaneChildLauncher` validates the inherited workspace at construction and the
generated child `--state-dir` before it makes a run directory or opens a log.
`validate_cli_state` is the common pre-write hook for source CLIs invoked
outside the governed launcher path.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_workspace_runtime tests.test_workspace tests.test_lane_child_launcher tests.test_service tests.test_p9a_writer_ops`

Result: 80 tests passed. The tests use two real workspace manifests and real
bootstraps. They cover cross-bound config/environment refusal without creating
the second workspace database, a tampered manifest, a missing environment for
a bound config, legacy compatibility, launcher construction against the wrong
state, and child argv rejection without a per-run directory or log.

## Boundary

The common launcher protects every registered lane child. A separately invoked
module must call `validate_cli_state` before it writes; converting every
standalone maintenance/debug CLI is outside this slice. Shared model capacity
governs the model adapter only; connector-wide host capacity remains
unimplemented and must not be inferred from workspace admission.
