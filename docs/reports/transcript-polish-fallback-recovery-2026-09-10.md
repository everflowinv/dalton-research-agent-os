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
`execute_chain` authority. A positively classified pre-invocation provider or
transport failure advances to the next approved link within the same bounded
Scheduler attempt. A returned result envelope remains on the normal accounting
path because it may already represent paid work; it is never skipped in order
to try another model. The served invocation is persisted and charged once.

Document extraction reserves once for the attempt, at the greatest
rate-card-derived amount among the configured purpose-chain candidates. A
pre-invocation failure can therefore reuse that reservation for the next link
without a second budget identity, while a more expensive fallback cannot
silently exceed the first profile's reservation.

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

Focused transcript/policy result: 8 tests passed. A broader extraction and
budget run passed 50 tests:

```text
PYTHONPATH=src python3 -m unittest tests.test_transcript_polish_model_worker tests.test_document_extraction tests.test_document_extraction_admission tests.test_document_extraction_policy_chain
```

No network, paid model call, live state write or deployment was performed.
