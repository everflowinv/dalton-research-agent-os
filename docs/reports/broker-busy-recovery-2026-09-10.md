# Broker BUSY recovery

Date: 2026-09-10

## Production failure

The broker's closed `BUSY` response (`broker concurrency limit reached`) crossed the OpenClaw adapter correctly, but `classify_model_failure` did not recognize the code. It became `unclassified_failure`, halted the brain chain, and Cockpit committed the content-addressed Scheduler work as terminal `failed`. Re-running the unchanged dossier request therefore replayed that failure forever.

## Correction

`BUSY` and `CONCURRENCY_LIMIT` are now the closed `capacity_busy` class. It halts the current chain without trying another profile because the broker concurrency limit is global and another model is not evidence of more host capacity. Cockpit records that attempt as Scheduler `retryable`, settles its budget reservation at zero, and raises the existing actionable failure to the lane. The next lane tick can claim the same work as the next Scheduler attempt after capacity returns.

The retry remains bounded by the Scheduler policy (`max_attempts`, currently three for this Cockpit scheduler). Each attempt gets a distinct route decision and budget admission; the failed BUSY attempt settles at zero and the successful attempt settles once at measured or governed fallback cost. Unknown broker codes remain `unclassified_failure` and terminal. Lane failure classification parks `capacity_busy` against `model_capacity`, using the existing dependency probe cadence rather than a busy loop.

The upgrade path recognizes only the exact legacy terminal envelope: `MODEL_CHAIN_EXHAUSTED` with one structured chain failure whose code is exactly `BUSY` and whose old class is `unclassified_failure`. It derives one versioned recovery request identity. It does not reopen or mutate the old Scheduler record, retry arbitrary historical failures, or recursively mint recovery identities. Codes merely containing `BUSY` remain unclassified.

Longer outages use the optional closed `capacity_retry` model-config block:

```json
{"cooldown_seconds": 1800, "max_recovery_epochs": 1, "scheduler_max_attempts": 3}
```

Those values are the conservative defaults. Setup preserves an existing validated block. When the block is explicit, its hash versions the base WorkOrder; absence preserves the historical identity and replays an existing success without another charge. A capacity-only Scheduler exhaustion waits for the configured cooldown, then derives at most `max_recovery_epochs` new content-addressed work identities; the policy hash and epoch are in each identity. Each epoch has the configured finite Scheduler attempt count. Exhausting every epoch reports `capacity_recovery_exhausted`, which follows the existing transient-to-held lane bound. Restart reads the Scheduler history and therefore cannot reset either attempt or epoch limits. Arbitrary failures never enter this path.

The company-dossier launcher reads the same validated block and supplies `cooldown_seconds` to its durable dependency budget. Thus a configured 60-second recovery is admitted after 60 seconds rather than being hidden behind the legacy 30-minute lane probe interval. A coordinator clock test covers initial failure, the single free probe, 59 seconds of zero dispatch, and admission at 60 seconds.

No live model call, configuration change, deployment, or budget increase was made.

## Verification

Focused command:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_openclaw_model_adapter \
  tests.test_model_fallback_chain \
  tests.test_cockpit_model_fallback \
  tests.test_lane_failure_classes
```

Coverage includes a real Unix-socket adapter stub carrying the signed BUSY error, exact classification, no same-attempt profile switch, a later successful attempt on the same profile, two settled admissions with zero charged to BUSY and one charge for success, chain replay behavior, and unchanged fail-closed handling for unknown errors.
