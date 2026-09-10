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

The execution, replay and accounting path is unchanged: one selected profile
is called for one Scheduler attempt, one invocation is persisted, and its
actual usage is recorded once. Adapter failures retain the existing bounded
Scheduler retry behavior. Broker-global capacity failures are therefore not
turned into an eager walk that could issue several paid calls during one
lease.

## Evidence

The new regression installs a legacy single-profile filter plus a
`document_extraction` explicit chain whose first profile is unavailable and
whose second profile lies outside that legacy pin. A real `ModelRouter` selects
the second eligible profile, the adapter executes once, and the normal
transcript artifact/accounting path succeeds.

Command:

```text
PYTHONPATH=src python3 -m unittest tests.test_transcript_polish_model_worker tests.test_document_extraction_policy_chain
```

Result: 8 tests passed.

No network, paid model call, live state write or deployment was performed.
