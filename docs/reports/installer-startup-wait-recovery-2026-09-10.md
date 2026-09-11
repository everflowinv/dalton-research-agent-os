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

The installer now waits up to 180 seconds of wall-clock time by default. It
loads zsh's datetime module and reads `EPOCHREALTIME` into task-specific
deadline variables without declaring or resetting the shell's `SECONDS` state.
Health probe execution time counts against that deadline and the last sleep is
clipped to its remaining duration. Operators can set
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

Ran 10 tests in 7.004s — OK
```


Full R4 acceptance found one compatibility regression in 6,809 tests: the
existing installer contract parses the script with both bash and zsh, while
zsh's `<->` numeric pattern is not accepted by bash's parser. The startup
validation now uses shared shell digit syntax, refuses overlong values before
arithmetic, and normalizes leading zeros as decimal. The actual installer
remains a zsh script. The focused installer and existing seed/parser suite
passes 17 tests, including zero, out-of-range, expression, huge-integer and
leading-zero cases. The failed R4 evidence is retained; no R4 deployment was
performed, and a new exact freeze will receive full acceptance.
