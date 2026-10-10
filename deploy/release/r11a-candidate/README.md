# R11a operations candidate

This directory prepares a review packet for source commit
`424c22529d5317c4c5c9dc8ba88f37299cea3f49`. It does not contain an accepted
release manifest and none of its commands mutate live state during staging.

`inputs.template.json` is an intentionally incomplete public protocol example. The original machine-bound candidate inputs and provider snapshot are privately archived; use a new reviewed input file for any actual operation. The historical tooling remains because its CAS, rollback and protected-state tests still exercise it, not because it is the current deployment plan.

Run the inert template check or prepare a new packet:

```sh
python3 scripts/stage_r11a_ops_candidate.py \
  --inputs deploy/release/r11a-candidate/inputs.template.json \
  --verify-inert-template

python3 scripts/stage_r11a_ops_candidate.py \
  --inputs <reviewed-input.json> \
  --output <new-review-packet-directory>
```

The staged manifest remains `staged_pending_owner_acceptance`.
`execute_r11a_stopped_window_candidate.py` performs a read-only preflight by
default. Execution requires both `--execute` and the exact staged manifest hash.
It records a full private log, creates a fresh rollback snapshot of runtime,
configuration, plists and authority databases, and restores those bytes plus
only the initially loaded services if any later step fails. Its order is:

1. exact preflight;
2. controlled service stop and child drain;
3. fresh verified backup;
4. preliminary `pip --no-deps` of the accepted wheel;
5. stopped-window CAS installation of only `backup.keep_latest=3`;
6. the unchanged frozen `deploy/macos/install.sh`;
7. installed byte and configuration verification.

The execution command also writes `installed-verification.json` from those
live checks. The health finalizer consumes that exact artifact; no operator
needs to synthesize its fields.

The observer requires 45 healthy samples over at least 660 seconds from one
postdeployment controller. The finalizer rechecks every raw sample and the
installed wheel inventory, then writes a
`passed_pending_owner_publication` candidate receipt. It never edits or
publishes the accepted manifest.
