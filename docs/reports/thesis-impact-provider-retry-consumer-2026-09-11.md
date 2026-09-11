# Thesis-impact model retry consumer wiring — 2026-09-11

## Production consumer

The reachable path is the disabled-by-default `service.thesis_impact` block. A
short-lived `ThesisImpactProductionRunner` asks the scoped Writer for eligible
closed ResearchPlan targets, then executes the assessment and independent
verifier WorkOrders with `ThesisImpactModelWorker`. This change does not enable
the service, alter a human phase pin, approve a plan, or publish a thesis
change.

When the service is enabled, the resident Writer reads the canonical workspace
`config/service.json` and resolves both phase policies against the registered
ModelRouter before admitting new Work. Each assessment and verifier WorkOrder
binds its own purpose, capability, policy ref and content hash, credential
slots, adapter timeout, transport retry policy, and provider retry policy. The
existing per-purpose call-budget resolver remains authoritative: the two Work
budgets and their fingerprints are independent and immutable. Work admitted
before this contract remains executable only by a worker with no configured
retry policy; it is never rewritten.

The service config accepts four optional closed fields:

- `assessment_transport_retry` and `verifier_transport_retry`
- `assessment_provider_retry` and `verifier_provider_retry`

The common validators define their shapes. Thesis-impact refuses
`unknown_recovery`: the lane has no versioned fresh-Work recovery graph and
therefore does not claim that capability. The Writer derives the Scheduler
attempt and lease requirements from both exact route chains and transport
policies before creating the shared Scheduler authority. The production runner
constructs separate assessment and verifier adapters, including each phase's
queue wait, and injects the same policies into the worker.

## Retry, proof, and accounting behavior

A broker-returned eligible provider failure is retryable only when the common
`returned_provider_failure_proof` validates the invocation and result. Each
paid retry gets a new Scheduler attempt, ModelRouter decision, invocation, day
budget admission, and settlement. Same-profile retries are consumed first;
then the declared route chain supplies fallback profiles. Persisted retry state
is reconstructed from Scheduler/Core authority, and the embedded invocation and
failed ResultEnvelope are validated again rather than trusting copied proof
metadata.

Only `BrokerDefinitelyNotSent` may retry within one Scheduler attempt under the
transport policy. A returned capacity result needs the adapter's exact
definitely-not-sent proof to settle at zero. Every dispatched call with missing
cost telemetry retains the full Work reservation. A typed post-send socket
drop commits its ModelInvocation and `POST_SEND_RESULT_UNKNOWN` result, settles
the full reservation, and terminates the Work without retrying that invocation.
Thesis-impact does not yet have a fresh-Work unknown-result recovery graph.

Assessment and verification remain separate authorities. The verifier route
derives `producer_family` from the committed assessment invocation and retains
the existing different-family requirement. A provider retry cannot weaken that
constraint or select outside the verifier's exact policy.

## Focused verification

The focused tests exercise the real Unix-domain broker adapter for a returned
`RATE_LIMITED` failure followed by a fallback profile, and for a request sent
before the broker drops the connection. They assert new attempts and invocation
IDs, queue propagation, full unknown-metering settlement, exact phase Work
bindings, and the verifier's distinct policy hash. A production Writer test
starts from an enabled canonical service block after a clean Router checkpoint
and confirms the phase policies enlarge Scheduler authority and reach the
coordinator. Existing thesis-impact closure, policy rollover, day-budget,
replay, output-contract, and verifier-family tests remain in the focused suite.

The exact focused command was:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_thesis_impact_control tests.test_thesis_impact_production tests.test_thesis_impact_budget tests.test_service tests.test_writer_service tests.test_model_selection tests.test_model_budget_configuration tests.test_openclaw_model_adapter tests.test_provider_retry tests.test_scheduler tests.test_contracts tests.test_thesis_impact tests.test_thesis_impact_policy_rollover tests.test_model_router tests.test_model_fallback_chain tests.test_cockpit_model_fallback
```

It passed 370 tests in 46.994 seconds with zero failures or errors.
