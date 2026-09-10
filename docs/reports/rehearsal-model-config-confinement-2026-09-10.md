# Rehearsal model-config confinement — 2026-09-10

## Problem

The deployment rehearsal rewrote `service.json` but copied existing
`*-model-config.json` files byte-for-byte. A copied extraction config could
therefore retain live router, budget, socket, and broker-key paths. The bounded
planner confinement check inspected only `service.json`; a controller tick
could report zero escaped lanes while a model config still named live state.

The final2 activation run exposed this as a cross-database read: Cockpit opened
the live router named by the copied extraction config and could not resolve a
deliverable policy correctly installed in the temporary router.

## Change

The existing path-replacement map now rewrites every copied runtime model
configuration immediately after `service.json`, before bootstrap, migration,
writer startup, or a controller tick. OpenClaw broker paths are redirected to
the refusing stub directory and Dalton state paths are redirected to the
temporary root. The rewrite changes path strings only and never reads or copies
credential contents.

The fatal confinement step now parses every runtime model config and refuses
the rehearsal if any absolute path remains outside the temporary root. The
activation extension repeats this validation after installing its eleven role
configs, so both preserved and newly generated configurations are checked.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_rehearse_deploy tests.test_activation_scenario_rehearsal`
passed **106 tests**.

The new coverage verifies that a copied extraction config's router and broker
key references move under the temporary root while the source bytes remain
unchanged. A foreign broker path makes confinement fail before `confined` can
be set, which prevents the writer or tick from being constructed.

No live files, credentials, routing policies, or services were modified.
