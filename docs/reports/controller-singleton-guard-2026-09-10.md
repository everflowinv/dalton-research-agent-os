# Controller singleton guard — 2026-09-10

## Runtime failure

Recovery found an unmanaged controller from September 9 and the newly loaded
LaunchAgent controller writing the same live heartbeat. `daltond` had no
single-instance ownership. The installer only stopped the LaunchAgent label,
while `launch_drain.py` intentionally inspects lane children rather than
controllers.

## Change

`ControllerOwnership` derives an owner-only lock beside the configured
heartbeat, takes a non-blocking advisory lock, and holds its file descriptor
for the complete controller lifetime. Both daemon mode and `--once` acquire it
before `ServiceConfig`, `DaltonService`, or any tick/backup side effect is
constructed.

The lock alone cannot detect a controller installed before this guard existed.
While holding it, startup therefore scans the public process list for either a
`daltond --config PATH` entry or `python -m dalton_core.service --config PATH`
whose final argument is the exact resolved configuration. It excludes itself,
other config paths, and unrelated commands that merely contain similar text.
A conflict returns a refusal and PID list; it never signals or kills a process.

The standalone stdlib-only pre-install interface is:

```sh
"$python_source" "$repo_root/src/dalton_core/controller_singleton.py" \
  --check --config "$config_path"
```

It exits 0 with a JSON `clear` result, exits 1 for a matching resident
controller, and exits 2 when it cannot validate the config or process scan.
This check is read-only and does not create the lock file, so the installer can
run it from new source with the old environment before pip, bootstrap, or
backup. A first install whose `service.json` does not exist is valid: the check
still scans the exact future path and creates neither its directory nor a lock.
An existing malformed config remains an error. Installer wiring is owned by a
separate slice.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_controller_singleton tests.test_service`
passed **51 tests**. Coverage uses real subprocesses to prove lock contention
and release, and a temporary legacy `dalton_core.service` module with no lock
to prove resident detection, including the `--config PATH --once` form. It also
covers different-config and fake `echo` false positives, first-install and
read-only CLI behavior, and verifies that a conflict constructs no
`DaltonService` in `--once` mode.

Both modified modules passed `py_compile`, and `git diff --check` passed. No
live process, file, service, or configuration was changed.
