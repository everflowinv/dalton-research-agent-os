# Transcript polish fallback-chain recovery — 2026-09-10

## Problem

The deployed document-extraction path loaded a valid Cockpit routing policy
with more than one approved profile, then failed while constructing
`DocumentExtractionModelWorker`. Its shared transcript-polish base class still
required `filters.allowed_profile_ids` to contain exactly one profile. This
rejected the policy before `ModelRouter.route()` could apply the
`document_extraction` purpose override and choose an exact immutable profile.

## Change

`RoutedTranscriptPolishModelWorker` now accepts a policy when it has either a
nonempty legacy profile allow-list or an override for the worker's declared
purpose. It still rejects a policy with no approved candidates. The worker
continues to pass its purpose, policy version, credential slots, input/output
budget and persisted route lineage to `ModelRouter`; the router performs the
exact profile selection. No code indexes the allow-list or silently chooses
its first member.

For a policy carrying a purpose chain, the worker now uses the shared
`execute_chain` authority. Only `BrokerDefinitelyNotSent`, emitted after a
proved socket/preflight failure before any request bytes, advances to the next
approved link. A generic connection error, timeout, protocol error, or returned
result may describe paid or uncertain work, so it halts the chain and retains
the conservative reservation. The served invocation is persisted and charged
once.

The adapter calls the document-extraction admission hook after connecting and
immediately before shared-capacity dispatch and socket send. Thus a proved
not-sent first link creates no paid admission; the second link's immutable
admission names the route that actually serves. Admission state is bound to
`(work_order_ref, attempt_number)` and config authority is revalidated before
reuse, so one long-lived worker cannot lend the first WorkOrder's admission to
a second WorkOrder.

## Evidence

The new regression installs a legacy single-profile filter plus a
`document_extraction` explicit chain whose first profile is sent and raises a
synthetic broker connection failure. Its second profile lies outside the
legacy pin. A real `ModelRouter` records both chain links, the adapter executes
the second profile successfully, and exactly one invocation is persisted and
accounted.

Command:

```text
PYTHONPATH=src python3 -m unittest tests.test_transcript_polish_model_worker tests.test_document_extraction_policy_chain
```

The final focused run passed 75 tests:

```text
PYTHONPATH=src python3 -m unittest tests.test_transcript_polish_model_worker tests.test_document_extraction tests.test_document_extraction_admission tests.test_document_extraction_policy_chain tests.test_openclaw_model_adapter
```

No network, paid model call, live state write or deployment was performed.
