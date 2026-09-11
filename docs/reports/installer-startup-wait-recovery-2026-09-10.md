# Installer startup wait recovery — 2026-09-10

The R3 installation completed its package and configuration work, but the
installer's fixed 15 × 2 second health loop expired while the controller was
still initializing the large live Core. The same controller reached `running`
after about 85 seconds. No live process or configuration was changed during
this repair.

`DaltonService.start()` deliberately writes a `starting` heartbeat before the
first `run_once()`. That first tick sweeps scheduler leases, polls services,
runs backup work, and builds the initial projection before it writes a
`running` or `degraded` heartbeat. `dalton-health` correctly rejects the
intermediate heartbeat; the defect was only the installer's shorter deadline.

The installer now waits up to 180 seconds by default. Operators can set
`DALTON_STARTUP_TIMEOUT_SECONDS` to an integer from 1 through 1800. The loop is
bounded and continues to call `dalton-health --max-age-seconds 45`; `starting`
never counts as success. On timeout, a final health invocation preserves the
real diagnostic and nonzero exit status.

Focused validation covered late success, bounded persistent failure, invalid
configuration before health execution, the default value, controller
preflight, and catalog-before-extraction ordering:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_installer_startup_wait \
  tests.test_installer_controller_preflight \
  tests.test_installer_model_catalog_order

Ran 9 tests in 4.291s — OK
```
