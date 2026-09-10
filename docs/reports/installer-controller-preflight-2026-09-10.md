# Installer controller preflight — 2026-09-10

The public macOS installer now runs the new checkout's stdlib-only controller
check immediately after stopping the three managed controller jobs and before
lane drain, writer shutdown, virtual-environment changes, package installation,
or bootstrap:

```text
"$python_source" "$repo_root/src/dalton_core/controller_singleton.py" \
  --check --config "$config_path"
```

An unmanaged controller using the same service configuration makes the check
nonzero. The installer prints that the runtime was not upgraded and exits
before any later installer operation. The check does not stop or signal the
unmanaged process.

The shell behavioral tests execute the real installer fragment with isolated
command probes. They verify the managed controller probe precedes the singleton
check, the check uses `controller_singleton.py` from the new checkout with the
exact service config path, lane drain follows a successful check, and a blocked
check prevents drain and every later Python mutation represented by the test.

```text
python3 -m unittest tests.test_installer_controller_preflight
Ran 2 tests in 1.490s — OK
```

The private deployment wrapper must invoke the same command before creating
its live backup or calling this installer. That wrapper is outside this change
and remains root-owned. No live process, configuration, backup, or deployment
was touched during this work.
