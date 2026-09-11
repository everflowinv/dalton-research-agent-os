# Human-intent provider retry consumer

`OpenClawIntentInterpreter` is reachable from the owner-only control service when
`service.json#control.config.intent_composer` is present. `agenda_control.serve`
parses that block as `IntentComposerConfig` and constructs
`NaturalLanguageComposerPlane`, whose default interpreter is the OpenClaw-backed
worker. This change makes the existing `transport_retry` field and the new
`provider_retry` field part of the Work and Scheduler authority used by that
production path.

The owner-configurable block is:

```json
{
  "transport_retry": {
    "max_definitely_not_sent_retries": 1,
    "queue_wait_seconds": 600,
    "retry_backoff_seconds": 2
  },
  "provider_retry": {
    "max_same_profile_retries": 1,
    "retry_backoff_seconds": 2
  },
  "max_scheduler_attempts": 6
}
```

These fields extend the existing intent-composer object; its paths, route policy,
credential slots, token limits, timeout and cost limit remain owner configuration.
No model or provider is pinned by this change. Cockpit model selection rewrites only
the route policy and credential slots, preserving both retry policies. Re-running
workspace control setup also preserves the whole installed intent-composer block.
An installation that omits `provider_retry` keeps the previous one-call provider
behavior; activation is therefore an explicit `service.json` configuration change
and service restart, as model selection already reports for this resident consumer.

Each returned provider failure with the adapter's closed
`provider_completed_failure` proof completes one Scheduler attempt. The accepted
ResultEnvelope contains the exact ModelInvocation, broker response hash, selected
route and retry state. A next attempt creates a new route decision and invocation;
it retries the same profile only for the configured count, then excludes that
profile so the pinned policy can select its next link. The scheduler attempt bound
must cover the declared chain and same-profile retries. A larger explicit bound can
also accommodate broker-local capacity deferrals.

`BrokerDefinitelyNotSent` remains the only exception eligible for a same-attempt
transport retry. A broker-returned local-capacity result is retryable only with the
adapter's exact `definitely_not_sent` proof, which is retained with its invocation in
Scheduler history. Every other returned failure retains its invocation and is
terminal unless it has the closed provider-failure proof.

A UDS request that was sent before the connection dropped is terminal for this
Work. The typed `PostSendUnknownEvidence` invocation and ResultEnvelope are
persisted together, including unavailable metering and the request-frame hash. The
interpreter does not call the same invocation again and does not enable the
`unknown_recovery` policy used by the annual-report recovery graph. When shared
model capacity is configured through the workspace manifest, the OpenClaw adapter
therefore retains the full reservation for the unknown completion rather than
refunding it.

