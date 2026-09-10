# DebateMap mission binding — 2026-09-10

## Outcome

`DebateMapVersion` schema 0.2 now binds every new map version to the exact
active CoverageMission version through `mission_version_ref` and
`mission_version_hash`. The write authority resolves those values from Core
and refuses stale or invented bindings, a mismatched automation principal, a
mission without the `debate_map` grant, or a subject outside the mission's
company universe and `industry_ref`.

Existing schema 0.1 records retain their original canonical JSON and content
hash and read with mission provenance unknown. No existing record is rewritten.

## Mission roll behavior

The lane treats a changed mission binding as new work even when the canonical
claim fingerprint is unchanged. Its persistent work signature includes the
mission id and hash, so a terminal result under an older mission cannot hide a
newly authorized mission input.

When the evidence fingerprint, constitution id/hash, and debate policy
id/hash are all unchanged, the CLI appends a `mission_rebind` version using
the already verified map. This path runs before model construction, makes zero
draft or verifier calls, and reports zero model cost. It does not claim that
new evidence arrived. A changed evidence fingerprint, constitution, or policy
uses the normal drafting and verification path. The DebateMap Q1 evidence
scope remains the cited claim refs; mission hashes are provenance rather than
evidence.

## Compatibility

Fresh stores admit the `mission_rebind` projection directly. An existing
SQLite table whose historical closed check predates this reason stores the
closest legacy index projection while preserving `mission_rebind` in the
canonical immutable record. Decode verifies that compatibility case and still
checks the canonical content hash.

## Validation

- Focused DebateMap and direct consumer suite: 507 tests passed.
- Lane failure-budget regression suite after binding its work signature: 36
  tests passed.
- Tests cover exact active mission authority, forged hash refusal, company and
  industry subject authorization, unchanged-claim mission versioning, a
  no-model rebind, policy/constitution fast-path exclusion, missing grant
  refusal, legacy 0.1 reads, and lane scheduling after a mission roll.
- `git diff --check` passed.

The test runner emitted pre-existing unclosed-SQLite `ResourceWarning`s in
consumer fixtures; all assertions passed.

## Deployment boundary

This change does not activate a lane, modify owner grants, deploy configuration,
or alter activation-readiness artifacts. A new mission still needs its real
`debate_map` authorization before the authority will publish its binding.
